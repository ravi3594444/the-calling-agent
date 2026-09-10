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
from .agent_config import (
    FALLBACK_VOICE,
    build_session_resume,
    build_session_update,
    run_tool,
)
from .config import settings
from .transport.base import AudioTransport

log = logging.getLogger(__name__)

# How long to wait for reply.done before sending queued tool results anyway.
TOOL_RESULT_TIMEOUT = 2.0


class AgentSession:
    def __init__(
        self,
        transport: AudioTransport,
        resume_session_id: str | None = None,
        voice: str | None = None,
    ) -> None:
        self._transport = transport
        self._resume_session_id = resume_session_id
        self._voice = voice
        self._session_id: str | None = None
        self._ready = False
        # Set on barge-in. Chunks for the cancelled turn are already on the
        # wire and cannot be recalled, so they are dropped here instead of
        # played -- otherwise the agent talks over the caller for the second or
        # so of audio still in transit.
        self._suppress_audio = False
        # Set when an attempt drops something optional. Delivered on
        # session.ready rather than after the attempt returns, because an
        # attempt does not return until the whole call is over.
        self._pending_notice: str | None = None
        # Tool results wait here until the agent's current reply finishes.
        # The API expects tool.result on the next reply.done; sent mid-reply it
        # is not acted on, so the agent says "let me check" and then goes quiet
        # until the caller prompts it again.
        self._pending_tool_results: list[dict] = []
        self._reply_active = False
        self._flush_guard: asyncio.Task | None = None

    async def run(self) -> None:
        """Open the upstream connection and pump audio until either side ends."""
        if not settings.assemblyai_api_key:
            await self._transport.send_event(
                {"type": "error", "message": "ASSEMBLYAI_API_KEY is not set on the server."}
            )
            return

        try:
            # Progressively simpler payloads. A 1008 refusal names one bad
            # field but kills the whole session, so rather than lose the call
            # we drop the optional parts in order of how likely they are to be
            # the problem. Each entry is (tune_turns, voice, what_was_dropped).
            plan: list[tuple[bool, str | None, str | None]] = [
                (True, self._voice, None),
                (False, self._voice, "turn detection tuning"),
            ]
            requested = self._voice or settings.agent_voice
            if requested != FALLBACK_VOICE:
                # An unavailable voice id is the other common refusal, and the
                # vendor's own documented default is the safest thing to land on.
                plan.append((False, FALLBACK_VOICE, f"the '{requested}' voice"))

            for index, (tune_turns, voice, dropped) in enumerate(plan):
                last = index == len(plan) - 1
                if dropped:
                    log.warning("session refused; retrying without %s", dropped)
                self._pending_notice = (
                    f"Connected, but {dropped} was rejected and had to be dropped."
                    if dropped
                    else None
                )
                refused = await self._attempt(
                    tune_turns=tune_turns, voice=voice, report_close=last
                )
                if not refused or last:
                    return
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

    async def _attempt(
        self, *, tune_turns: bool, report_close: bool, voice: str | None = None
    ) -> bool:
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
                opening = build_session_update(
                    self._transport.encoding, tune_turns=tune_turns, voice=voice
                )
            await upstream.send(json.dumps(opening))
            log.info(
                "upstream connected (encoding=%s, %d Hz, turn_tuning=%s)",
                self._transport.encoding,
                self._transport.sample_rate,
                tune_turns,
            )
            await self._pump(upstream, report_close=report_close)

            # Whether this attempt was refused, not whether it is worth
            # retrying -- that decision belongs to the caller's plan. Tying it
            # to tune_turns here made every later attempt look like a success.
            if self._resume_session_id:
                return False
            return not self._ready and upstream.close_code == 1008

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
            # Late audio from a turn the caller interrupted.
            if self._suppress_audio:
                return
            # reply.audio carries "data", while input.audio carries "audio".
            # Reading the wrong key drops every spoken reply in silence.
            if audio := msg.get(p.REPLY_AUDIO_FIELD):
                await self._transport.send_audio(audio)
            return

        if kind == p.INPUT_SPEECH_STARTED and settings.allow_interruptions:
            # Barge-in: the user cut in, so drop whatever is still queued for
            # playback or the agent talks over them. Gated on the same setting
            # that tells the API to allow interruptions -- otherwise disabling
            # them upstream would still cut playback here.
            self._suppress_audio = True
            await self._transport.clear()

        elif kind == p.REPLY_STARTED:
            # A genuinely new turn; audio is wanted again.
            self._suppress_audio = False
            self._reply_active = True

        elif kind == p.REPLY_DONE:
            self._reply_active = False
            await self._flush_tool_results(upstream)

        elif kind == p.SESSION_READY:
            self._ready = True
            self._session_id = msg.get("session_id")
            log.info("session ready: %s", self._session_id)
            if self._pending_notice:
                await self._transport.send_event(
                    {"type": "notice", "message": self._pending_notice}
                )
                self._pending_notice = None

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

        self._pending_tool_results.append(
            {
                "type": p.TOOL_RESULT,
                # The API pairs results by call_id; a wrong key leaves the
                # agent waiting on a tool that already ran.
                "call_id": msg.get("call_id"),
                "result": result,
                "is_error": is_error,
            }
        )

        if not self._reply_active:
            # No reply in progress to wait for -- send straight away, or the
            # result would sit here until some later turn happened to flush it.
            await self._flush_tool_results(upstream)
            return

        # Safety net: if reply.done never arrives the agent would wait forever
        # on a tool that has already run.
        if self._flush_guard is None or self._flush_guard.done():
            self._flush_guard = asyncio.create_task(self._flush_after_timeout(upstream))

    async def _flush_after_timeout(self, upstream: ClientConnection) -> None:
        await asyncio.sleep(TOOL_RESULT_TIMEOUT)
        if self._pending_tool_results:
            log.warning("no reply.done within %ss; sending tool results anyway",
                        TOOL_RESULT_TIMEOUT)
            await self._flush_tool_results(upstream)

    async def _flush_tool_results(self, upstream: ClientConnection) -> None:
        if not self._pending_tool_results:
            return
        queued, self._pending_tool_results = self._pending_tool_results, []
        if self._flush_guard is not None and not self._flush_guard.done():
            self._flush_guard.cancel()
        for payload in queued:
            await upstream.send(json.dumps(payload))
        log.info("sent %d tool result(s)", len(queued))
