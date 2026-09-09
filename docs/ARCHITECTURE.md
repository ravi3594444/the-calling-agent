# Architecture

## The shape of the system

```
┌──────────────┐   WebSocket    ┌──────────────┐   WebSocket   ┌────────────────┐
│    Browser   │  PCM16 24kHz   │  This server │  input.audio  │  AssemblyAI    │
│              │ ─────────────▶ │              │ ────────────▶ │  Voice Agent   │
│ AudioWorklet │                │  AgentSession│               │  API           │
│   capture    │ ◀───────────── │    (relay)   │ ◀──────────── │ (STT+LLM+TTS)  │
│   playback   │  audio / clear │              │  reply.audio  │                │
└──────────────┘                └──────────────┘               └────────────────┘
```

The important property: **the server is not a pipeline, it is a relay.** There
is no STT step, no LLM call, and no TTS step in this codebase. The Voice Agent
API does all three behind one connection, which is why `session.py` is short and
why there are no provider abstractions for models.

## Modules

| File | Responsibility |
|---|---|
| `main.py` | FastAPI app: static page, `/healthz`, `/ws` |
| `session.py` | Opens the upstream socket, pumps both directions, dispatches tools |
| `agent_config.py` | Prompt, voice, greeting, tool definitions and implementations |
| `protocol.py` | Message-type constants and audio encodings |
| `transport/base.py` | `AudioTransport` interface |
| `transport/browser.py` | Browser WebSocket implementation |
| `config.py` | Environment-backed settings |

## Why `AudioTransport` exists

It is the one abstraction that earns its place. A telephony transport differs
from the browser only in its audio encoding and wire framing:

| | Browser | Telnyx / Twilio |
|---|---|---|
| Encoding | `audio/pcm` 24 kHz | `audio/pcmu` 8 kHz |
| Framing | our own JSON | provider's `media` events |
| Transcoding | none | none — µ-law is byte-compatible |
| Relay logic | identical | identical |

`build_session_update(encoding)` takes the transport's encoding and pins both
input and output format to it. Input and output must match: if they differ, the
agent's reply comes back in a format the transport cannot play.

## Concurrency

`AgentSession._pump` runs two tasks — client→agent and agent→client — and stops
as soon as either finishes, cancelling the other. A browser disconnect ends
`recv_audio()`, which ends the session; an upstream close ends the downstream
iterator with the same result. Neither direction can outlive the other.

## Barge-in

When the user interrupts, the API emits `input.speech.started`. The relay turns
that into `transport.clear()`, and the browser stops every scheduled
`AudioBufferSourceNode` and resets its playback cursor. Without this the agent
keeps speaking from already-buffered audio while the user talks over it.

Playback scheduling keeps a `nextPlayAt` cursor so chunks play back-to-back. If
the cursor falls behind `currentTime` after a network stall, it restarts at now
plus a 40 ms cushion rather than dumping queued audio at once.

## Tool calls

`tool.call` → look up the name in `TOOL_IMPLEMENTATIONS` → run it in a thread
(so a slow tool cannot stall the audio pump) → reply with `tool.result`. Unknown
names and exceptions return an error *string* to the agent rather than raising,
so a broken tool degrades the conversation instead of dropping the call.

## Known gaps

- **`session.resume` is unimplemented.** The API offers a 30-second reconnect
  window; `protocol.py` names the message but `session.py` does not yet use it.
  A dropped upstream currently ends the call.
- **No authentication on `/ws`.** Anyone who can reach the host can open a
  session and spend your AssemblyAI credit. Add a token check before exposing
  the URL publicly.
- **No concurrency limit.** Each browser tab is one billed upstream session.
