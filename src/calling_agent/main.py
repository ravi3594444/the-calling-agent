"""FastAPI app: serves the browser client and bridges its audio to the agent."""

import importlib
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent_config import KNOWN_VOICES
from .agent_spec import AgentDefinition
from .config import settings
from .diagnostics import run_diagnostics
from .session import AgentSession
from .transport import BrowserTransport

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("calling_agent")

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"

app = FastAPI(title="calling-agent", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _build_agent() -> AgentDefinition | None:
    """Resolve AGENT_FACTORY for one connection, or None for the restaurant.

    Called per connection rather than at import, because a factory that binds
    an agent to its caller (an account, a phone number, a call id) must not
    hand the first caller's identity to everyone after them. A factory that
    ignores the distinction loses nothing by being called again.

    A broken factory serves the restaurant rather than dropping the call: a
    typo in a deployment variable should not be the difference between a phone
    that answers and one that rings out.
    """
    spec = settings.agent_factory.strip()
    if not spec:
        return None
    try:
        module_path, _, attribute = spec.partition(":")
        factory = getattr(importlib.import_module(module_path), attribute)
        agent = factory()
    except Exception as exc:  # noqa: BLE001 - never drop a call over config
        log.error("AGENT_FACTORY %r failed (%s); serving the default agent", spec, exc)
        return None
    if not isinstance(agent, AgentDefinition):
        log.error("AGENT_FACTORY %r returned %s, not an AgentDefinition", spec, type(agent))
        return None
    return agent


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe. Reports config validity without leaking the key."""
    return {
        "status": "ok",
        "api_key_configured": bool(settings.assemblyai_api_key),
        "upstream": settings.assemblyai_agent_ws_url,
    }


@app.get("/voices")
async def voices() -> dict:
    """Voices verified to work, and the one currently configured.

    AssemblyAI's full catalogue is larger than this; an id absent here may
    still be valid. Pass any id as /ws?voice=<id> to try it without redeploying.
    """
    return {"current": settings.agent_voice, "known": KNOWN_VOICES}


@app.get("/experience")
async def experience() -> dict:
    """Public product context, with no secrets and no billable upstream call."""
    return {
        "restaurant": settings.restaurant_name,
        "cuisine": settings.restaurant_cuisine,
        "live_configured": bool(settings.assemblyai_api_key),
        "booking_storage": "memory",
        "interruptions": settings.allow_interruptions,
    }


@app.get("/diagnose")
async def diagnose() -> dict:
    """Walk the real call path upstream and report which step fails.

    Read-only apart from opening one short agent session, which is billable
    but brief. Open this in a browser when the page will not talk.
    """
    return await run_diagnostics()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# Browsers request these regardless of the inline <link rel="icon">, and the
# catch-all rewrite on Vercel routes them here, so without a handler every page
# load logs two 404s.
@app.get("/favicon.ico")
@app.get("/favicon.png")
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.websocket("/ws")
async def ws(websocket: WebSocket, resume: str | None = None, voice: str | None = None) -> None:
    """Bridge one browser to one agent session.

    `resume` carries a session id the client saw earlier. The client holds it
    rather than the server because on a serverless platform the instance that
    started the call is not necessarily the one handling the reconnect -- there
    is no server-side memory to look it up in.

    `voice` overrides AGENT_VOICE for this call only, so voices can be compared
    by ear without a redeploy.
    """
    await websocket.accept()
    client = websocket.client.host if websocket.client else "unknown"
    log.info("browser connected from %s%s", client, " (resuming)" if resume else "")
    await AgentSession(
        BrowserTransport(websocket),
        resume_session_id=resume,
        voice=voice,
        agent=_build_agent(),
    ).run()
    log.info("session for %s ended", client)


def run() -> None:
    import uvicorn

    uvicorn.run(
        "calling_agent.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
