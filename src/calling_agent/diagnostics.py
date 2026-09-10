"""Self-test for the upstream connection.

"It is not talking" has several possible causes that look identical from the
page: no API key, a rejected key, a refused session config, an account with no
credit, or an agent that connects fine but never speaks. This walks the same
path a real call takes and reports which step failed, so the answer comes from
the deployment itself rather than from guesswork.
"""

import asyncio
import json
import logging
import time
from typing import Any

import websockets

from . import protocol as p
from .agent_config import build_session_update
from .config import settings

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10.0
# The greeting is synthesised before any audio is sent, so a few seconds is
# enough to see whether the agent speaks unprompted.
LISTEN_SECONDS = 8.0


def _step(name: str, ok: bool, **extra: Any) -> dict:
    return {"step": name, "ok": ok, **extra}


async def run_diagnostics() -> dict:
    steps: list[dict] = []

    # 1. Is the key even configured?
    if not settings.assemblyai_api_key:
        steps.append(
            _step(
                "api_key",
                False,
                detail="ASSEMBLYAI_API_KEY is empty.",
                fix="Set it in Vercel > Project > Settings > Environment "
                "Variables, then redeploy. Variables added after a deploy do "
                "not reach it.",
            )
        )
        return {"ok": False, "steps": steps}

    key = settings.assemblyai_api_key
    steps.append(
        _step("api_key", True, detail=f"present, {len(key)} chars, ends {key[-4:]}")
    )

    # 2. Can we open the socket? A rejection here is auth or a wrong path.
    started = time.monotonic()
    try:
        upstream = await asyncio.wait_for(
            websockets.connect(
                settings.assemblyai_agent_ws_url,
                additional_headers={"Authorization": f"Bearer {key}"},
                max_size=None,
            ),
            timeout=CONNECT_TIMEOUT,
        )
    except websockets.InvalidStatus as exc:
        code = exc.response.status_code
        fix = {
            401: "The key was rejected. Copy it again from the AssemblyAI "
            "dashboard; check for a trailing space.",
            403: "The key is valid but not permitted to use the Voice Agent "
            "API. Check the account is enabled for it and has credit.",
            404: f"No endpoint at {settings.assemblyai_agent_ws_url}. Set "
            "ASSEMBLYAI_AGENT_WS_URL to the other documented path "
            "(/v1/ws or /v1/realtime).",
        }.get(code, "Check the endpoint URL and the key.")
        steps.append(_step("connect", False, http_status=code, fix=fix))
        return {"ok": False, "steps": steps}
    except TimeoutError:
        steps.append(
            _step(
                "connect",
                False,
                detail=f"no response in {CONNECT_TIMEOUT}s",
                fix="Outbound WebSocket traffic may be blocked from this host.",
            )
        )
        return {"ok": False, "steps": steps}
    except OSError as exc:
        steps.append(_step("connect", False, detail=str(exc), fix="Network or DNS failure."))
        return {"ok": False, "steps": steps}

    steps.append(
        _step(
            "connect",
            True,
            detail=f"connected in {time.monotonic() - started:.2f}s",
            url=settings.assemblyai_agent_ws_url,
        )
    )

    # 3. Send the real session config and watch what comes back.
    async with upstream:
        payload = build_session_update(p.ENCODING_PCM)
        await upstream.send(json.dumps(payload))

        seen: list[str] = []
        transcripts: list[str] = []
        audio_chunks = 0
        audio_bytes = 0
        ready = False
        rejected: dict | None = None

        deadline = time.monotonic() + LISTEN_SECONDS
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                raw = await asyncio.wait_for(upstream.recv(), timeout=remaining)
                msg = json.loads(raw)
                kind = msg.get("type", "?")
                if kind != p.REPLY_AUDIO:
                    seen.append(kind)

                if kind == p.SESSION_READY:
                    ready = True
                elif kind == p.REPLY_AUDIO:
                    audio_chunks += 1
                    audio_bytes += len(msg.get(p.REPLY_AUDIO_FIELD) or "")
                elif kind == p.TRANSCRIPT_AGENT:
                    transcripts.append(msg.get("text") or msg.get("transcript") or "")
                elif kind == p.SESSION_ERROR:
                    rejected = {"code": msg.get("code"), "message": msg.get("message")}
                    break
        except TimeoutError:
            pass  # listening window elapsed, which is the normal exit
        except websockets.ConnectionClosed as exc:
            if not (ready and audio_chunks):
                rejected = {"close_code": exc.code, "reason": (exc.reason or "").strip()}

    # 4. Interpret.
    if rejected is not None:
        steps.append(
            _step(
                "session_config",
                False,
                events_seen=seen,
                rejection=rejected,
                sent_fields=sorted(payload["session"].keys()),
                fix="The reason above names the problem. A close code of 1008 "
                "means a field in session.update was refused.",
            )
        )
        return {"ok": False, "steps": steps}

    steps.append(_step("session_config", ready, events_seen=seen, accepted=ready))

    speaking = audio_chunks > 0
    steps.append(
        _step(
            "agent_speaks",
            speaking,
            audio_chunks=audio_chunks,
            audio_base64_bytes=audio_bytes,
            agent_said=transcripts,
            fix=None
            if speaking
            else "The session opened but the agent sent no audio. Usually an "
            "empty greeting, or an account with no credit -- the Voice Agent "
            "API bills $4.50/hr and will not synthesise at a zero balance.",
        )
    )

    ok = ready and speaking
    return {
        "ok": ok,
        "summary": "Upstream is healthy; the agent connects and speaks."
        if ok
        else "Upstream reachable but the call path is broken -- see the failing step.",
        "voice": settings.agent_voice,
        "steps": steps,
    }
