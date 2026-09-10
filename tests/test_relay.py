"""End-to-end relay tests against a mock Voice Agent API.

Verifies the four behaviours that matter: session config is sent first, audio
forwards both ways, barge-in clears playback, and tool calls round-trip.
"""

import asyncio
import base64
import json
import threading

import pytest
import websockets
from fastapi.testclient import TestClient


class MockUpstream:
    """Minimal stand-in for wss://agents.assemblyai.com."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.auth_header: str | None = None
        self._ready = threading.Event()
        self._script: list[dict] = []
        self._reject: tuple[int, str] | None = None
        self.port: int | None = None

    def will_send(self, *messages: dict) -> None:
        self._script = list(messages)

    def will_reject(self, code: int = 1008, reason: str = "invalid tool definition") -> None:
        """Close the connection the way the API refuses a bad session."""
        self._reject = (code, reason)

    async def _handler(self, conn) -> None:
        self.auth_header = conn.request.headers.get("Authorization")
        async for raw in conn:
            msg = json.loads(raw)
            self.received.append(msg)
            # Reply to whichever opening message arrives -- a fresh session
            # sends session.update, a reconnect sends session.resume.
            if msg.get("type") in ("session.update", "session.resume"):
                if self._reject is not None:
                    await conn.close(code=self._reject[0], reason=self._reject[1])
                    return
                for out in self._script:
                    await conn.send(json.dumps(out))

    def _serve(self) -> None:
        async def main() -> None:
            async with websockets.serve(self._handler, "127.0.0.1", 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                self._ready.set()
                await asyncio.Future()

        asyncio.run(main())

    def start(self) -> str:
        threading.Thread(target=self._serve, daemon=True).start()
        assert self._ready.wait(5), "mock upstream failed to start"
        return f"ws://127.0.0.1:{self.port}"


@pytest.fixture
def upstream(monkeypatch):
    mock = MockUpstream()
    url = mock.start()
    from calling_agent.config import settings

    monkeypatch.setattr(settings, "assemblyai_agent_ws_url", url)
    monkeypatch.setattr(settings, "assemblyai_api_key", "test-key-123")
    monkeypatch.setattr(settings, "agent_id", "")
    return mock


def _client() -> TestClient:
    from calling_agent.main import app

    return TestClient(app)


def _drain(ws, wanted: str, limit: int = 25) -> dict | None:
    """Pull frames until one has type `wanted`."""
    for _ in range(limit):
        msg = ws.receive_json()
        if msg.get("type") == wanted:
            return msg
    return None


def test_healthz_reports_config_without_leaking_key(upstream):
    resp = _client().get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["api_key_configured"] is True
    assert "test-key-123" not in resp.text


def test_index_serves_client_page(upstream):
    resp = _client().get("/")
    assert resp.status_code == 200
    # AudioWorklet, not MediaRecorder, and a real tap-to-start button.
    assert "pcm-worklet.js" in resp.text
    assert "MediaRecorder" not in resp.text
    assert 'id="call"' in resp.text


def test_sends_session_config_with_bearer_auth(upstream):
    upstream.will_send({"type": "session.ready", "session_id": "s-1"})
    with _client().websocket_connect("/ws") as ws:
        assert _drain(ws, "event")["event"]["type"] == "session.ready"

    assert upstream.auth_header == "Bearer test-key-123"
    first = upstream.received[0]
    assert first["type"] == "session.update"
    # Input and output encodings must match or playback needs transcoding.
    assert first["session"]["input"]["format"]["encoding"] == "audio/pcm"
    assert first["session"]["output"]["format"]["encoding"] == "audio/pcm"


def test_forwards_audio_in_both_directions(upstream):
    reply = base64.b64encode(b"\x01\x02" * 32).decode()
    upstream.will_send({"type": "reply.audio", "audio": reply})

    mic = base64.b64encode(b"\x10\x20" * 32).decode()
    with _client().websocket_connect("/ws") as ws:
        ws.send_json({"type": "audio", "data": mic})
        got = _drain(ws, "audio")
        assert got["data"] == reply

    audio_up = [m for m in upstream.received if m["type"] == "input.audio"]
    assert audio_up and audio_up[0]["audio"] == mic


def test_barge_in_clears_playback(upstream):
    upstream.will_send({"type": "input.speech.started"})
    with _client().websocket_connect("/ws") as ws:
        assert _drain(ws, "clear") is not None


def test_tool_call_round_trips(upstream):
    upstream.will_send(
        {
            "type": "tool.call",
            "name": "get_current_time",
            "call_id": "call-7",
            "arguments": {},
        }
    )
    with _client().websocket_connect("/ws") as ws:
        _drain(ws, "event")

    results = [m for m in upstream.received if m["type"] == "tool.result"]
    assert results, f"no tool.result sent; got {[m['type'] for m in upstream.received]}"
    assert results[0]["call_id"] == "call-7"
    assert "UTC" in results[0]["result"]
    assert results[0]["is_error"] is False


def test_unknown_tool_reports_error_rather_than_crashing(upstream):
    upstream.will_send(
        {"type": "tool.call", "name": "no_such_tool", "call_id": "c-9", "arguments": {}}
    )
    with _client().websocket_connect("/ws") as ws:
        _drain(ws, "event")

    results = [m for m in upstream.received if m["type"] == "tool.result"]
    assert results and "No tool named" in results[0]["result"]
    # Flagged rather than disguised as a successful result.
    assert results[0]["is_error"] is True


def test_missing_api_key_tells_the_user(upstream, monkeypatch):
    from calling_agent.config import settings

    monkeypatch.setattr(settings, "assemblyai_api_key", "")
    with _client().websocket_connect("/ws") as ws:
        msg = _drain(ws, "event")
        assert "ASSEMBLYAI_API_KEY" in msg["event"]["message"]


def test_stored_agent_mode_sends_only_agent_id(upstream, monkeypatch):
    from calling_agent.config import settings

    monkeypatch.setattr(settings, "agent_id", "agent-abc")
    upstream.will_send({"type": "session.ready", "session_id": "s-2"})
    with _client().websocket_connect("/ws") as ws:
        _drain(ws, "event")

    session = upstream.received[0]["session"]
    assert session == {"agent_id": "agent-abc"}


def test_resume_rejoins_an_existing_session(upstream):
    """A reconnect must resume, not start a fresh (and separately billed) session."""
    upstream.will_send({"type": "session.ready", "session_id": "s-resumed"})
    with _client().websocket_connect("/ws?resume=s-earlier") as ws:
        _drain(ws, "event")

    first = upstream.received[0]
    assert first["type"] == "session.resume"
    assert first["session_id"] == "s-earlier"
    # No session.update should follow; resume restores the stored config.
    assert not any(m["type"] == "session.update" for m in upstream.received)


def test_session_ready_id_reaches_the_browser(upstream):
    """The client stores this id; without it a reconnect cannot resume."""
    upstream.will_send({"type": "session.ready", "session_id": "s-42"})
    with _client().websocket_connect("/ws") as ws:
        event = _drain(ws, "event")["event"]
        assert event["type"] == "session.ready"
        assert event["session_id"] == "s-42"


def test_session_payload_matches_the_api_schema(upstream):
    """Locks the field names the API actually requires.

    Every one of these was wrong in the first implementation and the only
    symptom was the whole session being closed with 1008, which names no field.
    """
    upstream.will_send({"type": "session.ready", "session_id": "s-1"})
    with _client().websocket_connect("/ws") as ws:
        _drain(ws, "event")

    session = upstream.received[0]["session"]

    # Audio blocks carry an explicit type alongside the format.
    assert session["input"]["type"] == "audio"
    assert session["output"]["type"] == "audio"
    assert session["output"]["voice"]

    tool = session["tools"][0]
    assert tool["type"] == "function"
    assert "parameters" in tool, "the API wants 'parameters', not 'input_schema'"
    assert "input_schema" not in tool
    assert tool["parameters"]["type"] == "object"


def test_rejected_session_is_explained_to_the_client(upstream):
    """A 1008 must reach the page, not just the platform log."""
    upstream.will_reject(1008, "unknown field 'input_schema' in tools[0]")
    with _client().websocket_connect("/ws") as ws:
        event = _drain(ws, "event")["event"]

    assert event["type"] == "error"
    assert event["code"] == 1008
    # The API's own reason is the useful part; it must survive to the client.
    assert "input_schema" in event["message"]
    assert "configuration problem" in event["message"]


class _FakeUpstream:
    """Only the close attributes _report_close reads."""

    def __init__(self, code, reason=""):
        self.close_code = code
        self.close_reason = reason


class _RecordingTransport:
    encoding = "audio/pcm"
    sample_rate = 24_000

    def __init__(self):
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)


@pytest.mark.parametrize("code", [None, 1000, 1001])
async def test_clean_close_is_not_reported_as_an_error(code):
    """A normal hang-up, or a server going away, must not paint an error."""
    from calling_agent.session import AgentSession

    transport = _RecordingTransport()
    await AgentSession(transport)._report_close(_FakeUpstream(code))
    assert transport.events == []


async def test_unexpected_close_code_is_reported():
    from calling_agent.session import AgentSession

    transport = _RecordingTransport()
    await AgentSession(transport)._report_close(_FakeUpstream(1011, "internal error"))

    assert len(transport.events) == 1
    assert transport.events[0]["code"] == 1011
    assert "internal error" in transport.events[0]["message"]
