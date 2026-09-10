# the-calling-agent

A real-time voice agent you talk to in a browser. Open a link on your phone,
tap once, and speak to it.

It currently answers the phone for a restaurant and takes table reservations:
it checks availability, books, looks bookings up, and cancels them.

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

## The restaurant agent

The agent answers as a host at a restaurant and takes reservations. It has five
tools, in `src/calling_agent/restaurant.py`:

| Tool | |
|---|---|
| `check_availability` | Is a slot free? Suggests nearby times when it is not. |
| `book_table` | Confirms a reservation and returns a spoken reference code. |
| `lookup_booking` | Reads a reservation back from its code. |
| `cancel_booking` | Cancels one, returning the seats to the pool. |
| `restaurant_info` | Hours, address, cuisine. |

Configure the venue without touching code:

```
RESTAURANT_NAME=The Copper Kettle
RESTAURANT_CUISINE=modern North Indian food
RESTAURANT_ADDRESS=
RESTAURANT_MAX_PARTY=12
```

Opening hours and covers per slot are in `restaurant.py` (`OPENING_HOURS`,
`SEATS_PER_SLOT`).

### Details that matter on a voice call

**Today's date is baked into the prompt** rather than exposed as a tool.
Callers say "tomorrow" and "this Friday" constantly, and a tool round-trip for
something that static would add an audible pause to nearly every booking. The
prompt is rebuilt per session so a long-running process never serves
yesterday's date.

**Reference codes avoid `0`, `O`, `1` and `I`**, and are returned spelled out
(`P 8 R S F`) so the agent reads them character by character instead of running
them together.

**The agent must call `check_availability` before promising anything.** The
prompt is explicit that confirming a booking the caller did not agree to is the
worst failure mode here.

### ⚠️ Bookings are not persisted

`BOOKINGS` is an in-memory store. That is fine for trying this out and wrong
for real use: on a serverless host each function instance has its own copy, so
a reservation taken by one instance is invisible to the next, and a redeploy
wipes everything.

Every tool goes through `BookingStore`, so pointing it at a real database is
the only change needed — nothing else has to move.

## Choosing a voice

The page has a **voice picker** under the call button; your choice is
remembered in the browser. It only applies to the next call, since changing
voice needs a new session.

| Voice | |
|---|---|
| `arjun` | **Multilingual** — Hindi / Hinglish, code-switches with English. The default. |
| `diego` | Multilingual — Latin American Spanish |
| `james` | English — conversational US male, the most natural of the English voices |
| `sophie` | English — clear UK female |
| `claire` | English — US female |
| `ivy` | English — US female. Lighter and more synthetic; the API's own example, and not a good default. |

These are the ids verified to work. AssemblyAI publishes **18 English and 16
multilingual** voices, so an id not listed here may still be valid — try it
with `/ws?voice=<id>`, which overrides `AGENT_VOICE` for a single call. `GET
/voices` returns the list the picker uses.

Multilingual voices code-switch automatically, and the system prompt tells the
agent to reply in whatever language it is addressed in — including mixing two
languages mid-sentence, the way people actually speak. A multilingual voice
paired with an English-only prompt would waste half of what you are paying for.

### If it sounds robotic

The voice model is only half of it. Written-sounding sentences read as
synthetic no matter who speaks them, so `SYSTEM_PROMPT` in
`src/calling_agent/agent_config.py` pushes for contractions, short turns,
varied sentence length, and natural openers. Edit that before concluding a
voice is bad.

## Interrupting the agent

You can cut in while it is talking and it stops immediately. Two things make
that work:

1. `interrupt_response: true` tells the API to abandon the turn it is
   generating.
2. On `input.speech.started` the relay tells the browser to drop every queued
   audio buffer.

There is a third part that is easy to miss. The API cannot recall bytes it has
already put on the wire, so roughly a second of audio for the cancelled turn
still arrives after you interrupt. Those chunks are **dropped** rather than
played, until the next `reply.started` marks a genuinely new turn. Without
that, the agent stops, then carries on talking over you for another second and
a half.

Set `AGENT_ALLOW_INTERRUPTIONS=false` to turn the whole behaviour off; that
disables it both upstream and in the browser.

### If it interrupts itself

On a laptop or a phone speaker, the agent's own voice can reach the mic and be
heard as you starting to talk, which cuts it off mid-sentence. `getUserMedia`
is requested with `echoCancellation` on, which handles most of it. If it still
happens:

- Use headphones. This removes the problem entirely.
- Raise `AGENT_VAD_THRESHOLD` toward `0.7` so quiet sound is not treated as
  speech.

## Making it feel faster

Most of the delay is upstream: AssemblyAI advertises **~1 s end-to-end** for
speech in to speech out, and the relay here adds well under a millisecond. So
tuning is mostly about not waiting longer than necessary to decide you have
stopped talking.

| Setting | Effect |
|---|---|
| `AGENT_MIN_SILENCE_MS` (default 320) | Silence before the agent decides your turn ended. **The main lever.** Lower feels snappier; too low clips you mid-sentence. |
| `AGENT_MAX_SILENCE_MS` (default 1500) | Hard cap before the turn is forced to end. |
| `AGENT_VAD_THRESHOLD` (default 0.5) | Raise in a noisy room so background noise is not heard as speech. |

If replies start cutting you off, raise `AGENT_MIN_SILENCE_MS` to 500-700. If
the agent feels sluggish, lower it toward 250.

The client also keeps its playback cushion at 20 ms — enough to absorb jitter
without adding audible delay. Raise it in `static/index.html` only if playback
stutters.

Because `turn_detection` is the newest part of the API payload, a rejected
field there would otherwise kill the whole session. The relay retries once
without it, so a bad setting costs you the tuning rather than the call.

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
