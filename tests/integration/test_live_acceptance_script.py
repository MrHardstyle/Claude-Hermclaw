"""scripts/ops/live-acceptance.sh (BLOCKER-001 follow-up): the parts that run without the LAN.

* ``--prepare-db`` creates a CREATEDB-only login role and a pgvector template database; the throw-away test
  databases of the live suites are created from that template by the non-superuser role, and every migration
  applies (``CREATE EXTENSION vector`` needs a superuser otherwise) – exactly what ``tests/conftest.py`` does with
  ``HERMCLAW_TEST_ADMIN_URL`` + ``HERMCLAW_TEST_TEMPLATE_DB``.
* ``--cleanup-db`` drops the role, the template and leftover test databases.
* argument and precondition errors exit 2; ``--list`` names every live test file, and each of them exists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "live-acceptance.sh"
ADMIN_URL = os.environ.get("HERMCLAW_TEST_ADMIN_URL", "postgresql://postgres@127.0.0.1:5432/postgres")

pytestmark = pytest.mark.integration


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=120, check=False
    )


def _local_superuser() -> bool:
    if os.geteuid() != 0 or not shutil.which("runuser") or not shutil.which("psql"):
        return False
    try:
        with psycopg.connect(ADMIN_URL, connect_timeout=3) as conn:
            return bool(conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0])  # type: ignore[index]
    except Exception:
        return False


def test_list_names_existing_live_test_files() -> None:
    res = _run("--list")
    assert res.returncode == 0, res.stderr
    files = [f for line in res.stdout.splitlines() for f in line.split()[1:]]
    assert len(files) >= 9 and all((ROOT / f).is_file() for f in files)
    live = {
        str(p.relative_to(ROOT))
        for p in (ROOT / "tests").rglob("test_*.py")
        if p != Path(__file__).resolve() and "mark.live" in p.read_text(encoding="utf-8")
    }
    assert live <= set(files), f"live tests missing from the acceptance suites: {live - set(files)}"


def test_usage_errors_exit_2(tmp_path: Path) -> None:
    assert _run("--suite", "bogus").returncode == 2
    assert _run("--frobnicate").returncode == 2
    assert "Usage:" in _run("--help").stdout
    if os.geteuid() == 0:  # no Hermclaw role on this host
        res = _run(env={"HERMCLAW_CONFIG_DIR": str(tmp_path), "HERMCLAW_ACCEPTANCE_USER": "root"})
        assert res.returncode == 2 and "no Hermclaw role" in res.stderr


@pytest.mark.skipif(not _local_superuser(), reason="needs root, runuser, psql and a local PostgreSQL superuser")
def test_prepare_db_lets_a_non_superuser_create_migrated_test_databases(tmp_path: Path) -> None:
    host = psycopg.conninfo.conninfo_to_dict(ADMIN_URL).get("host", "127.0.0.1")
    port = str(psycopg.conninfo.conninfo_to_dict(ADMIN_URL).get("port", "5432"))
    socket_dir = "/tmp" if Path("/tmp/.s.PGSQL." + port).exists() else "/var/run/postgresql"
    env = {"HERMCLAW_CONFIG_DIR": str(tmp_path), "HERMCLAW_ACCEPTANCE_USER": "postgres", "PGHOST": socket_dir, "PGPORT": port}
    env["HERMCLAW_ACCEPTANCE_PGPORT"] = port
    res = _run("--prepare-db", env=env)
    try:
        assert res.returncode == 0, res.stderr
        url_file = tmp_path / "secrets" / "acceptance-test-admin-url"
        assert oct(url_file.stat().st_mode & 0o777) == "0o640"
        assert _run("--prepare-db", env=env).returncode == 0, "idempotent (new password)"
        url = url_file.read_text(encoding="utf-8").strip().replace("@127.0.0.1:", f"@{host}:")
        name = f"hermclaw_test_{uuid.uuid4().hex[:10]}"
        with psycopg.connect(url, autocommit=True) as conn:
            assert conn.execute("SELECT rolsuper, rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone() == (False, True)
            conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "hermclaw_acceptance_tpl"')
        db_url = url.rsplit("/", 1)[0].replace("postgresql://", "postgresql+psycopg://", 1) + f"/{name}"
        mig = subprocess.run(
            [str(ROOT / ".venv" / "bin" / "alembic"), "-c", str(ROOT / "alembic.ini"), "upgrade", "head"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={**os.environ, "HERMCLAW_DATABASE_URL": db_url},
            timeout=300,
            check=False,
        )
        assert mig.returncode == 0, mig.stderr[-3000:]
        with psycopg.connect(url.rsplit("/", 1)[0] + f"/{name}") as conn:
            assert conn.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'").fetchone() == ("vector",)
    finally:
        cleanup = _run("--cleanup-db", env=env)
    assert cleanup.returncode == 0, cleanup.stderr
    with psycopg.connect(ADMIN_URL) as conn:
        assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = 'hermclaw_acceptance'").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM pg_database WHERE datname LIKE 'hermclaw_acceptance%'").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM pg_database WHERE datname = %s", (name,)).fetchone() == (0,)
    assert not (tmp_path / "secrets" / "acceptance-test-admin-url").exists()
