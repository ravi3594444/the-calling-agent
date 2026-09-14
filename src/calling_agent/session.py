"""Relay between an AudioTransport and the AssemblyAI Voice Agent API.

The server does no audio processing: frames are already base64 in the encoding
the API expects, so both directions are a straight forward. All of the STT,
LLM and TTS work happens upstream.
"""

import asyncio
import json
import logging
from collections import OrderedDict
from time import monotonic, perf_counter
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from . import protocol as p
from .agent_config import (
    FALLBACK_VOICE,
    RESTAURANT,
    agent_mode_conflict,
    build_session_resume,
    build_session_update,
)
from .agent_spec import AgentDefinition
from .config import settings
from .transport.base import AudioTransport

log = logging.getLogger(__name__)

# How long to wait for reply.done before sending queued tool results anyway.
TOOL_RESULT_TIMEOUT = 2.0

# --- Tool results, which have to outlive the connection that produced them ---
#
# A reconnect builds a NEW AgentSession -- main.ws() constructs one per socket,
# including for ?resume=<id> -- so a cache held on the instance is empty on
# exactly the reconnect it exists to protect. The upstream session outlives the
# dropped socket, re-issues the tool call it never got an answer to, the cache
# misses, and a tool with a side effect runs twice: a second booking against
# the same seats, for a caller who said "book it" once.
#
# So the cache lives out here, keyed by the upstream session id that ?resume=
# names, and a session borrows the one belonging to its call. Scoping is the
# point: one call's results are reachable only through that call's id, never
# from another conversation.
#
# It is process-global state on a long-lived server, so it is bounded three
# ways -- how many sessions are remembered, how many calls within a session,
# and for how long. Everything here runs on the event loop, never in the tool
# worker thread, so it needs no lock.

CACHED_SESSIONS = 64
CACHED_CALLS_PER_SESSION = 32
# Generous next to the ~30s the API holds a session open after a drop, and
# still short enough that a busy server forgets a finished call quickly.
CACHE_TTL_SECONDS = 300.0


class SessionToolResults:
    """Tool results for ONE upstream session. Two jobs, both about a tool
    that has ALREADY RUN and must not run again.

    `get`/`remember` answer a re-issued call from what the first run returned,
    instead of repeating its side effect.

    `hold`/`release` carry results that never reached the upstream across the
    drop. Those tools ran; the upstream is still waiting on their call_ids and
    on a resume it is still waiting, so throwing the results away (which is
    what the teardown used to do) strands the call rather than losing a detail.
    """

    def __init__(self, max_calls: int = CACHED_CALLS_PER_SESSION) -> None:
        self._max_calls = max_calls
        self._results: OrderedDict[str, dict] = OrderedDict()
        self._undelivered: list[dict] = []
        self.touched = monotonic()

    def get(self, call_id: str) -> dict | None:
        payload = self._results.get(call_id)
        if payload is not None:
            self._results.move_to_end(call_id)
        return payload

    def remember(self, call_id: str, payload: dict) -> None:
        # An empty id is the provider omitting one, not an identity: two
        # unrelated calls both arrive as "", so a result recorded under it
        # would answer the second of them with the first one's answer. This is
        # the one door into `_results`, so refusing here is what makes `get`
        # safe for an id that is not one -- and guarding both ends instead
        # would leave neither guard able to fail a test on its own.
        if not call_id:
            return
        self._results[call_id] = payload
        self._results.move_to_end(call_id)
        # Oldest call in this conversation first. A re-issue follows its
        # original within a turn or two, so the horizon that matters is a
        # handful of calls; this bound is an order of magnitude past it.
        while len(self._results) > self._max_calls:
            self._results.popitem(last=False)

    def hold(self, payloads: list[dict]) -> None:
        """Keep results that have not reached the upstream yet."""
        if not payloads:
            return
        self._undelivered.extend(payloads)
        del self._undelivered[: -self._max_calls]

    def release(self) -> list[dict]:
        """Take everything held, to deliver on a session that is live again."""
        held, self._undelivered = self._undelivered, []
        return held


class ToolResultStore:
    """Every recent session's results, keyed by upstream session id."""

    def __init__(
        self, max_sessions: int = CACHED_SESSIONS, ttl: float = CACHE_TTL_SECONDS
    ) -> None:
        self._max_sessions = max_sessions
        self._ttl = ttl
        self._sessions: OrderedDict[str, SessionToolResults] = OrderedDict()

    def claim(self, session_id: str | None) -> SessionToolResults:
        """The cache for `session_id`, created on first sight of it.

        A connection that has no id yet -- every fresh call, until session.ready
        names one -- gets a private cache, which `bind` registers as soon as
        the id arrives. It is never a shared bucket: an unidentified call must
        not be able to see another call's results.
        """
        if not session_id:
            return SessionToolResults()
        cache = self._sessions.get(session_id) or SessionToolResults()
        self.bind(session_id, cache)
        return cache

    def bind(self, session_id: str, cache: SessionToolResults) -> None:
        """Make `session_id` name this cache, so a later ?resume= finds it.

        The API may answer a resume with a different session id from the one
        that was resumed; binding both to the same cache keeps the chain
        unbroken across a second drop.
        """
        if not session_id:
            return
        cache.touched = monotonic()
        self._sessions[session_id] = cache
        self._sessions.move_to_end(session_id)
        self._expire()

    def _expire(self) -> None:
        cutoff = monotonic() - self._ttl
        for session_id, cache in list(self._sessions.items()):
            if cache.touched < cutoff:
                del self._sessions[session_id]
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)  # least recently touched


TOOL_RESULTS = ToolResultStore()


class AgentSession:
    def __init__(
        self,
        transport: AudioTransport,
        resume_session_id: str | None = None,
        voice: str | None = None,
        agent: AgentDefinition | None = None,
    ) -> None:
        self._transport = transport
        # What this session IS. The relay below knows nothing about it beyond
        # these two uses -- the opening payload, and running a tool call.
        self._agent = agent or RESTAURANT
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
        # One ordered worker owns tool execution. Awaiting a threaded tool in
        # the audio reader still blocks that reader; a separate queue does not.
        self._tool_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=16)
        # Borrowed, not owned: this object is THIS CALL's, and a reconnect for
        # the same call gets the same one back. See ToolResultStore above for
        # why it cannot live on the instance.
        self._tool_cache = TOOL_RESULTS.claim(resume_session_id)

    async def run(self) -> None:
        """Open the upstream connection and pump audio until either side ends."""
        if not settings.assemblyai_api_key:
            await self._transport.send_event(
                {"type": "error", "message": "ASSEMBLYAI_API_KEY is not set on the server."}
            )
            await self._transport.close()
            return

        # Before the socket, because this call cannot work and the upstream is
        # billed by the minute. Without it the session opens, the agent talks,
        # and every single tool call comes back "No tool named X" -- the one
        # failure mode where the caller hears a working phone and nothing else
        # in the system says a word about why it can do nothing.
        if conflict := agent_mode_conflict(self._agent):
            log.error("%s", conflict)
            await self._transport.send_event(
                {"type": "error", "fatal": True, "message": conflict}
            )
            await self._transport.close()
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
            requested = self._voice or self._agent.voice or settings.agent_voice
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
                refused = await self._attempt(tune_turns=tune_turns, voice=voice, report_close=last)
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
                {"type": "error", "fatal": False, "message": f"Could not reach AssemblyAI: {exc}"}
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
            open_timeout=10,
        ) as upstream:
            if self._resume_session_id:
                opening = build_session_resume(self._resume_session_id)
                log.info("resuming session %s", self._resume_session_id)
            else:
                opening = build_session_update(
                    self._transport.encoding,
                    tune_turns=tune_turns,
                    voice=voice,
                    agent=self._agent,
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
        worker = asyncio.create_task(self._run_tools(upstream), name="tools")
        tasks = {up, down, worker}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and (exc := task.exception()) is not None:
                    log.error("%s failed: %s", task.get_name(), exc)
        finally:
            # No guard or worker may send into a closed/replaced upstream.
            if self._flush_guard is not None:
                tasks.add(self._flush_guard)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._flush_guard = None
            # These are results for tools that ALREADY RAN. Clearing them --
            # which is what this used to do -- loses them for good: the
            # upstream session outlives the socket, it is still waiting on
            # those call_ids, and it does not ask twice. Hand them to the
            # call's cache so the reconnect delivers them.
            self._tool_cache.hold(self._pending_tool_results)
            self._pending_tool_results = []
            self._reply_active = False
            self._tool_queue = asyncio.Queue(maxsize=16)

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

        await self._transport.send_event(
            {"type": "error", "code": code, "fatal": code == 1008, "message": message}
        )

    async def _client_to_agent(self, upstream: ClientConnection) -> None:
        async for chunk_b64 in self._transport.recv_audio():
            await upstream.send(json.dumps({"type": p.INPUT_AUDIO, p.INPUT_AUDIO_FIELD: chunk_b64}))

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
            # Register the cache under the id the client will reconnect with.
            # A resume may come back under a new id; both must reach the same
            # results, or the second drop re-runs what the first one paid for.
            if self._session_id:
                TOOL_RESULTS.bind(self._session_id, self._tool_cache)
            if held := self._tool_cache.release():
                # Tools that ran on the connection that dropped. The upstream
                # has been waiting on these since before the reconnect, and
                # there is no reply in progress to hold them behind.
                log.info("delivering %d held tool result(s) after reconnect", len(held))
                self._pending_tool_results.extend(held)
                await self._flush_tool_results(upstream)
            if self._pending_notice:
                await self._transport.send_event(
                    {"type": "notice", "message": self._pending_notice}
                )
                self._pending_notice = None

        elif kind == p.TOOL_CALL:
            # Keep consuming audio/interruptions while tools are running.
            try:
                self._tool_queue.put_nowait(msg)
            except asyncio.QueueFull:
                await self._transport.send_event(
                    {
                        "type": "error",
                        "message": "Too many pending actions. Please start a new call.",
                    }
                )
                raise RuntimeError("tool queue full") from None
            return

        elif kind == p.SESSION_ERROR:
            log.error(
                "upstream session error: code=%s message=%s",
                msg.get("code"),
                msg.get("message"),
            )

        # Forward everything non-audio so the page can show live transcripts
        # and connection state.
        await self._transport.send_event(msg)

    async def _run_tools(self, upstream: ClientConnection) -> None:
        while True:
            msg = await self._tool_queue.get()
            try:
                await self._handle_tool_call(upstream, msg)
            except Exception:
                # This worker COMPLETING is what ends the call: _pump waits on
                # FIRST_COMPLETED and cancels both audio directions the moment
                # any of its three tasks finishes, so an exception here cut the
                # caller off mid-sentence. Whatever went wrong with one tool
                # call -- a transport that went away, a result that would not
                # serialise -- the caller is still on the line. Log it, take
                # the next call. `_execute_tool` has already answered the agent
                # for anything the tool itself did.
                log.exception("tool call %r failed; the call continues", msg.get("name"))
            finally:
                self._tool_queue.task_done()

    async def _execute_tool(self, name: str, args: dict, call_id: str) -> tuple[Any, bool]:
        """Run one tool in a worker thread and always come back with a result.

        `run_tool` is documented as not raising, but AGENT_FACTORY means the
        implementation belongs to somebody else: a database timeout raises, and
        returning a bare string instead of the documented (result, is_error)
        pair makes the unpack raise here instead. Either one used to escape the
        worker task and take the whole call down with it.

        `is_error` exists so that a tool which fails is reported to the agent,
        which can then tell the caller something true, rather than ending the
        conversation. That is what this returns in every failing case.
        """
        try:
            outcome = await asyncio.to_thread(self._agent.run_tool, name, args, call_id)
        except Exception as exc:
            log.exception("tool %s raised; reporting the failure to the agent", name)
            return f"The {name} tool failed and did not complete: {exc}", True

        if isinstance(outcome, tuple | list) and len(outcome) == 2:
            result, is_error = outcome
            return result, bool(is_error)
        if isinstance(outcome, str):
            # A pair is the contract; a bare string is an implementation that
            # forgot the flag, and its text is still the real answer. Unpacking
            # it would tear it in half rather than fail loudly -- ("ok" becomes
            # result "o", is_error "k") -- so take it whole and say so once.
            log.warning("tool %s returned a bare string, not (result, is_error)", name)
            return outcome, False
        log.error("tool %s returned %s, not (result, is_error)", name, type(outcome).__name__)
        return f"The {name} tool answered in a way I could not read.", True

    async def _handle_tool_call(self, upstream: ClientConnection, msg: dict) -> None:
        name = msg.get("name", "")
        args = msg.get("arguments") or {}
        # Exactly what the provider sent, or "" when it sent nothing. Never
        # invented, renumbered or replaced with the session id: another
        # codebase derives the idempotency key for a real side effect from this
        # value, and an id this server made up would be a different id after a
        # reconnect -- which turns a retry into a second order. See agent_spec.
        call_id = str(msg.get("call_id") or "")
        cached = self._tool_cache.get(call_id)
        if cached is not None:
            # Already run, on the connection that dropped before its result
            # could be delivered. Answer from the record instead of repeating
            # the side effect.
            log.info("tool %s (call_id=%s) answered from the session cache", name, call_id)
            self._pending_tool_results.append(cached)
        else:
            await self._transport.send_event(
                {"type": "tool.activity", "status": "started", "name": name, "call_id": call_id}
            )
            started = perf_counter()
            result, is_error = await self._execute_tool(name, args, call_id)
            elapsed_ms = round((perf_counter() - started) * 1000)
            # Do not put caller names, phone numbers or booking notes in logs.
            log.info("tool %s completed in %dms (error=%s)", name, elapsed_ms, is_error)

            payload: dict[str, Any] = {
                "type": p.TOOL_RESULT,
                "result": result,
                "is_error": is_error,
            }
            # The API pairs a result to its call by this id. An explicit null
            # is not the same as saying nothing: it is a value the API cannot
            # pair with anything, so when the provider omitted the id the key
            # is left out entirely.
            if call_id:
                payload["call_id"] = call_id
            self._pending_tool_results.append(payload)
            self._tool_cache.remember(call_id, payload)
            # UI evidence comes from the executed tool, never a transcript guess.
            activity = {
                "type": "tool.activity",
                "status": "error" if is_error else "completed",
                "name": name,
                "call_id": call_id,
                "result": str(result),
                "duration_ms": elapsed_ms,
            }
            if receipt := getattr(result, "receipt", None):
                activity["receipt"] = receipt
            # Flush ready results before announcing completion to the browser.
            if not self._reply_active:
                await self._flush_tool_results(upstream)
            await self._transport.send_event(activity)

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
            log.warning(
                "no reply.done within %ss; sending tool results anyway", TOOL_RESULT_TIMEOUT
            )
            await self._flush_tool_results(upstream)

    async def _flush_tool_results(self, upstream: ClientConnection) -> None:
        if not self._pending_tool_results:
            return
        queued, self._pending_tool_results = self._pending_tool_results, []
        if (
            self._flush_guard is not None
            and not self._flush_guard.done()
            and self._flush_guard is not asyncio.current_task()
        ):
            self._flush_guard.cancel()
        for index, payload in enumerate(queued):
            try:
                await upstream.send(json.dumps(payload))
            except BaseException:
                # The socket went, or teardown cancelled this task, partway
                # through delivering results for tools that have already run.
                # What is left goes back to the call's cache for the reconnect
                # rather than onto the floor -- CancelledError included, which
                # is why this catches BaseException and re-raises untouched.
                self._tool_cache.hold(queued[index:])
                raise
        log.info("sent %d tool result(s)", len(queued))
