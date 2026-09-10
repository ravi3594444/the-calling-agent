"""FastAPI app: serves the browser client and bridges its audio to the agent."""

import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe. Reports config validity without leaking the key."""
    return {
        "status": "ok",
        "api_key_configured": bool(settings.assemblyai_api_key),
        "upstream": settings.assemblyai_agent_ws_url,
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
async def ws(websocket: WebSocket, resume: str | None = None) -> None:
    """Bridge one browser to one agent session.

    `resume` carries a session id the client saw earlier. The client holds it
    rather than the server because on a serverless platform the instance that
    started the call is not necessarily the one handling the reconnect -- there
    is no server-side memory to look it up in.
    """
    await websocket.accept()
    client = websocket.client.host if websocket.client else "unknown"
    log.info("browser connected from %s%s", client, " (resuming)" if resume else "")
    await AgentSession(BrowserTransport(websocket), resume_session_id=resume).run()
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
