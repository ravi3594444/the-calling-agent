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
        self.port: int | None = None

    def will_send(self, *messages: dict) -> None:
        self._script = list(messages)

    async def _handler(self, conn) -> None:
        self.auth_header = conn.request.headers.get("Authorization")
        async for raw in conn:
            msg = json.loads(raw)
            self.received.append(msg)
            # Reply to the initial config with the scripted server events.
            if msg.get("type") == "session.update":
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
            "tool_call_id": "call-7",
            "arguments": {},
        }
    )
    with _client().websocket_connect("/ws") as ws:
        _drain(ws, "event")

    results = [m for m in upstream.received if m["type"] == "tool.result"]
    assert results, f"no tool.result sent; got {[m['type'] for m in upstream.received]}"
    assert results[0]["tool_call_id"] == "call-7"
    assert "UTC" in results[0]["result"]


def test_unknown_tool_reports_error_rather_than_crashing(upstream):
    upstream.will_send(
        {"type": "tool.call", "name": "no_such_tool", "tool_call_id": "c-9", "arguments": {}}
    )
    with _client().websocket_connect("/ws") as ws:
        _drain(ws, "event")

    results = [m for m in upstream.received if m["type"] == "tool.result"]
    assert results and "no tool named" in results[0]["result"]


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
