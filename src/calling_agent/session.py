"""Relay between an AudioTransport and the AssemblyAI Voice Agent API.

The server does no audio processing: frames are already base64 in the encoding
the API expects, so both directions are a straight forward. All of the STT,
LLM and TTS work happens upstream.
"""

import asyncio
import json
import logging

import websockets
from websockets.asyncio.client import ClientConnection

from . import protocol as p
from .agent_config import build_session_resume, build_session_update, run_tool
from .config import settings
from .transport.base import AudioTransport

log = logging.getLogger(__name__)


class AgentSession:
    def __init__(self, transport: AudioTransport, resume_session_id: str | None = None) -> None:
        self._transport = transport
        self._resume_session_id = resume_session_id
        self._session_id: str | None = None
        self._ready = False

    async def run(self) -> None:
        """Open the upstream connection and pump audio until either side ends."""
        if not settings.assemblyai_api_key:
            await self._transport.send_event(
                {"type": "error", "message": "ASSEMBLYAI_API_KEY is not set on the server."}
            )
            return

        try:
            refused = await self._attempt(tune_turns=True, report_close=False)
            if refused:
                # Turn detection is the newest part of the payload and a single
                # unknown field there is refused with 1008, which would
                # otherwise take down the whole call rather than one setting.
                log.warning("session refused with turn detection; retrying without it")
                await self._attempt(tune_turns=False, report_close=True)
        except websockets.InvalidStatus as exc:
            # 401 = bad key. 404 = wrong path; docs list /v1/ws but the Twilio
            # example uses /v1/realtime, so surface the URL we actually tried.
            log.error("upstream rejected connection: %s", exc)
            await self._transport.send_event(
                {
                    "type": "error",
                    "message": (
                        f"AssemblyAI rejected the connection ({exc}). "
                        f"Check ASSEMBLYAI_API_KEY and ASSEMBLYAI_AGENT_WS_URL "
                        f"(currently {settings.assemblyai_agent_ws_url})."
                    ),
                }
            )
        except OSError as exc:
            log.error("could not reach upstream: %s", exc)
            await self._transport.send_event(
                {"type": "error", "message": f"Could not reach AssemblyAI: {exc}"}
            )
        finally:
            await self._transport.close()

    async def _attempt(self, *, tune_turns: bool, report_close: bool) -> bool:
        """Open one upstream session and pump it until it ends.

        Returns True if the session was refused before it ever became ready,
        which is the only case worth retrying with a smaller payload.
        """
        self._ready = False
        async with websockets.connect(
            settings.assemblyai_agent_ws_url,
            additional_headers={"Authorization": f"Bearer {settings.assemblyai_api_key}"},
            max_size=None,
        ) as upstream:
            if self._resume_session_id:
                opening = build_session_resume(self._resume_session_id)
                log.info("resuming session %s", self._resume_session_id)
            else:
                opening = build_session_update(self._transport.encoding, tune_turns=tune_turns)
            await upstream.send(json.dumps(opening))
            log.info(
                "upstream connected (encoding=%s, %d Hz, turn_tuning=%s)",
                self._transport.encoding,
                self._transport.sample_rate,
                tune_turns,
            )
            await self._pump(upstream, report_close=report_close)

            refusable = tune_turns and not self._resume_session_id
            return refusable and not self._ready and upstream.close_code == 1008

    async def _pump(self, upstream: ClientConnection, *, report_close: bool = True) -> None:
        """Run both directions concurrently; stop as soon as either finishes."""
        up = asyncio.create_task(self._client_to_agent(upstream), name="client->agent")
        down = asyncio.create_task(self._agent_to_client(upstream), name="agent->client")
        done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if (exc := task.exception()) is not None:
                log.error("%s failed: %s", task.get_name(), exc)

        if report_close:
            await self._report_close(upstream)

    async def _report_close(self, upstream: ClientConnection) -> None:
        """Explain an abnormal upstream close to the client.

        Code 1008 is the API refusing the session -- almost always a malformed
        session.update rather than anything transient -- so the reason it gives
        is the single most useful thing to see. Logging it alone hides it inside
        the platform's log viewer, where long lines get truncated.
        """
        code = upstream.close_code
        if code in (None, 1000, 1001):
            return

        reason = (upstream.close_reason or "").strip()
        log.error("upstream closed: code=%s reason=%r", code, reason)

        if code == 1008:
            detail = reason or "no reason given"
            message = (
                f"AssemblyAI rejected the session (1008 policy violation): {detail}. "
                "This is a configuration problem, not a network one -- check the "
                "session payload, voice name, and tool definitions."
            )
        else:
            message = f"Agent connection closed unexpectedly (code {code}). {reason}".strip()

        await self._transport.send_event({"type": "error", "code": code, "message": message})

    async def _client_to_agent(self, upstream: ClientConnection) -> None:
        async for chunk_b64 in self._transport.recv_audio():
            await upstream.send(
                json.dumps({"type": p.INPUT_AUDIO, p.INPUT_AUDIO_FIELD: chunk_b64})
            )

    async def _agent_to_client(self, upstream: ClientConnection) -> None:
        async for raw in upstream:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("dropping non-JSON frame from upstream")
                continue
            await self._handle_upstream(upstream, msg)

    async def _handle_upstream(self, upstream: ClientConnection, msg: dict) -> None:
        kind = msg.get("type")

        if kind == p.REPLY_AUDIO:
            # reply.audio carries "data", while input.audio carries "audio".
            # Reading the wrong key drops every spoken reply in silence.
            if audio := msg.get(p.REPLY_AUDIO_FIELD):
                await self._transport.send_audio(audio)
            return

        if kind == p.INPUT_SPEECH_STARTED:
            # Barge-in: the user cut in, so drop whatever is still queued for
            # playback or the agent talks over them.
            await self._transport.clear()

        elif kind == p.SESSION_READY:
            self._ready = True
            self._session_id = msg.get("session_id")
            log.info("session ready: %s", self._session_id)

        elif kind == p.TOOL_CALL:
            await self._handle_tool_call(upstream, msg)

        elif kind == p.SESSION_ERROR:
            log.error(
                "upstream session error: code=%s message=%s",
                msg.get("code"),
                msg.get("message"),
            )

        # Forward everything non-audio so the page can show live transcripts
        # and connection state.
        await self._transport.send_event(msg)

    async def _handle_tool_call(self, upstream: ClientConnection, msg: dict) -> None:
        name = msg.get("name", "")
        args = msg.get("arguments") or {}
        result, is_error = await asyncio.to_thread(run_tool, name, args)
        log.info("tool %s -> %s%s", name, result, " (error)" if is_error else "")
        await upstream.send(
            json.dumps(
                {
                    "type": p.TOOL_RESULT,
                    # The API pairs results by call_id; a wrong key leaves the
                    # agent waiting on a tool that already ran.
                    "call_id": msg.get("call_id"),
                    "result": result,
                    "is_error": is_error,
                }
            )
        )
