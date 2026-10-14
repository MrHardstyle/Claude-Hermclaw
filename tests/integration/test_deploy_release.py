"""P32 32.1/32.2/32.5 – ``scripts/deploy/build-release.sh`` builds a real, deployable release for Debian 13 / CPython 3.13.

The release set every deployment test uses is built **once per pytest session** (cached below the session's base temp
directory, shared by ``test_deploy_converge.py``):

* ``0.1.0rc1`` – the checkout's version, built online through the host's proxy with the real UI build (``npm ci && npm
  run build``); every wheel it used is collected into a local wheel cache,
* ``0.1.0rc2`` – the same sources as another version (``--version``), offline from that cache (``--offline-from``) with
  the first build's UI bundle (``--ui-dist``),
* ``0.1.0rc3`` – a source copy with one more Alembic revision (``0003``), offline: the schema-upgrade / rollback case.

If the checkout itself cannot produce a deployable release because of the known defect of its worker console entry
points (``pyproject.toml`` names ``worker.execution.app:main`` / ``worker.model.app:main``, which do not exist – the units
could never start the worker daemons), the set is built from a copy with exactly the documented fix applied
(``worker.<kind>.__main__:main``) and ``test_this_checkout_builds_a_deployable_release`` fails with the build error. All
other deployment tests then still exercise the real build script, playbooks and roles.

Then: contents and checksums of the release, an offline install into a fresh venv of ``python:3.13-slim-trixie``
(network none, ``--require-hashes``), the shipped migrations up and down on a real PostgreSQL, and the script's refusals.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tomllib
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "scripts" / "deploy" / "build-release.sh"
TRIXIE_PY = "docker.io/library/python:3.13-slim-trixie"
NODE_BIN = Path("/opt/node22/bin")
V1, V2, V3 = "0.1.0rc1", "0.1.0rc2", "0.1.0rc3"
CACHE_NAME = "hermclaw-deploy-releases"
SOURCE_PARTS = ["pyproject.toml", "README.md", "alembic.ini", "migrations", "hermclaw", "worker", "config", "ui", "scripts/ops",
                "infra/sandbox-images"]  # fmt: skip
EXTRA_MIGRATION = '''"""deploy test revision: one more table (tests/integration/test_deploy_release.py)."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("deploy_test_marker", sa.Column("id", sa.Integer, primary_key=True))


def downgrade() -> None:
    op.drop_table("deploy_test_marker")
'''


@dataclass
class Releases:
    root: Path
    dist: Path
    wheel_cache: Path
    checkout_defect: str | None  # build error of the unmodified checkout (None = the checkout builds)
    log: str

    def release(self, version: str) -> Path:
        return self.dist / f"release-{version}"

    def meta(self, version: str) -> dict[str, object]:
        return json.loads((self.release(version) / "release.json").read_text(encoding="utf-8"))


def run(argv: list[str], *, cwd: Path = REPO, env: dict[str, str] | None = None, timeout: int = 1800,
        input_text: str | None = None) -> subprocess.CompletedProcess[str]:  # fmt: skip
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False,
                          input=input_text, stdin=subprocess.DEVNULL if input_text is None else None)  # fmt: skip


def build_env() -> dict[str, str]:
    env = dict(os.environ)
    if NODE_BIN.is_dir():
        env["PATH"] = f"{NODE_BIN}{os.pathsep}{env.get('PATH', '')}"
    return env


def broken_entry_points(source: Path) -> dict[str, str]:
    """Console scripts of pyproject.toml whose ``module:function`` does not exist in the sources (static check)."""
    scripts = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"].get("scripts", {})
    broken = {}
    for name, target in scripts.items():
        module, _, func = target.partition(":")
        path = source.joinpath(*module.split("."))
        file = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
        defined = file.is_file() and any(
            isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == func
            for n in ast.parse(file.read_text(encoding="utf-8")).body
        )
        if not defined:
            broken[name] = target
    return broken


def copy_source(dest: Path) -> Path:
    dest.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", "dist", "test-results", "*.egg-info", ".runtime")
    for part in SOURCE_PARTS:
        src = REPO / part
        if src.is_dir():
            shutil.copytree(src, dest / part, ignore=ignore, symlinks=True)
        else:
            (dest / part).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / part)
    return dest


def fix_entry_points(source: Path, broken: dict[str, str]) -> None:
    """The fix reported as shared change: ``worker.<kind>.app:main`` -> ``worker.<kind>.__main__:main``."""
    text = (source / "pyproject.toml").read_text(encoding="utf-8")
    for target in broken.values():
        module = target.partition(":")[0]
        package = module.rsplit(".", 1)[0]
        assert (source.joinpath(*package.split(".")) / "__main__.py").is_file(), f"no known fix for {target}"
        text = text.replace(f'"{target}"', f'"{package}.__main__:main"')
    (source / "pyproject.toml").write_text(text, encoding="utf-8")
    assert not broken_entry_points(source)


def build(*args: str) -> subprocess.CompletedProcess[str]:
    return run([str(BUILD), *args], env=build_env(), timeout=3600)


def _build_set(root: Path) -> Releases:
    dist, cache = root / "dist", root / "wheels"
    log: list[str] = []
    defect = None
    source = REPO
    ui_args = []  # default: the real UI build (npm ci && npm run build)
    if not (shutil.which("npm", path=build_env()["PATH"]) and shutil.which("node", path=build_env()["PATH"])):
        if not (REPO / "ui" / "dist" / "index.html").is_file():
            pytest.skip("neither Node.js >= 22.12 nor a prebuilt ui/dist is available for the UI payload")
        ui_args = ["--ui-dist", str(REPO / "ui" / "dist")]
    broken = broken_entry_points(REPO)
    if broken:
        res = build("--out", str(root / "checkout-dist"), "--wheel-cache", str(cache), *ui_args)
        defect = f"scripts/deploy/build-release.sh of the checkout: rc={res.returncode}\n{res.stderr[-3000:]}"
        source = copy_source(root / "src-fixed")
        fix_entry_points(source, broken)
        log.append(f"checkout defect {broken} - building from {source} with the documented fix")
    res = build("--source", str(source), "--out", str(dist), "--wheel-cache", str(cache), *ui_args)
    assert res.returncode == 0, f"build of {V1} failed:\n{res.stdout[-2000:]}\n{res.stderr[-6000:]}"
    log.append(res.stderr)
    ui = root / "ui-v1"
    ui.mkdir()
    with tarfile.open(dist / f"release-{V1}" / f"hermclaw-ui-{V1}.tar.gz") as tf:
        tf.extractall(ui, filter="data")
    res = build("--source", str(source), "--out", str(dist), "--offline-from", str(cache), "--ui-dist", str(ui), "--version", V2)
    assert res.returncode == 0, f"offline build of {V2} failed:\n{res.stderr[-6000:]}"
    log.append(res.stderr)
    src3 = root / "src-v3"
    shutil.copytree(source, src3, symlinks=True) if source != REPO else copy_source(src3)
    (src3 / "migrations" / "versions" / "20261014_0003_deploy_test_marker.py").write_text(EXTRA_MIGRATION, encoding="utf-8")
    res = build("--source", str(src3), "--out", str(dist), "--offline-from", str(cache), "--ui-dist", str(ui), "--version", V3)
    assert res.returncode == 0, f"offline build of {V3} failed:\n{res.stderr[-6000:]}"
    log.append(res.stderr)
    releases = Releases(root=root, dist=dist, wheel_cache=cache, checkout_defect=defect, log="\n".join(log))
    (root / "releases.json").write_text(json.dumps({"checkout_defect": defect, "log": releases.log}), encoding="utf-8")
    return releases


def releases_for(tmp_path_factory: pytest.TempPathFactory) -> Releases:
    """Build the release set once per session (the result is cached below the session's base temp directory)."""
    if shutil.which("python3.13") is None:
        pytest.skip("python3.13 not installed (the release targets Debian 13's CPython 3.13)")
    root = tmp_path_factory.getbasetemp() / CACHE_NAME
    done = root / "releases.json"
    if done.is_file():
        data = json.loads(done.read_text(encoding="utf-8"))
        return Releases(
            root=root, dist=root / "dist", wheel_cache=root / "wheels", checkout_defect=data["checkout_defect"], log=data["log"]
        )
    if root.exists():
        shutil.rmtree(root)  # an earlier, interrupted attempt of this session
    root.mkdir(parents=True)
    return _build_set(root)


@pytest.fixture(scope="session")
def releases(tmp_path_factory: pytest.TempPathFactory) -> Releases:
    return releases_for(tmp_path_factory)


def _podman(*args: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return run(["podman", *args], cwd=Path("/"), timeout=timeout)


def _need_image(image: str) -> None:
    if os.geteuid() != 0 or shutil.which("podman") is None:
        pytest.skip("needs root and podman")
    if _podman("image", "exists", image).returncode != 0:
        pytest.skip(f"{image} not available locally")


# --------------------------------------------------------------------------------------------- the checkout


def test_this_checkout_builds_a_deployable_release(releases: Releases) -> None:
    """Canary for the release of the checkout itself (see the module docstring)."""
    assert releases.checkout_defect is None, (
        "the checkout does not build a deployable release - fix pyproject.toml [project.scripts] "
        "(hermclaw-exec-worker = worker.execution.__main__:main, hermclaw-model-worker = worker.model.__main__:main):\n"
        + releases.checkout_defect
    )


# --------------------------------------------------------------------------------------------- contents


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_names(tar: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(tar) as tf:
        return {m.name.removeprefix("./"): m for m in tf.getmembers()}


def test_release_layout_metadata_and_checksums(releases: Releases) -> None:
    rel = releases.release(V1)
    meta = releases.meta(V1)
    assert meta["name"] == "hermclaw" and meta["version"] == V1
    py = meta["python"]
    assert isinstance(py, dict) and py["version"] == "3.13" and py["abi"] == "cp313" and py["selftest_python"].startswith("3.13.")
    assert "manylinux_2_41_x86_64" in py["platforms"]
    # every file is listed and verifies (what the deployment's preflight runs on the control node)
    listed = {line.split("  ", 1)[1] for line in (rel / "SHA256SUMS").read_text(encoding="utf-8").splitlines()}
    on_disk = {str(p.relative_to(rel)) for p in rel.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    assert listed == on_disk
    check = run(["sha256sum", "--check", "--quiet", "--strict", "SHA256SUMS"], cwd=rel)
    assert check.returncode == 0, check.stdout + check.stderr
    # LiteLLM: the litellm-proxy extra of pyproject.toml is the single source of truth
    extra = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]["litellm-proxy"]
    llm = meta["litellm"]
    assert isinstance(llm, dict) and llm["requirements"] == extra
    pin = next(r for r in extra if r.startswith("litellm"))
    assert llm["version"] == pin.split("==", 1)[1]
    assert llm["venv_name"] == f"venv-{llm['version']}-{llm['lock_sha256'][:12]}"
    # Alembic head of the shipped migrations = the source's head
    from hermclaw.persistence.migrate import head_revision

    assert meta["alembic_head"] == head_revision()
    assert set(meta["entry_points"]) >= {"hermclaw", "hermclaw-exec-worker", "hermclaw-model-worker"}
    assert meta["sandbox_toolchains"] == ["generic", "node", "php", "python"]
    assert sorted(p.name for p in (rel / "config").iterdir()) == sorted(p.name for p in (REPO / "config").glob("*.example.yaml"))
    for script in ("hermclaw-backup", "hermclaw-build-sandbox-images", "hermclaw-model-pull", "hermclaw-ssh-gate"):
        assert os.access(rel / "scripts" / "ops" / script, os.X_OK), script
    assert (rel / "constraints.txt").is_file() and (rel / "litellm-constraints.txt").is_file()
    # tree-sitter grammars (research 027 Y6): the languages the runtime parses, for the locked language pack
    from hermclaw.repo_intelligence.symbols import _TS_LANGS

    pins = dict(ln.split("==", 1) for ln in (rel / "constraints.txt").read_text(encoding="utf-8").splitlines())
    assert meta["tree_sitter"] == {
        "package_version": pins["tree-sitter-language-pack"],
        "languages": sorted(set(_TS_LANGS.values())),
        "cache_dir": "tree-sitter",
    }


@pytest.mark.parametrize("payload", ["runtime", "litellm"])
def test_wheelhouse_payloads_are_hash_locked_for_cpython_313(releases: Releases, payload: str, tmp_path: Path) -> None:
    meta = releases.meta(V1)
    info = meta[payload]
    assert isinstance(info, dict)
    tar = releases.release(V1) / str(info["payload"])
    members = _payload_names(tar)
    for name, m in members.items():
        assert not name.startswith("/") and ".." not in name.split("/"), name
        assert m.uid == 0 and m.gid == 0 and m.mode & 0o022 == 0, (name, m.uid, oct(m.mode))
    with tarfile.open(tar) as tf:
        tf.extractall(tmp_path, filter="data")
    check = run(["sha256sum", "--check", "--quiet", "--strict", "SHA256SUMS"], cwd=tmp_path)
    assert check.returncode == 0, check.stdout + check.stderr
    wheels = {p.name: p for p in (tmp_path / "wheelhouse").glob("*.whl")}
    assert len(wheels) == info["distributions"], "one wheel per distribution"
    lock = [ln for ln in (tmp_path / "requirements.lock").read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    hashes = {h for ln in lock for h in re.findall(r"--hash=sha256:([0-9a-f]{64})", ln)}
    assert all("==" in ln and "--hash=sha256:" in ln for ln in lock)
    assert hashes == {_sha256(p) for p in wheels.values()}, "every wheel is pinned by its hash, nothing else"
    assert info["lock_sha256"] == _sha256(tmp_path / "requirements.lock")
    pins = {ln.split("==", 1)[0].split("[", 1)[0] for ln in lock}
    assert pins == {ln.split("==", 1)[0] for ln in (tmp_path / "constraints.txt").read_text(encoding="utf-8").splitlines()}
    for name in wheels:
        python_tag, abi_tag, platform_tag = name[:-4].split("-")[-3:]
        assert abi_tag in {"cp313", "abi3", "none"} and ("cp313" in python_tag or abi_tag != "cp313"), name
        for tag in platform_tag.split("."):
            m = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", tag)
            assert (
                tag == "any"
                or tag.startswith(("manylinux1", "manylinux2010", "manylinux2014"))
                or (m and (int(m[1]), int(m[2])) <= (2, 41))
            ), name
    if payload == "runtime":
        own = wheels[f"hermclaw-{V1}-py3-none-any.whl"]
        files = zipfile.ZipFile(own).namelist()
        data = f"hermclaw-{V1}.data/data/share/hermclaw/"
        assert data + "alembic.ini" in files and data + "migrations/env.py" in files
        assert any(f.startswith(data + "migrations/versions/") for f in files)
        assert not any("__pycache__" in f or f.startswith(("tests/", "build/")) for f in files)
        assert (tmp_path / "release.json").read_text(encoding="utf-8") == (releases.release(V1) / "release.json").read_text(
            encoding="utf-8"
        )
        assert sorted(p.name for p in (tmp_path / "sandbox-images").iterdir() if p.is_dir()) == ["generic", "node", "php", "python"]
        ts = meta["tree_sitter"]
        assert isinstance(ts, dict)
        grammars = tmp_path / str(ts["cache_dir"]) / "tree-sitter-language-pack" / f"v{ts['package_version']}"
        shipped = sorted(str(p.relative_to(grammars)) for p in grammars.rglob("*") if p.is_file())
        assert shipped == ["libs/" + f"libtree_sitter_{lang}.so" for lang in ts["languages"]] + ["manifest.json"], (
            "only the grammars and their manifest (no 23 MB download bundle, no lock file)"
        )
    else:
        assert any(n.startswith("litellm-") for n in wheels) and not any(
            n.rsplit("/", 1)[-1] == "litellm_init.pth" for w in wheels.values() for n in zipfile.ZipFile(w).namelist()
        )


def test_ui_payload_is_the_csp_compatible_build(releases: Releases, tmp_path: Path) -> None:
    meta = releases.meta(V1)
    ui = meta["ui"]
    assert isinstance(ui, dict)
    with tarfile.open(releases.release(V1) / str(ui["payload"])) as tf:
        tf.extractall(tmp_path, filter="data")
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert '<div id="root">' in index and re.search(r'<script type="module" crossorigin src="/assets/[^"]+\.js">', index)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", index) and "<style" not in index and "style=" not in index
    assert list((tmp_path / "assets").glob("*.js")) and list((tmp_path / "assets").glob("*.css"))
    if ui["source"] == "npm run build":
        assert str(ui["node"]).startswith("v")


def test_rehearsal_versions_differ_only_in_version_and_schema(releases: Releases) -> None:
    v1, v2, v3 = (releases.meta(v) for v in (V1, V2, V3))
    assert (v2["version"], v3["version"]) == (V2, V3)
    assert v1["alembic_head"] == v2["alembic_head"] != v3["alembic_head"] == "0003"
    llm = [{k: v for k, v in m["litellm"].items() if k != "payload"} for m in (v1, v2, v3)]  # type: ignore[union-attr]
    assert llm[0] == llm[1] == llm[2], "same LiteLLM lock: the LiteLLM venv is shared between the releases"
    assert v2["ui"]["source"] == "prebuilt"


# --------------------------------------------------------------------------------------------- install on Debian 13


def test_release_installs_offline_on_debian_13_python_313(releases: Releases) -> None:
    _need_image(TRIXIE_PY)
    rel = releases.release(V1)
    meta = releases.meta(V1)
    name = f"hcl-release-{uuid.uuid4().hex[:8]}"
    script = f"""set -euo pipefail
grep -q 'VERSION_CODENAME=trixie' /etc/os-release
mkdir -p /opt/payload && tar -C /opt/payload --touch -xf /release/{meta["runtime"]["payload"]}
cd /opt/payload && sha256sum --check --quiet --strict SHA256SUMS
python3 -m venv /opt/hermclaw/venv-{V1}
/opt/hermclaw/venv-{V1}/bin/pip install --quiet --no-index --find-links /opt/payload/wheelhouse --only-binary=:all: \\
    --no-cache-dir --require-hashes -r /opt/payload/requirements.lock
/opt/hermclaw/venv-{V1}/bin/pip check
/opt/hermclaw/venv-{V1}/bin/hermclaw --version
/opt/hermclaw/venv-{V1}/bin/python -I -c '
import importlib.metadata as md, sys
eps = {{e.name: e for e in md.distribution("hermclaw").entry_points if e.group == "console_scripts"}}
for n in sys.argv[1:]:
    eps[n].load()
from hermclaw.persistence.migrate import head_revision, migrations_root
print("head", head_revision(), migrations_root())
print("python", sys.version.split()[0])
' {" ".join(meta["entry_points"])}
HERMCLAW_CONFIG_DIR=/release/config /opt/hermclaw/venv-{V1}/bin/hermclaw config-check > /tmp/check.json
python3 -c 'import json; d = json.load(open("/tmp/check.json")); print("config", d["version"], len(d["hosts"]), sorted(d["model_profiles"]))'
# the shipped grammars serve the structure index offline, read-only, for an unprivileged service user
chmod -R a-w /opt/payload/tree-sitter
setpriv --reuid=65534 --regid=65534 --clear-groups env HOME=/nonexistent \
    TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=/opt/payload/tree-sitter /opt/hermclaw/venv-{V1}/bin/python -I -c '
from hermclaw.repo_intelligence.symbols import _parser, _TS_LANGS
for lang in sorted(set(_TS_LANGS.values())):
    root = _parser(lang).parse(b"<?php function f() {{}}" if lang == "php" else b"function f() {{}}").root_node
    print("grammar", lang, root.type, root.has_error)
'
"""
    res = _podman("run", "--rm", "--name", name, "--network=none", "-v", f"{rel}:/release:ro", TRIXIE_PY, "bash", "-c", script)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]
    out = res.stdout
    assert f"hermclaw {V1}" in out
    assert f"head {meta['alembic_head']} /opt/hermclaw/venv-{V1}/share/hermclaw" in out
    assert re.search(r"^python 3\.13\.\d+$", out, re.M)
    roles = re.search(r"^config (\S+) (\d+) (.*)$", out, re.M)
    assert roles and roles[1] == V1 and int(roles[2]) >= 6
    for alias in ("planner-gemma", "coder-main", "heavy-review", "fast-router", "embedding"):
        assert alias in roles[3]
    grammars = re.findall(r"^grammar (\S+) (\S+) (\S+)$", out, re.M)
    assert grammars == [(lang, "program", "False") for lang in meta["tree_sitter"]["languages"]], out[-2000:]


def test_release_migrations_upgrade_and_downgrade_on_postgresql(releases: Releases, tmp_path: Path) -> None:
    """The schema rule of the rollback: the newer release's shipped migrations downgrade to the older head."""
    from tests.conftest import ADMIN_URL, DB_AVAILABLE

    if not DB_AVAILABLE:
        pytest.skip("PostgreSQL not available (HERMCLAW_TEST_ADMIN_URL)")
    import psycopg

    meta = releases.meta(V3)
    payload = tmp_path / "payload"
    payload.mkdir()
    with tarfile.open(releases.release(V3) / str(meta["runtime"]["payload"])) as tf:
        tf.extractall(payload, filter="data")
    venv = tmp_path / "venv"
    assert run(["python3.13", "-m", "venv", str(venv)]).returncode == 0
    res = run([str(venv / "bin" / "pip"), "install", "--quiet", "--no-index", "--find-links", str(payload / "wheelhouse"),
               "--only-binary=:all:", "--require-hashes", "-r", str(payload / "requirements.lock")], timeout=900)  # fmt: skip
    assert res.returncode == 0, res.stderr[-3000:]
    db = f"hermclaw_deploy_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db}"')
    url = ADMIN_URL.rsplit("/", 1)[0].replace("postgresql://", "postgresql+psycopg://", 1) + f"/{db}"
    env = {**os.environ, "HERMCLAW_DATABASE_URL": url, "HERMCLAW_ENV": "test", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("HERMCLAW_MIGRATIONS_DIR", None)
    ini = venv / "share" / "hermclaw" / "alembic.ini"

    def revision() -> str | None:
        with psycopg.connect(ADMIN_URL.rsplit("/", 1)[0] + f"/{db}") as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            return row[0] if row else None

    try:
        up = run([str(venv / "bin" / "hermclaw"), "migrate"], env=env, cwd=tmp_path, timeout=600)
        assert up.returncode == 0, up.stdout + up.stderr
        assert "database migrated to head (0003)" in up.stdout and revision() == "0003"
        down = run([str(venv / "bin" / "alembic"), "-c", str(ini), "downgrade", releases.meta(V1)["alembic_head"]], env=env, cwd=tmp_path)
        assert down.returncode == 0, down.stdout + down.stderr
        assert revision() == releases.meta(V1)["alembic_head"]
        again = run([str(venv / "bin" / "hermclaw"), "migrate"], env=env, cwd=tmp_path, timeout=600)
        assert again.returncode == 0 and revision() == "0003", again.stderr
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (db,))
            conn.execute(f'DROP DATABASE IF EXISTS "{db}"')


# --------------------------------------------------------------------------------------------- refusals


@pytest.fixture
def ui_bundle(tmp_path: Path) -> Iterator[Path]:
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "assets" / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    (ui / "index.html").write_text(
        '<!doctype html><script type="module" src="/assets/app.js"></script><div id="root"></div>\n', encoding="utf-8"
    )
    yield ui


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--version", "0.1.0-rc1"], "not a canonical PEP 440 version"),
        (["--version", "1.0 ; rm"], "not a canonical PEP 440 version"),
        (["--source", "/etc"], "is not a Hermclaw checkout"),
        (["--offline-from", "/nonexistent/wheels"], "no such directory"),
        (["--constraints", "/nonexistent/constraints.txt"], "no such file"),
        (["--bogus"], "unknown argument"),
        (["--python", "/usr/bin/python3.12"], "is not CPython 3.13"),
    ],
)
def test_build_rejects_bad_input(args: list[str], message: str, tmp_path: Path, ui_bundle: Path) -> None:
    if shutil.which("python3.13") is None:
        pytest.skip("python3.13 not installed")
    if args[0] == "--python" and not Path(args[1]).exists():
        pytest.skip(f"{args[1]} not installed")
    res = build("--out", str(tmp_path / "dist"), "--ui-dist", str(ui_bundle), *args)
    assert res.returncode in (2, 69) and message in res.stderr, res.stderr
    assert not (tmp_path / "dist").exists() or not list((tmp_path / "dist").glob("release-*"))


@pytest.mark.parametrize(
    "index",
    [
        "<!doctype html><script>alert(1)</script>",
        '<!doctype html><script src="/assets/a.js">alert(1)</script>',
        "<!doctype html><style>body{}</style>",
        '<!doctype html><div style="color:red"></div>',
        '<!doctype html><img src="/x.png" onerror="alert(1)">',
    ],
)
def test_build_refuses_a_ui_bundle_the_csp_would_break(index: str, tmp_path: Path, ui_bundle: Path) -> None:
    if shutil.which("python3.13") is None:
        pytest.skip("python3.13 not installed")
    (ui_bundle / "index.html").write_text(index, encoding="utf-8")
    res = build("--out", str(tmp_path / "dist"), "--ui-dist", str(ui_bundle))
    assert res.returncode == 2 and "Content-Security-Policy" in res.stderr, res.stderr


def test_build_refuses_a_release_whose_entry_points_cannot_load(releases: Releases, tmp_path: Path, ui_bundle: Path) -> None:
    src = copy_source(tmp_path / "src")
    if broken_entry_points(src):
        fix_entry_points(src, broken_entry_points(src))
    py = src / "pyproject.toml"
    py.write_text(py.read_text(encoding="utf-8").replace("[project.scripts]\n", '[project.scripts]\nhermclaw-broken = "hermclaw.cli:nope"\n'),
                  encoding="utf-8")  # fmt: skip
    res = build(
        "--source", str(src), "--out", str(tmp_path / "dist"), "--offline-from", str(releases.wheel_cache), "--ui-dist", str(ui_bundle)
    )
    assert res.returncode == 1, res.stderr[-3000:]
    assert "hermclaw-broken = hermclaw.cli:nope" in res.stderr and "entry point check failed" in res.stderr
    assert not list((tmp_path / "dist").glob("release-*")), "nothing is published"


def test_offline_build_refuses_a_wheel_mirror_without_the_grammars(releases: Releases, tmp_path: Path, ui_bundle: Path) -> None:
    """An air-gapped build cannot fetch the tree-sitter grammars: it stops instead of shipping a release that would
    download native code on the hosts at run time (research 027 Y6)."""
    src = copy_source(tmp_path / "src")
    if broken_entry_points(src):
        fix_entry_points(src, broken_entry_points(src))
    mirror = tmp_path / "wheels"
    mirror.mkdir()
    for wheel in releases.wheel_cache.glob("*.whl"):
        (mirror / wheel.name).symlink_to(wheel)
    assert (releases.wheel_cache / "tree-sitter-language-pack").is_dir(), "the --wheel-cache build stored the grammars"
    res = build("--source", str(src), "--out", str(tmp_path / "dist"), "--offline-from", str(mirror), "--ui-dist", str(ui_bundle))
    assert res.returncode == 1, res.stderr[-3000:]
    assert "offline build: no tree-sitter grammars tree-sitter-language-pack/v" in res.stderr, res.stderr[-3000:]
    assert not list((tmp_path / "dist").glob("release-*")), "nothing is published"
