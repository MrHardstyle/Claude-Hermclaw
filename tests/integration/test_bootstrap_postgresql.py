"""P01 1.7 / 1.8 / 1.15 – the postgresql role against a real PostgreSQL server.

A throw-away cluster (the locally installed PostgreSQL major, run as the ``postgres`` OS user
on a free port with a private socket directory) is configured with the role's *rendered*
``conf.d/hermclaw.conf`` and ``pg_hba.conf``. Then the role's real task files run against it:

* ``database.yml`` creates the role (CREATEDB for the restore test), the database, the ``vector`` extension and the
  restore-test template (cloned by the service role like the restore test does), a second run
  reports ``changed=0``, and the active pgvector version is asserted (the local pgvector is
  older than 0.8.2, so the pgvector minimum (0.8.7, floor 0.8.2) must make the role fail – negative control);
* identifiers that need quoting are created verbatim (``format(%I)`` / psql variables, no
  string-built SQL);
* the server accepts both files without errors (``pg_hba_file_rules`` / ``pg_file_settings``),
  listens on localhost only, and the role has no password (peer authentication only).

``verify_pgvector.yml`` is exercised with a fake ``dpkg-query`` for Debian version strings
(epochs, revisions, multi-digit minors). Everything that needs the PGDG repository, systemd
or ``pg_createcluster`` on Debian 13 is BLOCKER-001 (live run on .225).
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.integration.test_bootstrap_infra import ANSIBLE_PLAYBOOK, GROUP_VARS, ROLES_DIR, _ansible_env, _render, _render_vars, _run
from tests.integration.test_bootstrap_roles import Case, _fake_tool, _run_cases

pytestmark = pytest.mark.integration


def _pg_bindir() -> Path | None:
    pg_config = shutil.which("pg_config")
    if pg_config is None:
        return None
    res = subprocess.run([pg_config, "--bindir"], capture_output=True, text=True, check=False)
    bindir = Path(res.stdout.strip())
    return bindir if (bindir / "initdb").is_file() and (bindir / "pg_ctl").is_file() else None


def _as_postgres(*argv: str, timeout: int = 120, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["runuser", "-u", "postgres", "--", *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        input=input_text,
        stdin=subprocess.DEVNULL if input_text is None else None,
        cwd="/",
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Cluster:
    def __init__(self, root: Path, bindir: Path, port: int) -> None:
        self.root, self.bindir, self.port = root, bindir, port
        self.data = root / "data"
        self.sock = root / "sock"

    def psql(self, sql: str, db: str = "postgres") -> str:
        res = _as_postgres(
            "psql", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", "-h", str(self.sock), "-p", str(self.port), "-d", db, "-c", sql
        )
        assert res.returncode == 0, res.stderr
        return res.stdout.strip()


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    bindir = _pg_bindir()
    if bindir is None or os.geteuid() != 0 or shutil.which("runuser") is None:
        pytest.skip("needs root, runuser and a local PostgreSQL server installation (pg_config --bindir)")
    try:
        subprocess.run(["id", "postgres"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pytest.skip("no postgres OS user")
    # rendered with the role's templates and the example inventory (production values)
    tmp = tmp_path_factory.mktemp("pg-render")
    rendered = _render(tmp, tmp / "out", _render_vars())
    root = Path(tempfile.mkdtemp(prefix="hermclaw-pg-", dir="/tmp"))
    port = _free_port()
    pg = Cluster(root, bindir, port)
    try:
        pg.sock.mkdir()
        shutil.chown(root, "postgres", "postgres")
        shutil.chown(pg.sock, "postgres", "postgres")
        init = _as_postgres(str(bindir / "initdb"), "-D", str(pg.data), "-E", "UTF8", "--locale=C.UTF-8", "-A", "peer", "--no-sync")
        assert init.returncode == 0, init.stderr
        confd = pg.data / "conf.d"
        confd.mkdir()
        shutil.copy(rendered["postgresql/hermclaw.conf"], confd / "hermclaw.conf")
        shutil.copy(rendered["postgresql/pg_hba.conf"], pg.data / "pg_hba.conf")
        with (pg.data / "postgresql.conf").open("a", encoding="utf-8") as fh:
            fh.write("\ninclude_dir = 'conf.d'\n")  # what the Debian packaging does for /etc/postgresql/<v>/<c>/conf.d
        for path in (confd, confd / "hermclaw.conf", pg.data / "pg_hba.conf", pg.data / "postgresql.conf"):
            shutil.chown(path, "postgres", "postgres")
        # only the port, socket directory and memory are overridden; everything else is the rendered file
        opts = f"-p {port} -k {pg.sock} -c shared_buffers=128MB"
        start = _as_postgres(str(bindir / "pg_ctl"), "-D", str(pg.data), "-o", opts, "-l", str(root / "server.log"), "-w", "start")
        assert start.returncode == 0, start.stderr + (root / "server.log").read_text(errors="replace")[-2000:]
        yield pg
    finally:
        if pg.data.is_dir():
            _as_postgres(str(bindir / "pg_ctl"), "-D", str(pg.data), "-m", "immediate", "-w", "stop")
        shutil.rmtree(root, ignore_errors=True)


def _database_play(tmp_path: Path, pg: Cluster, overrides: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    """Run roles/postgresql/tasks/database.yml (the real task file) against the cluster."""
    return _role_play(tmp_path, pg, "database.yml", overrides)


def _role_play(tmp_path: Path, pg: Cluster, tasks_from: str, overrides: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    play = [
        {
            "name": tasks_from,
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "become": False,
            "vars_files": [str(GROUP_VARS)],
            "vars": {
                "postgresql_port": pg.port,
                "postgresql_bin_dir": str(pg.bindir),
                "postgresql_conf_dir": str(pg.data),
                "hermclaw_orchestrator_ip": "192.168.178.225",
                **overrides,
            },
            "environment": {"PGHOST": str(pg.sock)},
            "tasks": [{"name": "role tasks", "ansible.builtin.include_role": {"name": "postgresql", "tasks_from": tasks_from}}],
        }
    ]
    tmp_path.mkdir(parents=True, exist_ok=True)
    pb = tmp_path / f"play-{tasks_from}"
    pb.write_text(yaml.safe_dump(play), encoding="utf-8")
    env = _ansible_env(tmp_path)
    env["ANSIBLE_ROLES_PATH"] = str(ROLES_DIR)
    return _run([str(ANSIBLE_PLAYBOOK), "-i", "localhost,", str(pb)], env=env, cwd=tmp_path, timeout=300)


def _recap(stdout: str) -> tuple[int, int, int]:
    m = re.search(r"localhost\s+: ok=(\d+)\s+changed=(\d+)\s+unreachable=\d+\s+failed=(\d+)", stdout)
    assert m, stdout[-2000:]
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def test_server_accepts_rendered_config_and_listens_on_localhost_only(tmp_path: Path, cluster: Cluster) -> None:
    hba_errors = cluster.psql("SELECT line_number || ':' || error FROM pg_hba_file_rules WHERE error IS NOT NULL")
    assert hba_errors == ""
    # the only pg_file_settings "errors" are restart-only values that differ from the running server
    # (port and shared_buffers are overridden on the command line here, like a first run before the
    # restart handler) - the role must not treat them as broken configuration
    pending = cluster.psql(
        "SELECT f.name || '|' || s.context || '|' || f.error FROM pg_file_settings f JOIN pg_settings s USING (name) "
        "WHERE f.error IS NOT NULL ORDER BY 1"
    ).splitlines()
    assert pending == ["port|postmaster|setting could not be applied", "shared_buffers|postmaster|setting could not be applied"]
    res = _role_play(tmp_path, cluster, "validate_config.yml", {})
    assert res.returncode == 0, res.stdout[-3000:]
    assert cluster.psql("SHOW listen_addresses") == "localhost"
    assert cluster.psql("SHOW password_encryption") == "scram-sha-256"
    assert cluster.psql("SHOW idle_in_transaction_session_timeout") == "10min"
    # the settings really come from the Hermclaw drop-in
    sources = cluster.psql("SELECT name FROM pg_settings WHERE sourcefile LIKE '%/conf.d/hermclaw.conf' ORDER BY name").split()
    assert {"listen_addresses", "password_encryption", "log_min_duration_statement", "work_mem"} <= set(sources)
    rules = cluster.psql("SELECT type || ' ' || array_to_string(database, ',') || ' ' || array_to_string(user_name, ',') "
                         "|| ' ' || coalesce(address, '-') || ' ' || auth_method FROM pg_hba_file_rules ORDER BY line_number")  # fmt: skip
    lines = rules.splitlines()
    assert lines[0] == "local all postgres - peer"
    assert all(ln.split()[3] in {"-", "127.0.0.1", "::1"} for ln in lines), lines
    assert not any(ln.endswith(" trust") for ln in lines)


def test_database_tasks_create_role_db_extension_idempotently(tmp_path: Path, cluster: Cluster) -> None:
    available = cluster.psql("SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
    if not available:
        pytest.skip("pgvector is not installed for the local PostgreSQL server")
    # the local pgvector is older than the production minimum: run with the local version as minimum
    local = {"postgresql_pgvector_min_version": available, "postgresql_pgvector_accept_below_recommended": True}
    first = _database_play(tmp_path / "first", cluster, local)
    assert first.returncode == 0, first.stdout[-3000:]
    ok, changed, failed = _recap(first.stdout)
    assert failed == 0 and changed >= 2, first.stdout[-3000:]

    role = cluster.psql(
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolpassword IS NULL "
        "FROM pg_authid WHERE rolname = 'hermclaw'"
    )
    assert role == "t|f|t|f|f|t", "login role with CREATEDB only (restore test) and without a password (peer only)"
    assert cluster.psql("SELECT pg_get_userbyid(datdba) || ' ' || pg_encoding_to_char(encoding) FROM pg_database "
                        "WHERE datname = 'hermclaw'") == "hermclaw UTF8"  # fmt: skip
    assert cluster.psql("SELECT extversion FROM pg_extension WHERE extname = 'vector'", db="hermclaw") == available
    # restore-test template: owned by postgres, a template that accepts no connections
    assert cluster.psql("SELECT pg_get_userbyid(datdba) || ' ' || datistemplate || ' ' || datallowconn FROM pg_database "
                        "WHERE datname = 'hermclaw_restore_template'") == "postgres true false"  # fmt: skip
    # what the restore test does as the service role: clone the template and find the extension in the clone
    clone = _as_postgres("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", str(cluster.sock), "-p", str(cluster.port), "-d", "postgres",
                         "-c", "SET ROLE hermclaw", "-c", "CREATE DATABASE hermclaw_restoretest_ci TEMPLATE hermclaw_restore_template")  # fmt: skip
    assert clone.returncode == 0, clone.stderr
    try:
        assert cluster.psql("SELECT extversion FROM pg_extension WHERE extname = 'vector'", db="hermclaw_restoretest_ci") == available
        assert cluster.psql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'hermclaw_restoretest_ci'") == "hermclaw"
    finally:
        cluster.psql("DROP DATABASE IF EXISTS hermclaw_restoretest_ci")

    second = _database_play(tmp_path / "second", cluster, local)
    assert second.returncode == 0, second.stdout[-3000:]
    assert _recap(second.stdout)[1:] == (0, 0), "second run must not change anything"


def test_database_tasks_refuse_pgvector_below_the_cve_minimum(tmp_path: Path, cluster: Cluster) -> None:
    available = cluster.psql("SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
    if not available or not re.match(r"^0\.([0-7]\.|8\.[01]$)", available):
        pytest.skip(f"local pgvector {available or 'missing'} is not older than 0.8.2 - nothing to refuse")
    res = _database_play(tmp_path, cluster, {})  # production default: >= 0.8.7 (CVE-2026-103484)
    assert res.returncode != 0
    assert f"extension vector {available} < 0.8.7" in res.stdout
    floor = _database_play(tmp_path / "floor", cluster, {"postgresql_pgvector_accept_below_recommended": True})
    assert floor.returncode != 0 and f"extension vector {available} < 0.8.2" in floor.stdout, "the 0.8.2 floor stays"


def test_database_tasks_quote_identifiers(tmp_path: Path, cluster: Cluster) -> None:
    """Names that need quoting are created verbatim - psql variables + format(%I), no string-built SQL."""
    owner, db = "hermclaw-ci Owner", "hermclaw-ci db"
    res = _database_play(tmp_path, cluster, {"postgresql_db_owner": owner, "postgresql_db_name": db, "postgresql_extensions": []})
    assert res.returncode == 0, res.stdout[-3000:]
    assert cluster.psql(f"SELECT count(*) FROM pg_roles WHERE rolname = '{owner}'") == "1"
    assert cluster.psql(f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '{db}'") == owner
    # a hostile name stays one identifier (and validate.yml refuses it before it gets here)
    evil = "x'; CREATE ROLE pwned SUPERUSER; --"
    res = _database_play(
        tmp_path / "evil", cluster, {"postgresql_db_owner": evil, "postgresql_db_name": "hermclaw_evil", "postgresql_extensions": []}
    )
    assert res.returncode == 0, res.stdout[-3000:]
    assert cluster.psql("SELECT count(*) FROM pg_roles WHERE rolname = 'pwned'") == "0"
    assert cluster.psql("SELECT count(*) FROM pg_roles WHERE rolname = $$" + evil + "$$") == "1"


# --------------------------------------------------------------------------------------------- dpkg version check

DPKG_QUERY = """case "$*" in
  *postgresql-17-pgvector*) if [ -n "$FAKE_PGVECTOR" ]; then printf '%s' "$FAKE_PGVECTOR"; else
      echo "dpkg-query: no packages found matching postgresql-17-pgvector" >&2; exit 1; fi ;;
  *) exit 1 ;;
esac
"""


def test_verify_pgvector_package_versions(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    _fake_tool(bindir, "dpkg-query", DPKG_QUERY)
    accept: dict[str, object] = {"postgresql_pgvector_accept_below_recommended": True}
    versions: dict[str, tuple[str, dict[str, object], str]] = {
        "pgdg-fixed": ("0.8.7-1.pgdg13+1", {}, "PASS"),
        "pgdg-newer-minor": ("0.10.0-1.pgdg13+1", {}, "PASS"),  # numeric, not string comparison
        "epoch": ("1:0.8.7-2", {}, "PASS"),
        "cve-2026-103484": ("0.8.6-1.pgdg13+1", {}, "FAIL"),  # IVFFlat build OOB write (default refuses)
        "accepted-below-recommended": ("0.8.6-1.pgdg13+1", accept, "PASS"),
        "accepted-floor": ("0.8.2-1.pgdg13+1", accept, "PASS"),
        "debian-vulnerable": ("0.8.0-1", accept, "FAIL"),  # trixie's own package (CVE-2026-3172): never
        "one-below-floor": ("0.8.1-1.pgdg13+1", accept, "FAIL"),
        "epoch-vulnerable": ("2:0.7.4-1", accept, "FAIL"),
        "not-installed": ("", {}, "FAIL"),
    }
    cases: list[Case] = [
        (f"pgvector-{name}", "postgresql", "verify_pgvector.yml", extra, {"FAKE_PGVECTOR": version}, want)
        for name, (version, extra, want) in versions.items()
    ]
    _run_cases(tmp_path, cases, bindir=bindir)


# --------------------------------------------------------------------------------------------- config validation + rollback


@pytest.mark.parametrize(
    ("case", "broken_conf", "broken_hba", "message"),
    [
        ("sighup-value", "work_mem = 'lots'\n", None, "work_mem"),
        # restart-only parameter: pg_file_settings only says "could not be applied" - postgres -C must catch it
        ("postmaster-value", "shared_buffers = 'lots'\n", None, "postgres -C"),
        ("unknown-parameter", "shared_bufers = 1GB\n", None, "shared_bufers"),
        ("syntax", "work_mem = = 4MB\n", None, "syntax error"),
        ("hba-method", None, "host all all 127.0.0.1/32 bogus\n", "hba:"),
    ],
)
def test_validate_config_rolls_back_broken_files(
    tmp_path: Path, cluster: Cluster, case: str, broken_conf: str | None, broken_hba: str | None, message: str
) -> None:
    conf = cluster.data / "conf.d" / "hermclaw.conf"
    hba = cluster.data / "pg_hba.conf"
    good_conf, good_hba = conf.read_text(encoding="utf-8"), hba.read_text(encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "hermclaw.conf").write_text(good_conf, encoding="utf-8")
    (backups / "pg_hba.conf").write_text(good_hba, encoding="utf-8")
    try:
        if broken_conf:
            conf.write_text(good_conf + broken_conf, encoding="utf-8")
        if broken_hba:
            hba.write_text(good_hba + broken_hba, encoding="utf-8")
        # what the template tasks registered in this run: changed, with the previous file as backup
        registered = {
            "postgresql_conf": {"changed": bool(broken_conf), "backup_file": str(backups / "hermclaw.conf")},
            "postgresql_hba": {"changed": bool(broken_hba), "backup_file": str(backups / "pg_hba.conf")},
        }
        res = _role_play(tmp_path / "play", cluster, "validate_config.yml", registered)
        assert res.returncode != 0, f"{case}: broken configuration accepted\n{res.stdout[-3000:]}"
        assert "PostgreSQL configuration errors" in res.stdout and message in res.stdout, res.stdout[-3000:]
        assert conf.read_text(encoding="utf-8") == good_conf, "settings drop-in restored"
        assert hba.read_text(encoding="utf-8") == good_hba, "pg_hba.conf restored"
        # and the restored files validate again
        again = _role_play(tmp_path / "again", cluster, "validate_config.yml", {})
        assert again.returncode == 0, again.stdout[-3000:]
    finally:
        conf.write_text(good_conf, encoding="utf-8")
        hba.write_text(good_hba, encoding="utf-8")
        shutil.chown(conf, "postgres", "postgres")
        shutil.chown(hba, "postgres", "postgres")


def test_validate_config_removes_a_new_broken_dropin(tmp_path: Path, cluster: Cluster) -> None:
    conf = cluster.data / "conf.d" / "hermclaw.conf"
    good_conf = conf.read_text(encoding="utf-8")
    try:
        conf.write_text("work_mem = 'lots'\n", encoding="utf-8")
        res = _role_play(tmp_path, cluster, "validate_config.yml", {"postgresql_conf": {"changed": True}})
        assert res.returncode != 0 and "work_mem" in res.stdout
        assert not conf.exists(), "a drop-in without predecessor is removed, the next restart stays safe"
    finally:
        conf.write_text(good_conf, encoding="utf-8")
        shutil.chown(conf, "postgres", "postgres")


GPG = """case "$FAKE_KEYS" in
  missing) echo "gpg: can't open: No such file or directory" >&2; exit 2 ;;
esac
for fpr in $FAKE_KEYS; do
  printf 'pub:-:4096:1:7FCC7D46ACCC4CF8:1308931425:::-:::scESC::::::23::0:\\n'
  printf 'fpr:::::::::%s:\\n' "$fpr"
done
"""
PGDG_FPR = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"


def test_pgdg_keyring_fingerprint_check(tmp_path: Path) -> None:
    """The PGDG keyring from Debian's postgresql-common must hold exactly the published signing key."""
    bindir = tmp_path / "bin"
    _fake_tool(bindir, "gpg", GPG)
    keys = {
        "pgdg": (PGDG_FPR, "PASS"),
        "other-key": ("0123456789ABCDEF0123456789ABCDEF01234567", "FAIL"),
        "extra-key": (f"{PGDG_FPR} 0123456789ABCDEF0123456789ABCDEF01234567", "FAIL"),
        "missing": ("missing", "FAIL"),
    }
    cases: list[Case] = [
        (f"pgdg-key-{name}", "postgresql", "pgdg_key.yml", {}, {"FAKE_KEYS": value}, want) for name, (value, want) in keys.items()
    ]
    _run_cases(tmp_path, cases, bindir=bindir)
