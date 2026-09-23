"""Browser transport: audio over our own WebSocket.

Wire format, both directions:
    {"type": "audio", "data": "<base64 audio in this session's encoding>"}
    {"type": "clear"}                     # server -> client, barge-in
    {"type": "event", "event": {...}}     # server -> client, transcripts etc.

TWO ENCODINGS. The default is 24 kHz linear PCM, which is what a browser
does best. `pcmu` is 8 kHz G.711 mu-law -- the same bytes a phone call
carries -- so the browser client can be used to judge how the agent sounds
and how turn-taking feels ON A PHONE, without a phone number.

That matters because PRD §16 gates the whole provider choice on whether
barge-in feels natural at telephony latency and fidelity. Judging it on
24 kHz browser audio answers a question nobody asked.
"""

import logging
from collections.abc import AsyncIterator

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..protocol import ENCODING_PCM, ENCODING_PCMU, PCM_SAMPLE_RATE, PCMU_SAMPLE_RATE
from .base import AudioTransport

log = logging.getLogger(__name__)

#: Short name on the URL -> (what the API is told, sample rate).
ENCODINGS = {
    "pcm": (ENCODING_PCM, PCM_SAMPLE_RATE),
    "pcmu": (ENCODING_PCMU, PCMU_SAMPLE_RATE),
}


class BrowserTransport(AudioTransport):
    def __init__(self, ws: WebSocket, encoding: str = "pcm") -> None:
        self._ws = ws
        # An unknown name falls back to pcm rather than raising: a typo in a
        # URL should cost fidelity, never the call.
        self.encoding, self.sample_rate = ENCODINGS.get(encoding, ENCODINGS["pcm"])

    async def recv_audio(self) -> AsyncIterator[str]:
        try:
            while True:
                try:
                    msg = await self._ws.receive_json()
                except (ValueError, KeyError, TypeError) as exc:
                    # One malformed frame (bad JSON, a binary frame) costs that
                    # frame. Ending the stream here hung up the whole call.
                    log.debug("skipping malformed frame from browser: %s", exc)
                    continue
                # Valid JSON is not necessarily an object; `[1]` raised
                # AttributeError here, which nothing caught.
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "audio" and (data := msg.get("data")):
                    yield data
        except WebSocketDisconnect:
            log.info("browser disconnected")
        except RuntimeError as exc:
            # Socket torn down underneath us; end the stream rather than
            # killing the whole session with a traceback.
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
