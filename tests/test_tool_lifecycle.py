"""What happens to a tool call when something goes wrong around it.

Three failures that all ended the same way -- the caller cut off mid-sentence,
or a side effect run twice -- and none of which the agent could report:

  * a tool that RAISES. `_run_tools` had no `except`, so the exception
    completed the worker task, `_pump` (waiting on FIRST_COMPLETED) cancelled
    both audio directions, and `run()` returned while the caller was talking.
  * a tool that already RAN. Its result lived in a cache on the AgentSession,
    but a reconnect builds a new AgentSession, so the cache was empty on
    exactly the reconnect it exists to protect; and the teardown cleared the
    results that had not been delivered yet.
  * a tool call the provider did not number. `call_id: null` went upstream,
    which the API cannot pair with anything.

The doubles here derive everything from what they are PASSED -- the tool name,
the arguments, the call id -- so a relay that dispatched the wrong call, or
answered one call with another's result, fails these instead of passing them.
"""

import asyncio
import json

import pytest

from calling_agent import protocol as p
from calling_agent import session as session_module
from calling_agent.agent_spec import AgentDefinition
from calling_agent.session import SessionToolResults, ToolResultStore


class Transporte:
    """The browser end. Records what the relay tells the page."""

    encoding = p.ENCODING_PCM
    sample_rate = p.PCM_SAMPLE_RATE

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.audio: list[str] = []
        self.closed = False

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, chunk_b64: str) -> None:
        self.audio.append(chunk_b64)

    async def clear(self) -> None:
        self.events.append({"type": "clear"})

    async def close(self) -> None:
        self.closed = True

    async def recv_audio(self):
        await asyncio.Future()  # a caller who has not hung up
        yield ""


class Upstream:
    """The AssemblyAI end. Records the frames the relay sends it."""

    close_code = 1000
    close_reason = ""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        await asyncio.sleep(0)  # a real send suspends
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Future()  # a session that stays open


def _sesion(run_tool, *, resume: str | None = None, transporte=None):
    """A session whose ONLY tool is `run_tool`, as AGENT_FACTORY would build it."""
    return session_module.AgentSession(
        transporte or Transporte(),
        resume_session_id=resume,
        agent=AgentDefinition(
            name="under test",
            build_prompt=lambda: "prompt",
            greeting="hello",
            tools=[],
            run_tool=run_tool,
        ),
    )


async def _drop_the_socket(sesion, upstream) -> None:
    """Run the REAL teardown, the way a dropped call runs it.

    `_pump`'s `finally` is the code under test in the held-results tests, so
    the test must not hand-roll its own version of it: a copy cannot disagree
    with the original about anything.
    """
    pump = asyncio.create_task(sesion._pump(upstream))
    await asyncio.sleep(0)
    pump.cancel()
    await asyncio.gather(pump, return_exceptions=True)


@pytest.fixture(autouse=True)
def tienda_vacia(monkeypatch):
    """A fresh result store per test; it is process-global in production."""
    monkeypatch.setattr(session_module, "TOOL_RESULTS", ToolResultStore())


# --- a tool that fails must cost the tool, not the call ----------------------


async def test_a_raising_tool_is_reported_to_the_agent_and_the_next_call_runs():
    """The whole call used to end here.

    AGENT_FACTORY invites third-party tools, and a database timeout raises like
    anything else. The exception completed the worker task, which is what
    `_pump` waits on, so both audio directions were cancelled and the caller
    was cut off. `is_error` exists so the agent can say "I could not do that"
    instead.
    """
    corridas: list[str] = []

    def herramientas(name, args, call_id=""):
        corridas.append(name)
        if name == "book_table":
            raise TimeoutError("the reservations database did not answer")
        return f"{name} is fine", False

    sesion = _sesion(herramientas)
    upstream = Upstream()
    worker = asyncio.create_task(sesion._run_tools(upstream))
    try:
        sesion._tool_queue.put_nowait(
            {"type": "tool.call", "name": "book_table", "call_id": "c-1", "arguments": {}}
        )
        sesion._tool_queue.put_nowait(
            {"type": "tool.call", "name": "restaurant_info", "call_id": "c-2", "arguments": {}}
        )
        await asyncio.wait_for(sesion._tool_queue.join(), 2)

        # The failure is ANSWERED, not swallowed: an upstream still waiting on
        # c-1 leaves the agent silent mid-booking just as surely as a crash.
        assert [frame["call_id"] for frame in upstream.sent] == ["c-1", "c-2"]
        assert upstream.sent[0]["is_error"] is True
        assert "book_table" in upstream.sent[0]["result"]
        # ...and the call carries on, with the next tool working normally.
        assert upstream.sent[1] == {
            "type": p.TOOL_RESULT,
            "result": "restaurant_info is fine",
            "is_error": False,
            "call_id": "c-2",
        }
        assert corridas == ["book_table", "restaurant_info"]
        assert not worker.done(), "the worker died; _pump would tear the call down"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_a_transport_that_fails_mid_tool_does_not_end_the_call():
    """The outer net, for what `_execute_tool` cannot answer for.

    Reporting a tool to the browser is not part of running it. If that send
    fails -- a page that went away, a socket closing under us -- the exception
    still travelled out of the worker and ended the upstream call for a caller
    who may be on a phone rather than at the page.
    """

    class TransporteRoto(Transporte):
        async def send_event(self, event):
            if event.get("status") == "started" and event.get("call_id") == "c-1":
                raise ConnectionResetError("the page went away")
            await super().send_event(event)

    llamadas: list[str] = []

    def herramienta(name, args, call_id=""):
        llamadas.append(call_id)
        return "done", False

    sesion = _sesion(herramienta, transporte=TransporteRoto())
    upstream = Upstream()
    worker = asyncio.create_task(sesion._run_tools(upstream))
    try:
        sesion._tool_queue.put_nowait({"name": "book_table", "call_id": "c-1", "arguments": {}})
        sesion._tool_queue.put_nowait({"name": "book_table", "call_id": "c-2", "arguments": {}})
        await asyncio.wait_for(sesion._tool_queue.join(), 2)

        assert not worker.done(), "one failed browser event ended the whole call"
        assert [frame["call_id"] for frame in upstream.sent] == ["c-2"]
        assert llamadas == ["c-2"]
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_a_tool_that_forgets_the_error_flag_keeps_its_answer():
    """`return "Booked ABC12"` instead of `return "Booked ABC12", False`.

    The unpack raised ValueError for most strings -- ending the call -- and
    silently tore two-character ones in half. Neither is something to do to an
    answer the tool actually produced.
    """

    def sin_bandera(name, args, call_id=""):
        return "Booked: 4 for Ravi, reference ABC12"

    sesion = _sesion(sin_bandera)
    upstream = Upstream()
    await sesion._handle_tool_call(
        upstream, {"name": "book_table", "call_id": "c-1", "arguments": {}}
    )

    assert len(upstream.sent) == 1
    assert upstream.sent[0]["result"] == "Booked: 4 for Ravi, reference ABC12"
    assert upstream.sent[0]["is_error"] is False


# --- a tool that already ran must not run again ------------------------------


async def test_a_reconnect_answers_a_re_issued_call_without_running_it_again():
    """The double booking.

    The caller says "book it"; the tool creates the reservation; the socket
    drops before the result is delivered. The upstream session outlives the
    socket and re-issues the SAME call_id when the client reconnects -- but
    ws() builds a new AgentSession for `?resume=`, so an idempotency cache held
    on the instance was empty at precisely that moment and the booking was made
    twice, against the same twelve seats.

    Two consumers of the same cache hit, so both are asserted: the tool must
    not run, and the upstream must still receive the first run's result -- it
    is waiting on that call_id and does not ask a third time.
    """
    reservas: list[str] = []

    def book_table(name, args, call_id=""):
        reservas.append(call_id)
        return f"Booked, reference ABC{len(reservas)}", False

    primera = _sesion(book_table)
    up1 = Upstream()
    await primera._handle_upstream(up1, {"type": p.SESSION_READY, "session_id": "S-1"})
    await primera._handle_tool_call(
        up1, {"name": "book_table", "call_id": "call-1", "arguments": {}}
    )
    assert reservas == ["call-1"]

    # The socket drops; the browser reconnects with the id it stored, which is
    # a NEW AgentSession; the upstream re-issues the call it never got an
    # answer to.
    segunda = _sesion(book_table, resume="S-1")
    up2 = Upstream()
    await segunda._handle_tool_call(
        up2, {"name": "book_table", "call_id": "call-1", "arguments": {}}
    )

    assert reservas == ["call-1"], "the table was booked a second time"
    assert [frame["result"] for frame in up2.sent] == ["Booked, reference ABC1"]
    assert up2.sent[0]["call_id"] == "call-1"


async def test_one_call_s_results_are_never_visible_to_another():
    """Scope. Two conversations, and a provider that numbers both from one.

    A cache that remembered call ids without remembering whose they were would
    answer a stranger's first booking with somebody else's.
    """
    llamadas: list[tuple[str, dict]] = []

    def book_table(name, args, call_id=""):
        llamadas.append((call_id, dict(args)))
        return f"Booked for {args.get('name')}", False

    una = _sesion(book_table)
    await una._handle_upstream(Upstream(), {"type": p.SESSION_READY, "session_id": "S-1"})
    await una._handle_tool_call(
        Upstream(), {"name": "book_table", "call_id": "call-1", "arguments": {"name": "Ravi"}}
    )

    otra = _sesion(book_table)
    up = Upstream()
    await otra._handle_upstream(up, {"type": p.SESSION_READY, "session_id": "S-2"})
    await otra._handle_tool_call(
        up, {"name": "book_table", "call_id": "call-1", "arguments": {"name": "Priya"}}
    )

    assert llamadas == [("call-1", {"name": "Ravi"}), ("call-1", {"name": "Priya"})]
    assert [frame["result"] for frame in up.sent] == ["Booked for Priya"]


async def test_results_of_tools_that_already_ran_survive_a_dropped_socket():
    """The other half of the same reconnect, and the one with no second chance.

    A result queued behind an in-progress reply has been EXECUTED. The teardown
    used to clear the queue, so the upstream never heard about a booking that
    exists, and on resume it was still waiting on that call_id -- a call that
    cannot finish, holding a table nobody will be told about.

    Two consumers again -- the teardown that keeps them, and session.ready that
    delivers them -- so mutating either one has to break this.
    """

    def book_table(name, args, call_id=""):
        return "Booked, reference ABC12", False

    primera = _sesion(book_table)
    up1 = Upstream()
    await primera._handle_upstream(up1, {"type": p.SESSION_READY, "session_id": "S-9"})
    primera._reply_active = True  # the agent is mid-sentence
    await primera._handle_tool_call(
        up1, {"name": "book_table", "call_id": "call-9", "arguments": {}}
    )
    assert not up1.sent, "a result mid-reply is held back, as designed"

    await _drop_the_socket(primera, up1)
    assert not up1.sent, "and the drop is what loses it"

    segunda = _sesion(book_table, resume="S-9")
    up2 = Upstream()
    await segunda._handle_upstream(up2, {"type": p.SESSION_READY, "session_id": "S-9"})

    assert [frame.get("call_id") for frame in up2.sent] == ["call-9"]
    assert up2.sent[0]["result"] == "Booked, reference ABC12"
    assert up2.sent[0]["type"] == p.TOOL_RESULT


# --- the store is process-global, so it has to forget things -----------------


def test_the_store_forgets_old_sessions_rather_than_growing_without_limit():
    tienda = ToolResultStore(max_sessions=3)

    vieja = tienda.claim("S-old")
    vieja.remember("call-1", {"result": "from a call that ended long ago"})
    tienda.claim("S-0")
    tienda.claim("S-1")
    reciente = tienda.claim("S-2")
    reciente.remember("call-2", {"result": "from the call still in progress"})

    # Bounded by dropping the LEAST RECENT, not by dropping anything handy:
    # a live call losing its own results would re-run its own side effects.
    assert tienda.claim("S-2").get("call-2") == {"result": "from the call still in progress"}
    assert tienda.claim("S-old").get("call-1") is None


def test_the_store_forgets_a_session_that_is_long_over():
    tienda = ToolResultStore(ttl=60.0)
    vieja = tienda.claim("S-1")
    vieja.remember("call-1", {"result": "stale"})
    vieja.touched -= 61  # that call ended a minute ago

    tienda.claim("S-2")  # any later activity sweeps it

    assert tienda.claim("S-1").get("call-1") is None


def test_a_session_forgets_its_oldest_calls_first():
    cache = SessionToolResults(max_calls=2)
    for numero in (1, 2, 3):
        cache.remember(f"call-{numero}", {"result": numero})

    assert cache.get("call-1") is None
    assert cache.get("call-3") == {"result": 3}


# --- the id the API pairs a result to ----------------------------------------


async def test_a_tool_call_the_provider_did_not_number_sends_no_null_id():
    """`"call_id": null` is not the same as saying nothing.

    It is a value the API cannot pair with any call, so the result is lost and
    the agent waits. And the id handed to run_tool stays exactly what the
    provider sent -- another codebase derives the idempotency key for a real
    order from it, and an id invented here would be a different one after a
    reconnect, turning a retry into a second order.
    """
    vistos: list[str] = []

    def restaurant_info(name, args, call_id=""):
        vistos.append(call_id)
        return "Opening hours are noon to ten.", False

    sesion = _sesion(restaurant_info)
    upstream = Upstream()
    await sesion._handle_tool_call(upstream, {"name": "restaurant_info", "arguments": {}})

    assert len(upstream.sent) == 1
    assert "call_id" not in upstream.sent[0]
    assert upstream.sent[0]["result"] == "Opening hours are noon to ten."
    # Not a uuid, not a counter, not the session id: the provider's own absence.
    assert vistos == [""]


async def test_two_unnumbered_calls_are_two_calls_and_not_one():
    """An empty id is the ABSENCE of an identity, so it must not become one.

    Caching on it -- or letting the downstream key an order on it -- would
    answer the caller's second request with the first one's result.
    """
    pedidos: list[dict] = []

    def book_table(name, args, call_id=""):
        pedidos.append(dict(args))
        return f"Booked for {args.get('name')}", False

    sesion = _sesion(book_table)
    upstream = Upstream()
    await sesion._handle_tool_call(upstream, {"name": "book_table", "arguments": {"name": "Ravi"}})
    await sesion._handle_tool_call(
        upstream, {"name": "book_table", "arguments": {"name": "Priya"}}
    )

    assert pedidos == [{"name": "Ravi"}, {"name": "Priya"}]
    assert [frame["result"] for frame in upstream.sent] == [
        "Booked for Ravi",
        "Booked for Priya",
    ]
