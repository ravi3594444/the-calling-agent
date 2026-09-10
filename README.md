# the-calling-agent

A real-time voice agent you talk to in a browser. Open a link on your phone,
tap once, and hold a spoken conversation with an AI.

Built on the [AssemblyAI Voice Agent API][vaapi], which handles speech-to-text,
LLM routing, and text-to-speech over a single WebSocket. This repo is the thin
server that bridges a browser's microphone to it, plus the infrastructure to
deploy that server on Google Cloud behind a DuckDNS hostname.

```
Phone browser  ──mic PCM16──▶  this server  ──▶  AssemblyAI Voice Agent API
     ▲                                                      │
     └────────────────── agent speech ◀────────────────────┘
```

**Only one secret is needed: an AssemblyAI API key.** No telephony provider, no
phone number, no identity verification.

[vaapi]: https://www.assemblyai.com/docs/voice-agents/voice-agent-api

## Quick start

```bash
make install                      # creates .venv, installs deps
cp .env.example .env              # then put your ASSEMBLYAI_API_KEY in it
make dev                          # http://localhost:8080
```

Open <http://localhost:8080> and tap **Start call**.

`localhost` is the one origin browsers exempt from the HTTPS requirement for
microphone access. Anywhere else you need a real certificate — see
[docs/DEPLOY.md](docs/DEPLOY.md).

## How it works

The server does **no audio processing**. Frames arrive from the browser already
base64-encoded in the format the Voice Agent API expects, so both directions
are a straight forward. Turn detection, interruption handling, and speech
synthesis all happen upstream.

| Concern | Where it lives |
|---|---|
| Agent prompt, voice, greeting, tools | `src/calling_agent/agent_config.py` |
| Relay + tool dispatch | `src/calling_agent/session.py` |
| Protocol message names | `src/calling_agent/protocol.py` |
| Audio transport interface | `src/calling_agent/transport/` |
| Browser client | `static/index.html`, `static/pcm-worklet.js` |

### Audio formats

| Path | Encoding | Rate |
|---|---|---|
| Browser (today) | `audio/pcm` — PCM16 LE mono | 24 kHz |
| Telephony (later) | `audio/pcmu` — G.711 µ-law | 8 kHz |

`AudioTransport` in `transport/base.py` exists so a telephony transport can be
added later without touching the relay. µ-law is byte-compatible with Telnyx and
Twilio media streams, so that path needs no transcoding either.

### Reconnect and resume

If the socket drops mid-call, the client reconnects with exponential backoff
(up to five attempts) and passes `?resume=<session_id>`, which makes the server
send `session.resume` instead of starting a fresh session. The audio graph is
kept alive across the reconnect — only the WebSocket is rebuilt.

The browser holds the session id rather than the server because on a serverless
platform the instance that started the call is not the one handling the
reconnect.

### Two details worth knowing

**Capture uses an AudioWorklet, not `MediaRecorder`.** `MediaRecorder` emits
chunked WebM/Opus containers, which add container overhead and chunk latency.
The API wants raw PCM16, so frames are taken straight off the audio graph.

**The mic cannot open on page load.** iOS Safari and desktop autoplay policy
both require a user gesture before an `AudioContext` will start or a permission
prompt will appear. Hence the explicit Start button.

## Configuration

All settings come from the environment or `.env` — see
[`.env.example`](.env.example) for the full list.

| Variable | Notes |
|---|---|
| `ASSEMBLYAI_API_KEY` | Required. |
| `ASSEMBLYAI_AGENT_WS_URL` | Docs list `/v1/ws`; the official Twilio example uses `/v1/realtime`. If you get a 404 on connect, try the other. |
| `AGENT_VOICE`, `AGENT_GREETING` | Inline agent config. |
| `AGENT_ID` | Bind to a stored agent instead. Mutually exclusive with inline config. |
| `PUBLIC_HOSTNAME` | Your DuckDNS hostname. Deployment only. |

To change what the agent says or add tools, edit `agent_config.py` — the
`get_current_time` tool is there as a worked example of the JSON Schema shape
and the `tool.call` → `tool.result` round trip.

## Cost

The Voice Agent API bills **$4.50/hr** of connected time, roughly **$0.075 per
minute** — billed while the WebSocket is open, not only while someone is
talking. End calls when you are done testing.

## Commands

```
make help          list targets
make dev           run with auto-reload
make test          run the test suite
make lint          ruff
make deploy-up     app + Caddy TLS (on the server)
```

## Deploying

Two supported paths. The application code is identical for both — only the
process boundary differs.

### Vercel — fastest

```bash
vercel --prod
vercel env add ASSEMBLYAI_API_KEY production
```

HTTPS on a real domain with no certificate work, no DuckDNS, and no VM. The
catch: a WebSocket lives inside a Function and inherits its duration limit, so
calls are cut every **5 minutes on Hobby** (up to 30 on Pro). The client handles
this by reconnecting with `session.resume`, so the caller hears a gap rather
than a dropped call. See [docs/VERCEL.md](docs/VERCEL.md).

### Google Cloud VM — no duration cap

`./infra/gcp-setup.sh` provisions an `e2-small` with a reserved static IP,
DuckDNS points a hostname at it, and Caddy obtains a Let's Encrypt certificate
automatically. Flat ~$20/mo, no call-length limit. Prefer this if you are adding
telephony later, since a media stream held open for a whole call fights a
platform that closes connections on a timer. See
[docs/DEPLOY.md](docs/DEPLOY.md).
