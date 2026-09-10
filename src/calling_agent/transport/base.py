"""Audio transport abstraction.

The relay core does not care where audio comes from. Today the only transport
is the browser; a telephony transport (Telnyx / Twilio media streams) is a
second implementation of this interface, differing only in `encoding` and in
how frames are framed on the wire. Everything between here and the AssemblyAI
Voice Agent API stays identical.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class AudioTransport(ABC):
    #: AssemblyAI audio encoding this transport speaks (see protocol.py).
    encoding: str
    #: Sample rate in Hz, for logging and diagnostics.
    sample_rate: int

    @abstractmethod
    def recv_audio(self) -> AsyncIterator[str]:
        """Yield base64-encoded audio chunks from the caller.

        Should return cleanly when the far end disconnects.
        """

    @abstractmethod
    async def send_audio(self, chunk_b64: str) -> None:
        """Play a base64-encoded audio chunk to the caller."""

    @abstractmethod
    async def clear(self) -> None:
        """Drop any audio still queued for playback.

        Called on barge-in so the agent stops mid-sentence the moment the
        user starts speaking.
        """

    @abstractmethod
    async def send_event(self, event: dict) -> None:
        """Forward a non-audio event (transcripts, state) to the client.

        Transports with nowhere to put this may no-op.
        """

    @abstractmethod
    async def close(self) -> None:
        """Tear down the transport."""
