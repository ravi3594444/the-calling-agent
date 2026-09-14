"""FastAPI app: serves the browser client and bridges its audio to the agent."""

import asyncio
import importlib
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI, Response, WebSocket
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .agent_config import KNOWN_VOICES, RESTAURANT
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
    """Where the browser client lives, from a checkout OR an installed wheel.

    Three sources, in this order, and the order is the contract:

    1. STATIC_DIR, when set. A deployment serving its own agent through this
       relay points it at its own copy of the client, and writing one file
       into that directory replaces one page. It must keep winning: the copy
       below now exists in every install, and if it could outrank the override
       such a deployment would silently serve OUR pages to ITS callers.
    2. `parents[2]`, the repo root from `src/calling_agent/main.py`. This is
       the checkout and the editable install -- `make dev`, the tests, and
       Vercel, which puts the source tree and `static/` side by side.
    3. `static/` inside the package. `pip install .` ships it there (see
       [tool.setuptools] in pyproject.toml). Before that it shipped nowhere:
       both branches above missed, this one named a directory that could not
       exist, and the installed server answered /healthz while every page load
       failed. Lowest precedence on purpose -- it is the fallback for an
       install with nothing configured, not an override of anyone's choice.
    """
    override = os.getenv("STATIC_DIR", "").strip()
    if override:
        return Path(override)
    repo = Path(__file__).resolve().parents[2] / "static"
    if repo.is_dir():
        return repo
    # Last resort: alongside the package, for a wheel that ships it.
    return Path(__file__).resolve().parent / "static"


STATIC_DIR = _static_dir()


def _mount_static(app: FastAPI, directory: Path) -> bool:
    """Mount the browser client if it is there, and carry on if it is not.

    `StaticFiles(check_dir=True)` raises AT IMPORT, so a missing directory did
    not cost the page -- it cost the process. `import calling_agent.main`
    raised, which means the server never started, `/healthz` never answered,
    and a consumer's tests could not even import the module to check anything
    else.

    A plain `pip install` used to land exactly there: the wheel did not ship
    `static/`, so with no STATIC_DIR set every branch of `_static_dir` missed.
    The wheel carries it now, which makes this a mount that normally succeeds
    -- but it is still allowed to fail, because STATIC_DIR can name a
    directory that is not there and a source tree can be deployed without one.

    A missing UI must cost the UI. The websocket, `/healthz` and every agent
    this relay carries do not read a single file from here; the browser client
    is one of the transports, not the product. So: no directory, no `/static`,
    a warning that names the path, and a phone that still answers.
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


app = FastAPI(title="calling-agent", version="0.1.0")
_mount_static(app, STATIC_DIR)


def _build_agent(params: Mapping[str, str] | None = None) -> AgentDefinition | None:
    """Resolve AGENT_FACTORY for one connection, or None for the restaurant.

    Called per connection rather than at import, because a factory that binds
    an agent to its caller (an account, a phone number, a call id) must not
    hand the first caller's identity to everyone after them. A factory that
    ignores the distinction loses nothing by being called again.

    The factory receives the connection's query parameters as its one
    argument. That is the only channel by which who-is-calling can reach an
    agent WITHOUT passing through the conversation: a caller cannot talk their
    way into a different identity, because the identity was fixed before they
    said anything and the model never sees this mapping. A telephony transport
    puts the caller id here for the same reason.

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
        agent = factory(dict(params or {}))
    except Exception:  # noqa: BLE001 - never drop a call over config
        # log.exception, not log.error: this handler covers a dynamic import, an
        # attribute lookup and arbitrary third-party code, and all three end in
        # the same fallback. Without the traceback, "the factory failed" is the
        # only thing anyone ever learns about any of them.
        log.exception("AGENT_FACTORY %r failed; serving the default agent", spec)
        return None
    if not isinstance(agent, AgentDefinition):
        log.error("AGENT_FACTORY %r returned %s, not an AgentDefinition", spec, type(agent))
        return None
    return agent


async def _build_agent_async(params: Mapping[str, str] | None = None) -> AgentDefinition | None:
    """`_build_agent` off the event loop. Every request path uses this one.

    AGENT_FACTORY is arbitrary third-party code, and its documented use --
    bind the agent to its caller -- is a lookup: a row, an HTTP call, a file
    read. Called on the loop it does not merely delay the connection asking
    for it, it stops the audio pump of every OTHER call in the process for as
    long as the lookup takes, and the first one to arrive pays
    `importlib.import_module` (disk I/O) on the same thread. `run_tool` has
    gone through `asyncio.to_thread` for exactly this reason since the tool
    worker existed; this was the other door synchronous foreign code came in
    through, and it was open on `/experience` -- once per page load -- as well
    as on every websocket connection.

    Unset AGENT_FACTORY stays on the loop: there is nothing to run, and
    handing a thread back and forth to answer None would be a cost every page
    load pays for a feature nobody configured. The fallbacks are `_build_agent`'s
    and are unchanged, deliberately: a factory that raises still serves the
    default agent rather than dropping the call.
    """
    if not settings.agent_factory.strip():
        return None
    return await asyncio.to_thread(_build_agent, params)


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
    """Public product context, with no secrets and no billable upstream call.

    The name comes from the CONFIGURED agent, not from the restaurant settings:
    a relay serving someone else's agent otherwise labels their product with
    the restaurant's name. Resolved without connection parameters, so a factory
    that varies by caller answers for its default caller — which is all a page
    can ask before anyone has called.
    """
    agente = await _build_agent_async() or RESTAURANT
    return {
        "restaurant": agente.display_name or settings.restaurant_name,
        "agent": agente.name,
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
    # The CONFIGURED agent, not the restaurant: diagnosing a payload that no
    # live call ever sends is how /diagnose reports a healthy session while
    # every real one is refused for a malformed prompt or tool declaration.
    return await run_diagnostics(agent=await _build_agent_async())


_SIN_CLIENTE = (
    "The browser client is not deployed on this instance.\n"
    "The API, /healthz and the /ws relay are unaffected.\n"
    "Set STATIC_DIR to the directory holding index.html to serve the UI.\n"
)


@app.get("/")
async def index() -> Response:
    """Serve the browser client, or say plainly that it is not deployed.

    `_mount_static` decided a missing `static/` costs the UI and not the
    process -- then this route handed the same missing path to FileResponse,
    which raises while the response is being written, so the one URL a human
    opens first answered 500 with a traceback for a condition the module had
    already called survivable. Half an invariant is not one: the directory is
    checked here too.

    503, not 404: the page is missing because this deployment has no copy of
    it, which is a deployment fact an operator can fix, not a URL that does
    not exist. The body names STATIC_DIR and nothing else -- the path it
    resolved to goes in the log, not to the public.
    """
    page = STATIC_DIR / "index.html"
    if not page.is_file():
        return PlainTextResponse(_SIN_CLIENTE, status_code=503)
    return FileResponse(page)


# Browsers request these regardless of the inline <link rel="icon">, and the
# catch-all rewrite on Vercel routes them here, so without a handler every page
# load logs two 404s.
@app.get("/favicon.ico")
@app.get("/favicon.png")
async def favicon() -> Response:
    """404 when there is no icon, rather than a 500 from FileResponse.

    404 and not 503 like `/`: a browser asking for an icon this deployment
    does not have gets the answer it already knows how to handle and caches.
    Whether the UI is deployed at all is `/`'s answer to give, once.
    """
    icon = STATIC_DIR / "favicon.svg"
    if not icon.is_file():
        return Response(status_code=404)
    return FileResponse(icon, media_type="image/svg+xml")


@app.websocket("/ws")
async def ws(websocket: WebSocket, resume: str | None = None, voice: str | None = None) -> None:
    """Bridge one browser to one agent session.

    `resume` carries a session id the client saw earlier. The client holds it
    rather than the server because on a serverless platform the instance that
    started the call is not necessarily the one handling the reconnect -- there
    is no server-side memory to look it up in.

    `voice` overrides AGENT_VOICE for this call only, so voices can be compared
    by ear without a redeploy.

    Every query parameter, named or not, is handed to AGENT_FACTORY. What an
    agent makes of them is its own business; this relay does not read them.
    """
    await websocket.accept()
    client = websocket.client.host if websocket.client else "unknown"
    log.info("browser connected from %s%s", client, " (resuming)" if resume else "")
    await AgentSession(
        BrowserTransport(websocket),
        resume_session_id=resume,
        voice=voice,
        agent=await _build_agent_async(websocket.query_params),
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
