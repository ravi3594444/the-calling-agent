"""Regressions for call latency, cleanup, receipts and concurrent reservations."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from calling_agent import main as main_module
from calling_agent import restaurant
from calling_agent import session as session_module
from calling_agent.agent_spec import AgentDefinition
from calling_agent.config import settings
from calling_agent.main import app


class Transport:
    def __init__(self):
        self.events = []
        self.audio = []
        self.closed = False

    async def send_event(self, event):
        self.events.append(event)

    async def send_audio(self, data):
        self.audio.append(data)

    async def clear(self):
        self.events.append({"type": "clear"})

    async def close(self):
        self.closed = True

    async def recv_audio(self):
        await asyncio.Future()
        yield ""


class Upstream:
    close_code = 1000
    close_reason = ""

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        # A real send can suspend. This catches the guard cancelling itself.
        await asyncio.sleep(0)
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Future()


def test_public_experience_has_no_key_and_does_not_claim_live_readiness(monkeypatch):
    monkeypatch.setattr(settings, "assemblyai_api_key", "")
    body = TestClient(app).get("/experience").json()
    assert body["live_configured"] is False
    assert body["booking_storage"] == "memory"
    assert "assemblyai_api_key" not in body



def _agent_running(tool):
    """An AgentSession whose only tool is `tool`.

    The fake is what the session is CONSTRUCTED with, not a name patched onto
    the module: a session that ignored the definition it was handed would fail
    these tests instead of passing them by accident.
    """
    return session_module.AgentSession(
        Transport(),
        agent=AgentDefinition(
            name="test",
            build_prompt=lambda: "test prompt",
            greeting="hello",
            tools=[],
            run_tool=tool,
        ),
    )



async def test_missing_key_closes_transport(monkeypatch):
    monkeypatch.setattr(settings, "assemblyai_api_key", "")
    transport = Transport()
    await session_module.AgentSession(transport).run()
    assert transport.closed
    assert transport.events[0]["type"] == "error"


async def test_slow_tool_does_not_block_audio_or_interruption(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_tool(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return "Tool completed", False

    monkeypatch.setattr(settings, "allow_interruptions", True)
    upstream = Upstream()
    agent = _agent_running(slow_tool)
    transport = agent._transport
    worker = asyncio.create_task(agent._run_tools(upstream))
    try:
        await agent._handle_upstream(
            upstream, {"type": "tool.call", "name": "slow", "call_id": "slow-1"}
        )
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(
            agent._handle_upstream(upstream, {"type": "reply.audio", "data": "audio"}), 0.2
        )
        await asyncio.wait_for(
            agent._handle_upstream(upstream, {"type": "input.speech.started"}), 0.2
        )
        assert transport.audio == ["audio"]
        assert {"type": "clear"} in transport.events
        assert not upstream.sent
        release.set()
        await asyncio.wait_for(agent._tool_queue.join(), 1)
        assert upstream.sent[0]["result"] == "Tool completed"
        assert [
            event["status"] for event in transport.events if event["type"] == "tool.activity"
        ] == ["started", "completed"]
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_timeout_guard_can_await_send_without_cancelling_itself(monkeypatch):
    monkeypatch.setattr(session_module, "TOOL_RESULT_TIMEOUT", 0.01)
    upstream = Upstream()
    agent = session_module.AgentSession(Transport())
    agent._reply_active = True
    await agent._handle_tool_call(upstream, {"name": "restaurant_info", "call_id": "guard-1"})
    await asyncio.wait_for(agent._flush_guard, 1)
    assert upstream.sent[0]["call_id"] == "guard-1"


async def test_duplicate_tool_call_executes_side_effect_once():
    calls = []

    def action(*args, **kwargs):
        calls.append(args)
        return "Booked once", False

    agent = _agent_running(action)
    upstream = Upstream()
    message = {"name": "book_table", "call_id": "same-id", "arguments": {}}
    await agent._handle_tool_call(upstream, message)
    await agent._handle_tool_call(upstream, message)
    assert len(calls) == 1
    assert len(upstream.sent) == 2
    assert upstream.sent[0] == upstream.sent[1]


async def test_cancelled_pump_cleans_up_worker_and_guard():
    agent = session_module.AgentSession(Transport())
    upstream = Upstream()
    agent._flush_guard = asyncio.create_task(asyncio.sleep(20))
    guard = agent._flush_guard
    pump = asyncio.create_task(agent._pump(upstream))
    await asyncio.sleep(0)
    pump.cancel()
    await asyncio.gather(pump, return_exceptions=True)
    assert guard.cancelled()
    assert agent._flush_guard is None
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() in {"tools", "client->agent", "agent->client"}
    ]


def test_receipts_only_accompany_actual_bookings(monkeypatch):
    monkeypatch.setattr(restaurant, "BOOKINGS", restaurant.BookingStore())
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    rejected = restaurant.book_table({"name": "Alex", "party_size": 0})
    assert not hasattr(rejected, "receipt")
    result = restaurant.book_table(
        {"name": "Alex", "party_size": 4, "date": tomorrow, "time": "19:00"}
    )
    assert result.startswith("Booked:")
    assert result.receipt["name"] == "Alex"
    assert result.receipt["status"] == "confirmed"
    reference = result.receipt["reference"]
    cancelled = restaurant.cancel_booking({"reference": reference})
    assert cancelled.receipt["status"] == "cancelled"


def test_simultaneous_reservations_do_not_overbook(monkeypatch):
    store = restaurant.BookingStore()
    monkeypatch.setattr(restaurant, "BOOKINGS", store)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    barrier = threading.Barrier(8)

    def book(index):
        barrier.wait()
        return restaurant.book_table(
            {"name": "Guest " + str(index), "date": tomorrow, "time": "19:00", "party_size": 2}
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(book, range(8)))
    assert sum(result.startswith("Booked:") for result in results) == 6
    assert sum(booking.party_size for booking in store.bookings.values()) == 12


# --- AGENT_FACTORY runs off the event loop -------------------------------
#
# Module level so AGENT_FACTORY can address it by import path, the way a real
# deployment does. The gate is what the test opens; nothing in here reads a
# clock or a module constant, so the factory cannot agree with the code by
# accident about when it ran.
_FACTORY_ENTERED = threading.Event()
_FACTORY_GATE = threading.Event()


def _gated_factory(params):
    _FACTORY_ENTERED.set()
    if not _FACTORY_GATE.wait(2):
        raise RuntimeError("the gate never opened: the loop never got to open it")
    return AgentDefinition(
        name="gated",
        build_prompt=lambda: "prompt from the gated factory",
        greeting="hello",
        tools=[],
        run_tool=lambda name, args, call_id="": ("ran", False),
    )


async def test_a_slow_agent_factory_does_not_freeze_the_event_loop(monkeypatch):
    """The documented use of AGENT_FACTORY is a lookup, and a lookup blocks.

    A factory that resolves the agent from the caller's phone number reads a
    row, or calls an HTTP API. Run on the loop, that does not merely delay the
    connection asking for it: it stops the audio pump of every OTHER call in
    the process for the whole lookup, and the first connection pays
    `importlib.import_module`'s disk I/O on the same thread. `run_tool` has
    gone through `asyncio.to_thread` for exactly this reason.

    The assertion needs no clock. The factory blocks until the gate opens, and
    the gate is opened FROM THE LOOP after the factory has started: a factory
    running on the loop can therefore never see it open, times out, raises,
    and is served the default agent. So `agent.name == "gated"` is reachable
    only if the two ran at the same time.

    Mutation: `return _build_agent(params)` in place of the `to_thread` call
    kills this -- the build resolves to None (the gate cannot open while the
    loop is inside the factory) and nothing else in the suite notices.
    """
    _FACTORY_ENTERED.clear()
    _FACTORY_GATE.clear()
    monkeypatch.setattr(settings, "agent_factory", "test_experience:_gated_factory")
    build = asyncio.create_task(main_module._build_agent_async({"telefono": "5493511234567"}))
    try:
        assert await asyncio.to_thread(_FACTORY_ENTERED.wait, 2)
        # The loop is still serving while the factory blocks: this coroutine
        # runs to completion, and then opens the gate the factory is waiting on.
        assert (await main_module.healthz())["status"] == "ok"
        _FACTORY_GATE.set()
        agent = await asyncio.wait_for(build, 2)
    finally:
        _FACTORY_GATE.set()
        build.cancel()
    assert agent is not None, "the factory blocked the loop that had to release it"
    assert agent.name == "gated"


async def test_a_factory_that_fails_off_the_loop_still_serves_the_default_agent(monkeypatch):
    """Moving the call to a worker thread must not change what a failure means.

    A typo in a deployment variable is still not allowed to be the difference
    between a phone that answers and one that rings out, and the exception now
    crosses a thread boundary on its way to that decision.

    Mutation: letting `_build_agent_async` raise instead of returning None --
    for instance `await asyncio.to_thread(factory, params)` with no try -- kills
    this.
    """
    monkeypatch.setattr(settings, "agent_factory", "test_agent_spec:_broken_factory")
    assert await main_module._build_agent_async() is None


async def test_no_factory_configured_never_reaches_a_worker_thread(monkeypatch):
    """/experience builds an agent on every page load, and usually there is none.

    With AGENT_FACTORY unset there is nothing to run off the loop, so handing
    a thread back and forth to answer None would be a cost every page load
    pays for a feature nobody configured.

    Mutation: deleting the `if not settings.agent_factory.strip()` early return
    kills this -- the unset case would go to the pool to discover it has
    nothing to do.
    """

    def _no_deberia_ir_al_pool(*args, **kwargs):
        raise AssertionError("no factory is configured: there is nothing to run off the loop")

    monkeypatch.setattr(settings, "agent_factory", "")
    monkeypatch.setattr(asyncio, "to_thread", _no_deberia_ir_al_pool)
    assert await main_module._build_agent_async() is None
