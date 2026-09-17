"""Regressions for call latency, cleanup, receipts and concurrent reservations."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from calling_agent import agent_tools
from calling_agent import session as session_module
from calling_agent.agent_spec import AgentDefinition
from calling_agent.config import settings
from calling_agent.main import app
from tests.conftest import committed, future_slot


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
    assert body["booking_storage"] == "postgres"
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


def test_structured_data_only_accompanies_actual_bookings(business):
    """A refusal carries no booking data, so no UI can render one from it.

    The in-memory book these used to check was replaced (PRD §17); the
    behaviour they pin -- evidence comes from the write, never from a
    hopeful read of the sentence -- is the same and still matters.
    """
    at = future_slot(business)

    rejected = agent_tools.hold(business, {"date": at.date().isoformat(), "units": 0})
    assert rejected.data.get("ok") is not True
    assert "hold_id" not in rejected.data

    held = agent_tools.hold(
        business,
        {"date": at.date().isoformat(), "time": at.strftime("%H:%M"), "units": 4},
    )
    assert held.data["ok"]

    booked = agent_tools.confirm(
        business, {"hold_id": held.data["hold_id"], "name": "Alex", "phone": "+910000000123"}
    )
    assert booked.startswith("Booked:")
    assert booked.data["name"] == "Alex"
    assert booked.data["status"] == "confirmed"

    cancelled = agent_tools.cancel_booking(
        business, {"reference": booked.data["reference"], "name": "Alex"}
    )
    assert cancelled.data["status"] == "cancelled"


def test_simultaneous_reservations_do_not_overbook(business):
    """Eight callers, room for six. The counter is the assertion."""
    at = future_slot(business)
    barrier = threading.Barrier(8)

    def book(index):
        barrier.wait()
        held = agent_tools.hold(
            business,
            {"date": at.date().isoformat(), "time": at.strftime("%H:%M"), "units": 2},
        )
        if not held.data.get("ok"):
            return held
        return agent_tools.confirm(
            business, {"hold_id": held.data["hold_id"], "name": f"Guest {index}"}
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(book, range(8)))

    assert sum(result.startswith("Booked:") for result in results) == 6
    assert committed(business.id, at) == 12
