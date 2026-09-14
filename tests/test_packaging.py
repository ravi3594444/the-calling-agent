"""Guards on deployment config.

Vercel installs from requirements.txt while local development installs from
pyproject.toml. Nothing enforces that those agree, and a missing dependency
only shows up as a failed deploy, so check it here.
"""

import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
STATIC_FILES = sorted(
    f.relative_to(ROOT / "static").as_posix() for f in (ROOT / "static").rglob("*") if f.is_file()
)


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


def _build_a_wheel(tmp_path: Path) -> zipfile.ZipFile:
    """Build this project's wheel in a COPY of the tree, and return it.

    A copy, because building in place writes `build/` and `.egg-info` into the
    checkout, and because a wheel built from a copy can only carry what the
    packaging configuration says to carry -- which is the thing under test.
    """
    proyecto, salida = tmp_path / "proyecto", tmp_path / "dist"
    proyecto.mkdir()
    salida.mkdir()
    for nombre in ("pyproject.toml", "README.md"):
        shutil.copy(ROOT / nombre, proyecto / nombre)
    basura = shutil.ignore_patterns("*.egg-info", "__pycache__")
    shutil.copytree(ROOT / "src", proyecto / "src", ignore=basura)
    shutil.copytree(ROOT / "static", proyecto / "static", ignore=basura)

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, setuptools.build_meta as bm; bm.build_wheel(sys.argv[1])",
            str(salida),
        ],
        cwd=proyecto,
        check=True,
        capture_output=True,
    )
    (rueda,) = salida.glob("*.whl")
    return zipfile.ZipFile(rueda)


def test_the_wheel_carries_the_browser_client(tmp_path):
    """`pip install .` has to install the UI, not just the relay.

    `static/` sits at the repo root, beside `src/`, so it is not inside the
    package and setuptools left it out: the installed server resolved
    `_static_dir()` to a path that cannot exist, answered /healthz 200 and
    500ed on every page load. The Docker image was that install.

    The assertion is every file, not `index.html`: a pattern that stopped
    recursing would ship the page and none of the js, fonts or worklet it
    loads, which is the same blank screen with a healthier-looking wheel.

    Mutation: narrowing package-data from "**/*" to "*" in pyproject.toml --
    the wheel still carries index.html, styles.css and the worklet, and every
    other test in the suite passes, but js/ and fonts/ are gone and the page
    they leave behind is blank. 1 failed, 128 passed.

    (Dropping "calling_agent.static" from `packages` also kills this, but it
    kills `test_every_package_and_static_subdirectory_is_declared` with it, so
    it measures less: the narrowing is the mutation this test alone catches.)
    """
    assert STATIC_FILES, "static/ is empty: this test would assert nothing"
    dentro = set(_build_a_wheel(tmp_path).namelist())
    faltan = [f"calling_agent/static/{name}" for name in STATIC_FILES]
    assert not [n for n in faltan if n not in dentro], (
        f"the wheel is missing: {[n for n in faltan if n not in dentro]}"
    )


def test_every_package_and_static_subdirectory_is_declared(tmp_path):
    """`packages` is an explicit list now, and an explicit list goes stale.

    Mapping `static/` in cost the project `packages.find`, which had picked up
    a new subpackage on its own. Two different things go missing without it,
    so both are checked:

    * a module under a directory nobody listed imports fine from the checkout
      and is simply absent from the wheel -- visible only once something
      installs it;
    * a NEW subdirectory of `static/` (an `img/`, say) still ships today, but
      setuptools warns that it is "absent from the `packages` configuration"
      and says it may stop shipping such a directory in a future version. A
      warning in a build log is not a thing anyone reads.

    Two claims, two mutations, because one of them passing proves nothing
    about the other:

    | mutation                                            | result           |
    |-----------------------------------------------------|------------------|
    | delete "calling_agent.transport" from `packages`     | 1 failed, 128 passed |
    | delete "calling_agent.static.js" from `packages`     | 1 failed, 128 passed |
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declarados = set(pyproject["tool"]["setuptools"]["packages"])

    codigo = {
        "calling_agent."
        + init.parent.relative_to(ROOT / "src" / "calling_agent").as_posix().replace("/", ".")
        for init in (ROOT / "src" / "calling_agent").rglob("__init__.py")
        if init.parent != ROOT / "src" / "calling_agent"
    } | {"calling_agent"}
    # RELATIVE parts, not the absolute path's: a checkout living under a
    # dotted directory would otherwise skip every subdirectory of static/ and
    # leave this half of the test asserting nothing at all.
    subdirectorios = [
        d.relative_to(ROOT / "static")
        for d in (ROOT / "static").rglob("*")
        if d.is_dir()
    ]
    datos = {
        "calling_agent.static." + d.as_posix().replace("/", ".")
        for d in subdirectorios
        # Hidden and dunder directories are tooling droppings, not client files.
        if not any(part.startswith((".", "__")) for part in d.parts)
    } | {"calling_agent.static"}
    assert len(datos) > 1, "static/ has no subdirectories: this half asserts nothing"
    assert not (codigo | datos) - declarados, (
        f"not declared in pyproject.toml: {sorted((codigo | datos) - declarados)}"
    )

    # ...and the code packages really are in the wheel, not merely named.
    dentro = set(_build_a_wheel(tmp_path).namelist())
    for paquete in codigo:
        assert f"{paquete.replace('.', '/')}/__init__.py" in dentro


def test_static_dir_precedence_is_override_then_checkout_then_package(monkeypatch, tmp_path):
    """The order is a cross-repo contract, not an implementation detail.

    Another repository installs this relay, keeps its clone on disk, points
    STATIC_DIR at that clone's `static/` and overwrites one file in it with
    its own page. Now that the wheel ALSO ships a copy of `static/`, a
    resolution order that preferred the packaged copy would serve this
    project's restaurant page to that deployment's callers -- silently, with
    every health check green.

    So all three sources exist at once here, and each one is removed in turn:

    | present                        | must resolve to |
    |--------------------------------|-----------------|
    | override + checkout + package  | the override    |
    | checkout + package             | the checkout    |
    | package                        | the package     |

    Mutation: returning the package directory first when it exists (leaving
    every other branch untouched) kills this and nothing else -- from a
    checkout `src/calling_agent/static` does not exist, so every other test in
    this file sees the unmutated order.
    """
    from calling_agent import main

    raiz = tmp_path.resolve()
    paquete = raiz / "site-packages" / "calling_agent"
    paquete.mkdir(parents=True)
    monkeypatch.setattr(main, "__file__", str(paquete / "main.py"))

    empaquetado = paquete / "static"  # what an installed wheel carries
    empaquetado.mkdir()
    checkout = raiz / "static"  # what `parents[2]` finds in a source tree
    propio = raiz / "el-cliente-del-consumidor"  # what STATIC_DIR names
    propio.mkdir()

    monkeypatch.delenv("STATIC_DIR", raising=False)
    assert main._static_dir() == empaquetado

    checkout.mkdir()
    assert main._static_dir() == checkout

    monkeypatch.setenv("STATIC_DIR", str(propio))
    assert main._static_dir() == propio


def test_index_degrades_instead_of_500ing_when_the_client_is_missing(monkeypatch, tmp_path):
    """`/` is the one URL a human opens first, and it answered 500.

    `_mount_static` already treats a missing directory as costing the UI and
    not the process, but `index()` handed that same missing path to
    FileResponse, which raises while the response is being written. So the
    module logged "serving the API without the browser client" and then
    returned a traceback to the only visitor who could have read it.

    Mutation: `return FileResponse(STATIC_DIR / "index.html")`, as it was,
    kills this (500, not 503) and leaves every other test passing.
    """
    from calling_agent import main

    monkeypatch.setattr(main, "STATIC_DIR", tmp_path / "no-desplegado")
    cliente = TestClient(main.app, raise_server_exceptions=False)

    respuesta = cliente.get("/")
    assert respuesta.status_code == 503
    assert "STATIC_DIR" in respuesta.text
    # The phone still answers -- the point of degrading rather than failing.
    assert cliente.get("/healthz").json()["status"] == "ok"


def test_the_favicon_degrades_instead_of_500ing(monkeypatch, tmp_path):
    """Two routes, same missing file, and browsers ask for it unprompted.

    404 rather than `/`'s 503: a browser that asked for an icon this
    deployment does not have gets the answer it already handles and caches,
    and the deployment's actual state is reported once, by `/`.

    Mutation: `return FileResponse(STATIC_DIR / "favicon.svg", ...)`, as it
    was, kills this (500, not 404). `/` keeps passing its own test.
    """
    from calling_agent import main

    monkeypatch.setattr(main, "STATIC_DIR", tmp_path / "no-desplegado")
    cliente = TestClient(main.app, raise_server_exceptions=False)

    for ruta in ("/favicon.ico", "/favicon.png"):
        assert cliente.get(ruta).status_code == 404


def test_a_deployed_client_is_still_served_from_the_directory_that_wins(monkeypatch, tmp_path):
    """The degraded path must not become a fallback that MASKS a real page.

    A deployment that points STATIC_DIR at its own directory and replaces one
    file in it has to see its own file, not this project's copy and not the
    "not deployed" notice. Writing into the resolved directory is how that
    deployment brands the relay.

    Mutation: have `index()` keep checking `STATIC_DIR / "index.html"` but
    hand FileResponse the repo's own page instead -- a check/use mismatch
    that every other test in the suite passes, because every other test's
    resolved directory IS the repo's. Only a deployment that replaced the
    file notices, which is the whole point of this one.
    """
    from calling_agent import main

    propio = tmp_path / "cliente-propio"
    propio.mkdir()
    (propio / "index.html").write_text("<!doctype html><title>otro producto</title>")
    (propio / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    monkeypatch.setattr(main, "STATIC_DIR", propio)
    cliente = TestClient(main.app, raise_server_exceptions=False)

    respuesta = cliente.get("/")
    assert respuesta.status_code == 200
    assert "otro producto" in respuesta.text
    assert "Tableline" not in respuesta.text
    assert cliente.get("/favicon.ico").status_code == 200
