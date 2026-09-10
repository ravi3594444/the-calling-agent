"""Browser transport: raw PCM over our own WebSocket.

Wire format, both directions:
    {"type": "audio", "data": "<base64 PCM16LE 24kHz mono>"}
    {"type": "clear"}                     # server -> client, barge-in
    {"type": "event", "event": {...}}     # server -> client, transcripts etc.
"""

import logging
from collections.abc import AsyncIterator

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..protocol import ENCODING_PCM, PCM_SAMPLE_RATE
from .base import AudioTransport

log = logging.getLogger(__name__)


class BrowserTransport(AudioTransport):
    encoding = ENCODING_PCM
    sample_rate = PCM_SAMPLE_RATE

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def recv_audio(self) -> AsyncIterator[str]:
        try:
            while True:
                msg = await self._ws.receive_json()
                if msg.get("type") == "audio" and (data := msg.get("data")):
                    yield data
        except WebSocketDisconnect:
            log.info("browser disconnected")
        except (ValueError, RuntimeError, KeyError) as exc:
            # Malformed frame or socket torn down underneath us; end the stream
            # rather than killing the whole session with a traceback.
            log.info("browser stream ended: %s", exc)

    async def _send(self, payload: dict) -> None:
        if self._ws.client_state is not WebSocketState.CONNECTED:
            return
        try:
            await self._ws.send_json(payload)
        except (WebSocketDisconnect, RuntimeError) as exc:
            log.debug("dropping frame, browser gone: %s", exc)

    async def send_audio(self, chunk_b64: str) -> None:
        await self._send({"type": "audio", "data": chunk_b64})

    async def clear(self) -> None:
        await self._send({"type": "clear"})

    async def send_event(self, event: dict) -> None:
        await self._send({"type": "event", "event": event})

    async def close(self) -> None:
        if self._ws.client_state is WebSocketState.CONNECTED:
            try:
                await self._ws.close()
            except RuntimeError:
                pass
