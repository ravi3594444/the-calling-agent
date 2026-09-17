"""Twilio Media Streams as an AudioTransport.

Everything above this file is unchanged: the relay, the tool worker, barge-in
and reconnect handling all treat a phone call exactly like a browser tab. The
only differences a telephony call brings are the encoding and the wire framing,
and both are contained here.

WHY THERE IS NO TRANSCODING
Twilio sends and accepts G.711 mu-law at 8 kHz, base64 encoded. The Voice Agent
API speaks the same thing as `audio/pcmu`. The bytes are compatible, so frames
are forwarded untouched -- one encode at the caller's handset, one decode at
the far end, nothing in between. Leaving the output encoding at the default
24 kHz PCM is the classic failure here: the agent talks happily and the caller
hears silence, because Twilio cannot play it.

Wire format (Twilio -> us):
    {"event": "connected"}
    {"event": "start", "start": {"streamSid": ..., "customParameters": {...}}}
    {"event": "media", "media": {"payload": "<base64 mu-law>"}}
    {"event": "stop"}

Wire format (us -> Twilio):
    {"event": "media", "streamSid": ..., "media": {"payload": "<base64>"}}
    {"event": "clear", "streamSid": ...}
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..protocol import ENCODING_PCMU, PCMU_SAMPLE_RATE
from .base import AudioTransport

log = logging.getLogger(__name__)


class TwilioTransport(AudioTransport):
    encoding = ENCODING_PCMU
    sample_rate = PCMU_SAMPLE_RATE

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._stream_sid: str | None = None
        self.call_sid: str | None = None
        self.parameters: dict[str, str] = {}

    async def recv_audio(self) -> AsyncIterator[str]:
        """Yield the caller's audio, and learn the stream id on the way.

        The stream id arrives in the `start` event and is required on every
        frame sent back. Until it lands there is nowhere to send audio, which
        is why the greeting is only ever heard after `start`.
        """
        try:
            while True:
                message = json.loads(await self._ws.receive_text())
                event = message.get("event")

                if event == "media":
                    payload = message.get("media", {}).get("payload")
                    if payload:
                        yield payload
                    continue

                if event == "start":
                    start = message.get("start", {})
                    self._stream_sid = start.get("streamSid")
                    self.call_sid = start.get("callSid")
                    self.parameters = dict(start.get("customParameters") or {})
                    log.info("twilio stream %s started", self._stream_sid)
                    continue

                if event == "stop":
                    log.info("twilio stream %s stopped", self._stream_sid)
                    return

        except WebSocketDisconnect:
            log.info("twilio disconnected")
        except (ValueError, RuntimeError, KeyError) as exc:
            log.info("twilio stream ended: %s", exc)

    async def _send(self, payload: dict) -> None:
        if self._ws.client_state is not WebSocketState.CONNECTED:
            return
        try:
            await self._ws.send_text(json.dumps(payload))
        except (WebSocketDisconnect, RuntimeError) as exc:
            log.debug("dropping frame, twilio gone: %s", exc)

    async def send_audio(self, chunk_b64: str) -> None:
        if not self._stream_sid:
            return
        await self._send(
            {
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": chunk_b64},
            }
        )

    async def clear(self) -> None:
        """Barge-in: drop what Twilio has buffered so the agent stops mid-word.

        Without this the caller interrupts, the agent stops generating, and
        several seconds of already-sent audio keep playing over them.
        """
        if not self._stream_sid:
            return
        await self._send({"event": "clear", "streamSid": self._stream_sid})

    async def send_event(self, event: dict) -> None:
        """A phone has nowhere to show a transcript. Deliberately does nothing."""
        return

    async def close(self) -> None:
        if self._ws.client_state is WebSocketState.CONNECTED:
            try:
                await self._ws.close()
            except RuntimeError:
                pass
