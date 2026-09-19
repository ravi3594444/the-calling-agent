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


def test_static_dir_is_found_from_a_checkout():
    """The default path must resolve in the repo, or `make dev` serves nothing."""
    from calling_agent.main import STATIC_DIR

    assert (STATIC_DIR / "index.html").is_file()


def test_static_dir_can_be_named_explicitly(monkeypatch, tmp_path):
    """What a container installing this from git needs.

    Mutation: ignoring STATIC_DIR kills this. Without the override, an
    installed wheel resolves `parents[2]` to site-packages/../.. and the
    server fails at import rather than at first request.
    """
    from calling_agent import main

    (tmp_path / "index.html").write_text("<!-- elsewhere -->")
    monkeypatch.setenv("STATIC_DIR", str(tmp_path))
    assert main._static_dir() == tmp_path


def test_no_override_prefers_the_checkout_over_the_package_dir(monkeypatch):
    from calling_agent import main

    monkeypatch.delenv("STATIC_DIR", raising=False)
    assert (main._static_dir() / "index.html").is_file()


def test_a_missing_static_dir_costs_the_page_and_not_the_process(tmp_path):
    """The failure a plain `pip install` hits, and the reason it was invisible.

    The wheel does not ship `static/` -- it sits at the repo root, beside
    `src/`, so it is not inside the package. Installed normally, with no
    STATIC_DIR set, `_static_dir()` returns a path that cannot exist and
    `StaticFiles(check_dir=True)` raised while the MODULE was importing: not a
    404, a server that never starts. The consumer repo's tests could not even
    import `calling_agent.main`.

    One mutation per half, because the two halves protect opposite mistakes:

    | mutation                                   | result             |
    |--------------------------------------------|--------------------|
    | `app.mount(...)` unconditionally, as it was | 1 failed, 119 passed |
    | `_mount_static` returns True without mounting | 2 failed, 118 passed |

    The first is killed only by this test. The second is killed by its second
    half AND by `test_index_serves_client_page` (404 != 200) -- which is the
    point of asserting the route exists rather than only that nothing raised:
    a `_mount_static` that quietly mounts nothing passes "it did not raise".
    """
    from fastapi import FastAPI

    from calling_agent import main

    vacio = FastAPI()
    assert main._mount_static(vacio, tmp_path / "no-existe") is False
    assert not [r for r in vacio.routes if getattr(r, "name", "") == "static"]

    real = FastAPI()
    (tmp_path / "index.html").write_text("<!-- sí existe -->")
    assert main._mount_static(real, tmp_path) is True
    assert [r for r in real.routes if getattr(r, "name", "") == "static"]


# --- where the browser client is found ---------------------------------------
#
# These exist because `/` returned a 500 in the container for the first real
# deploy. The image pip-installs the package into site-packages and copies
# `static/` beside the WORKDIR, so both paths derived from `__file__` miss --
# a layout no test had ever exercised, because every test runs from a checkout.


def test_the_client_is_found_from_the_working_directory(tmp_path, monkeypatch):
    """The container layout: package installed, static/ beside the WORKDIR."""
    from calling_agent import main

    (tmp_path / "static").mkdir()
    monkeypatch.delenv("STATIC_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    # Point the __file__-relative candidates somewhere that cannot exist.
    monkeypatch.setattr(main, "__file__", str(tmp_path / "nowhere" / "deep" / "main.py"))

    assert main._static_dir() == tmp_path / "static"


def test_static_dir_wins_over_everything(tmp_path, monkeypatch):
    from calling_agent import main

    explicit = tmp_path / "somewhere-else"
    explicit.mkdir()
    monkeypatch.setenv("STATIC_DIR", str(explicit))

    assert main._static_dir() == explicit


def test_a_missing_client_costs_the_page_and_nothing_else(tmp_path, monkeypatch):
    """No index.html must not read as a broken server.

    The API, the dashboard and every phone this relay carries work without it,
    so the answer says what is missing instead of raising a 500 with nothing
    in it.
    """
    from fastapi.testclient import TestClient

    from calling_agent import main

    monkeypatch.setattr(main, "STATIC_DIR", tmp_path / "not-here")
    client = TestClient(main.app)

    page = client.get("/")
    assert page.status_code == 503
    assert "STATIC_DIR" in page.json()["fix"]

    # The icon browsers ask for regardless must not 500 either.
    assert client.get("/favicon.ico").status_code == 204

    # And the parts that do not need a file are unaffected.
    assert client.get("/healthz").status_code == 200


def test_the_client_is_served_from_a_checkout():
    """The layout every developer runs. Would have passed all along -- which is
    exactly why the container layout went unnoticed."""
    from fastapi.testclient import TestClient

    from calling_agent import main

    if not (main.STATIC_DIR / "index.html").is_file():
        import pytest

        pytest.skip("no static/ in this install")
    assert TestClient(main.app).get("/").status_code == 200
