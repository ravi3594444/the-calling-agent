"""Vercel entrypoint.

Vercel's Python runtime looks for an ASGI app named `app` in this module and
serves it as a Function, so there is no uvicorn process here -- the platform
provides the server. The application itself is unchanged, which is why the same
code runs under `make dev` locally and on Vercel unmodified.

The path insert is needed because the package lives under src/ and Vercel
installs only requirements.txt, not the local project.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from calling_agent.main import app  # noqa: E402

__all__ = ["app"]
