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

## Reconnect and resume

A dropped socket is routine, not exceptional: any serverless host closes the
connection when its function hits the duration limit. The recovery path:

1. The client stores `session_id` from `session.ready`.
2. On an unexpected close it retries with backoff — 0.5s, 1s, 2s, 4s, 8s — up
   to five attempts, reconnecting to `/ws?resume=<session_id>`.
3. The server sees `resume` and sends `session.resume` rather than
   `session.update`, rejoining the session the API holds open for ~30 seconds.
4. Queued playback is dropped on disconnect so no stale fragment plays after
   the gap.

The microphone stream, `AudioContext`, and worklet are **not** torn down during
a reconnect; only the WebSocket is rebuilt. Re-acquiring the mic would prompt
the user again and lose the audio graph.

The client holds the session id because on a serverless platform the instance
that started the call is not the one handling the reconnect — there is no
server-side memory to look it up in.

## Known gaps

- **No authentication on `/ws`.** Anyone who can reach the host can open a
  session and spend your AssemblyAI credit. Add a token check before exposing
  the URL publicly.
- **No concurrency limit.** Each browser tab is one billed upstream session.
- **A resumed session is not verified as still valid.** If the ~30-second
  window has passed, the API's response to `session.resume` decides what
  happens; the client does not fall back to starting a fresh session.
