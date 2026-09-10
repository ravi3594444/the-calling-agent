"""Guards on deployment config.

Vercel installs from requirements.txt while local development installs from
pyproject.toml. Nothing enforces that those agree, and a missing dependency
only shows up as a failed deploy, so check it here.
"""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _names(lines: list[str]) -> set[str]:
    out = set()
    for raw in lines:
        line = raw.split("#")[0].strip()
        if not line:
            continue
        # Strip extras and any version specifier: "uvicorn[standard]>=0.32" -> "uvicorn"
        out.add(re.split(r"[<>=!\[;]", line)[0].strip().lower())
    return out


def test_requirements_covers_runtime_dependencies():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = _names(pyproject["project"]["dependencies"])
    pinned = _names((ROOT / "requirements.txt").read_text().splitlines())

    # uvicorn is a local dev server only; Vercel provides the server itself.
    missing = declared - pinned - {"uvicorn"}
    assert not missing, f"requirements.txt is missing: {sorted(missing)}"


def test_vercel_config_is_consistent_with_the_entrypoint():
    cfg = json.loads((ROOT / "vercel.json").read_text())
    entry = "api/index.py"
    assert entry in cfg["functions"], f"vercel.json must configure {entry}"
    assert (ROOT / entry).is_file()

    fn = cfg["functions"][entry]
    # 300s is the Hobby ceiling. Anything higher silently fails to apply there.
    assert fn["maxDuration"] <= 300, "Hobby plan caps maxDuration at 300s"
    # The app serves static/ itself, so those files must ship with the function.
    assert "static" in fn["includeFiles"]


def test_all_routes_reachable_through_the_catch_all_rewrite():
    cfg = json.loads((ROOT / "vercel.json").read_text())
    assert cfg["rewrites"] == [{"source": "/(.*)", "destination": "/api/index"}]
