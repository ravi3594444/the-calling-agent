"""FastAPI app: the voice relay, the dashboard, the tool API and telephony.

Four surfaces, one process:

    /ws              a browser microphone bridged to an agent session
    /twilio/*        a phone call bridged to the same agent session
    /api/*           the dashboard (PRD §16b)
    /api/tools/*     the agent's own tool contract, for other systems (PRD §8)

The relay itself is unchanged and is still the hardest code here. What this
file adds is the seam where a connection becomes a TENANT: a dialled number, a
dashboard token or a slug, resolved once at the edge and passed down.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import agent_tools, db, jobs
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


def _static_dir() -> Path:
    """Where the browser clients live, from a checkout OR an installed wheel.

    STATIC_DIR names the directory explicitly, which is what a container that
    pip-installs from git needs.
    """
    override = os.getenv("STATIC_DIR", "").strip()
    if override:
        return Path(override)
    repo = Path(__file__).resolve().parents[2] / "static"
    if repo.is_dir():
        return repo
    return Path(__file__).resolve().parent / "static"


STATIC_DIR = _static_dir()


def _mount_static(app: FastAPI, directory: Path) -> bool:
    """Mount the browser client if it is there, and carry on if it is not.

    `StaticFiles(check_dir=True)` raises AT IMPORT, so a missing directory
    would cost the process rather than the page: the server never starts,
    /healthz never answers, and every phone this relay carries stops ringing
    over a missing HTML file. A missing UI must cost only the UI.
    """
    if not directory.is_dir():
        log.warning(
            "static directory %s does not exist: serving the API without the "
            "browser client. Set STATIC_DIR if you meant to serve it.",
            directory,
        )
        return False
    app.mount("/static", StaticFiles(directory=directory), name="static")
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Migrate if asked, then start the background jobs.

    Migrations are opt-in because in production they belong to the deploy, not
    to whichever web process happened to boot first. In development, one
    variable saves a step.
    """
    if settings.migrate_on_start:
        applied = await asyncio.to_thread(db.migrate)
        log.info("migrations applied: %s", applied or "none")

    worker = None
    if settings.run_jobs_in_process:
        worker = asyncio.create_task(jobs.run_forever())

    yield

    if worker is not None:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


app = FastAPI(title="tableline", version="1.0.0", lifespan=lifespan)
_mount_static(app, STATIC_DIR)

from .api import (  # noqa: E402
    dashboard_router,
    manage_router,
    telephony_router,
    tools_router,
)

app.include_router(dashboard_router)
app.include_router(tools_router)
app.include_router(telephony_router)
app.include_router(manage_router)


def _build_agent(params: Mapping[str, str] | None = None) -> AgentDefinition | None:
    """Resolve AGENT_FACTORY for one connection, or None for the default agent.

    Called per connection rather than at import, because a factory that binds
    an agent to its caller (an account, a phone number, a call id) must not
    hand the first caller's identity to everyone after them.

    A broken factory serves the default agent rather than dropping the call: a
    typo in a deployment variable should not be the difference between a phone
    that answers and one that rings out.
    """
    spec = settings.agent_factory.strip()
    if not spec:
        return None
    try:
        module_path, _, attribute = spec.partition(":")
        factory = getattr(importlib.import_module(module_path), attribute)
        agent = factory(dict(params or {}))
    except Exception:  # noqa: BLE001 - never drop a call over config
        log.exception("AGENT_FACTORY %r failed; serving the default agent", spec)
        return None
    if not isinstance(agent, AgentDefinition):
        log.error("AGENT_FACTORY %r returned %s, not an AgentDefinition", spec, type(agent))
        return None
    return agent


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe. Reports config validity without leaking the key."""
    database_ok, database_detail = await asyncio.to_thread(db.healthy)
    return {
        "status": "ok" if database_ok else "degraded",
        "api_key_configured": bool(settings.assemblyai_api_key),
        "upstream": settings.assemblyai_agent_ws_url,
        "database": {"ok": database_ok, "detail": database_detail},
    }


@app.get("/voices")
async def voices() -> dict:
    """Voices verified to work, and the one currently configured.

    AssemblyAI's full catalogue is larger than this; an id absent here may
    still be valid. Pass any id as /ws?voice=<id> to try it without redeploying.
    """
    return {"current": settings.agent_voice, "known": KNOWN_VOICES}


@app.get("/experience")
async def experience(business: str | None = None) -> dict:
    """Public product context, with no secrets and no billable upstream call."""
    agent = _build_agent() or await asyncio.to_thread(_agent_for_slug, business)
    database_ok, _ = await asyncio.to_thread(db.healthy)
    return {
        "restaurant": agent.display_name or "Tableline",
        "agent": agent.name,
        "live_configured": bool(settings.assemblyai_api_key),
        "booking_storage": "postgres" if database_ok else "unavailable",
        "interruptions": settings.allow_interruptions,
    }


def _agent_for_slug(slug: str | None) -> AgentDefinition:
    if slug:
        return agent_tools.agent_for(slug=slug)
    return agent_tools.default_agent()


@app.get("/diagnose")
async def diagnose() -> dict:
    """Walk the real call path upstream and report which step fails.

    Read-only apart from opening one short agent session, which is billable
    but brief. Open this in a browser when the page will not talk.
    """
    return await run_diagnostics(agent=_build_agent())


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard")
async def dashboard_page() -> FileResponse:
    """The staff dashboard. Opened from a long secret link, saved to a home screen."""
    page = STATIC_DIR / "dashboard" / "index.html"
    if not page.is_file():
        return JSONResponse({"error": "dashboard is not installed"}, status_code=404)
    return FileResponse(page)


# Browsers request these regardless of the inline <link rel="icon">, and the
# catch-all rewrite on Vercel routes them here, so without a handler every page
# load logs two 404s.
@app.get("/favicon.ico")
@app.get("/favicon.png")
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.websocket("/ws")
async def ws(
    websocket: WebSocket,
    resume: str | None = None,
    voice: str | None = None,
    business: str | None = None,
    encoding: str = "pcm",
) -> None:
    """Bridge one browser to one agent session.

    `resume` carries a session id the client saw earlier. The client holds it
    rather than the server because on a serverless platform the instance that
    started the call is not necessarily the one handling the reconnect.

    `voice` overrides AGENT_VOICE for this call only, so voices can be compared
    by ear without a redeploy.

    `business` names the tenant. A browser has no dialled number, so it says
    which venue it is calling; without one, DEFAULT_BUSINESS_SLUG decides.

    `encoding=pcmu` runs the call at 8 kHz mu-law -- telephone band, telephone
    codec -- so the browser client can be used to judge how the agent will
    sound on a real line before there is a real line.
    """
    await websocket.accept()
    client = websocket.client.host if websocket.client else "unknown"
    log.info("browser connected from %s%s", client, " (resuming)" if resume else "")

    agent = _build_agent(websocket.query_params)
    if agent is None:
        agent = await asyncio.to_thread(_agent_for_slug, business)

    await AgentSession(
        BrowserTransport(websocket, encoding=encoding),
        resume_session_id=resume,
        voice=voice,
        agent=agent,
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
