"""FastAPI app: serves the browser client and bridges its audio to the agent."""

import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    client = websocket.client.host if websocket.client else "unknown"
    log.info("browser connected from %s", client)
    await AgentSession(BrowserTransport(websocket)).run()
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
