# Deploying to Vercel

Vercel is the fastest way to get this on your phone: it provides HTTPS on a
real domain, so there is no certificate, no DuckDNS, and no VM to run.

Vercel Functions gained WebSocket support in public beta (June 2026), and
FastAPI WebSockets are on the supported list, which is what makes this possible
at all.

## The one constraint that matters

**A WebSocket on Vercel lives inside a Function and inherits its duration
limit.** When the limit is reached, the connection closes.

| Plan | Max duration | Effect on a call |
|---|---|---|
| Hobby | 300 s | Cut every 5 minutes |
| Pro / Enterprise | up to 1800 s (beta) | Cut every 30 minutes |

`vercel.json` sets `maxDuration: 300`, the Hobby ceiling. On Pro you can raise
it; a value above your plan's limit does not apply.

This is why the client implements **reconnect with backoff plus
`session.resume`**. The AssemblyAI session survives ~30 seconds after the socket
drops, so the browser reconnects with `?resume=<session_id>` and rejoins the
same session rather than starting a new one. The caller hears a brief gap, not a
dropped call — and you are not billed for a second session.

Resuming is the client's job because on a serverless platform the instance that
started the call is not the one handling the reconnect. There is no server-side
memory to look the session up in, so the browser holds the id.

## Deploy

```bash
npm i -g vercel
vercel login
vercel                 # preview deploy
vercel --prod          # production
```

Set the API key as an environment variable — do **not** commit it:

```bash
vercel env add ASSEMBLYAI_API_KEY production
vercel env add ASSEMBLYAI_API_KEY preview
```

Or add it under *Project → Settings → Environment Variables* in the dashboard.

Optional variables, same place: `AGENT_VOICE`, `AGENT_GREETING`, `AGENT_ID`,
`ASSEMBLYAI_AGENT_WS_URL`, `LOG_LEVEL`. Anything unset falls back to the
defaults in `config.py`.

`PUBLIC_HOSTNAME` is not needed on Vercel — it exists only for Caddy on the VM.

## Verify

```
https://<your-project>.vercel.app/healthz
```

Expect `{"status":"ok","api_key_configured":true,...}`. If `api_key_configured`
is `false`, the environment variable did not reach the deployment — check you
set it for the right environment and redeployed.

Then open the root URL on your phone and tap **Start call**.

## How it fits together

| | |
|---|---|
| `api/index.py` | Exports the ASGI app. Vercel serves it; no uvicorn. |
| `requirements.txt` | What Vercel installs. Mirrors `pyproject.toml`. |
| `vercel.json` | Duration, memory, and a catch-all rewrite to the function. |

The application code is unchanged between local, VM, and Vercel — only the
process boundary differs. `uvicorn` is deliberately absent from
`requirements.txt` because Vercel provides the server.

A test in `tests/test_packaging.py` fails if `requirements.txt` drifts from
`pyproject.toml`, since that mismatch otherwise shows up only as a failed
deploy.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `api_key_configured: false` | Variable not set for that environment, or set after the last deploy. Redeploy. |
| 404 on `/static/pcm-worklet.js` | `includeFiles` missing from `vercel.json`; static files did not ship with the function. |
| Call drops at exactly 5 minutes with no recovery | Expected close, but reconnect failed. Check the browser console for the `?resume=` request. |
| "Connection lost. Tap to start again." | Five reconnects failed. Usually the function is erroring — check `vercel logs`. |
| Reconnect starts a *new* session | The client lost `sessionId`. Confirm `session.ready` carries `session_id`. |

## Cost

Fluid compute bills on function duration, and a voice call is the awkward shape
for that: the connection is held open for the full wall-clock while using
almost no CPU. A flat-rate VM does not care how long a connection stays open;
duration billing does.

Check your actual usage in the Vercel dashboard after a few real calls rather
than assuming the Hobby allowance covers it. If the numbers do not work, the VM
path in [DEPLOY.md](DEPLOY.md) is flat-rate at ~$20/mo and has no duration cap.

## When to prefer the VM

- You want calls longer than your plan's duration limit without a seam.
- You are adding Telnyx later. Telephony holds a media WebSocket open for the
  whole call, which fights a platform that closes it on a timer.
- Duration billing turns out to cost more than a fixed VM at your call volume.
