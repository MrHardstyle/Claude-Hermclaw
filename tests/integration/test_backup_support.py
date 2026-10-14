"""Shared helpers for the P33 backup/restore tests (no tests in here).

``make_world`` builds a config dir (with plaintext secrets that must NOT be backed up), an encrypted credstore,
an artifact store and private staging/target/work dirs; ``seed`` adds research records, deployments and events to
the session database of ``tests/conftest.py``; ``start_sshd`` runs a private ``sshd`` on a loopback port whose
backup key is restricted with ``rrsync`` (the production setup of the Terra ``.60`` share, simulated locally).
"""

from __future__ import annotations

import os
import pwd
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from hermclaw.backup.config import BackupConfig, LocalTarget, RestoreTestConfig, RetentionPolicy

ADMIN_URL = os.environ.get("HERMCLAW_TEST_ADMIN_URL", "postgresql://postgres@127.0.0.1:5432/postgres")
SSHD = shutil.which("sshd") or "/usr/sbin/sshd"
RRSYNC = shutil.which("rrsync") or "/usr/bin/rrsync"
TEST_PREFIX = "hcbk_test"
PRIVATE_KEY_TEXT = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ\n-----END OPENSSH PRIVATE KEY-----\n"
PLAINTEXT_PASSWORD = "hunter2-Plaintext-Secret"


def libpq_url(sa_url: str) -> str:
    return sa_url.replace("postgresql+psycopg://", "postgresql://", 1)


@dataclass
class BackupWorld:
    root: Path
    config_dir: Path
    credstore: Path
    artifacts_dir: Path
    staging: Path
    target_dir: Path
    work: Path
    db_url: str

    def config(self, *, target: Any = None, **overrides: Any) -> BackupConfig:
        data: dict[str, Any] = {
            "target": target or LocalTarget(path=self.target_dir),
            "staging_dir": self.staging,
            "database_url": self.db_url,
            "config_dir": self.config_dir,
            "artifacts_dir": self.artifacts_dir,
            "credstore_dirs": [self.credstore],
            "lock_wait_seconds": 0,
            "retention": RetentionPolicy(keep_last=1, keep_daily=7, keep_weekly=4, prune_after_backup=False),
            "restore_test": RestoreTestConfig(admin_url=ADMIN_URL, db_prefix=TEST_PREFIX, work_dir=self.work),
        }
        data.update(overrides)
        return BackupConfig(**data)


def _write(path: Path, text: str | bytes, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def make_world(root: Path, db_url: str) -> BackupWorld:
    cfg = root / "etc-hermclaw"
    # --- regular configuration (must be in the backup)
    _write(cfg / "hosts.yaml", "hosts:\n  - {id: terra-60, address: 192.168.178.60, role: backup}\n")
    _write(cfg / "policies.yaml", "policies:\n  commands: {}\n  smtp:\n    password: cred:smtp-password\n    token: file:/etc/x\n")
    _write(cfg / "models.yaml", "models: []\n# token: ${GITLAB_TOKEN}\n")
    _write(cfg / "ssh" / "known_hosts", "192.168.178.60 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBackupHostKey\n")
    _write(cfg / "ssh" / "backup_ed25519.pub", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPublicPart hermclaw-backup\n")
    _write(cfg / "tls" / "server.crt", "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    _write(cfg / "nested" / "deep" / "limits.yaml", "limits: {cpu: 4}\n")
    # --- plaintext secrets (must NOT be in the backup)
    _write(cfg / "secrets" / "hermclaw-db-password", PLAINTEXT_PASSWORD, 0o600)
    _write(cfg / "ssh" / "id_ed25519", PRIVATE_KEY_TEXT, 0o600)
    _write(cfg / "ssh" / "backup_ed25519", PRIVATE_KEY_TEXT, 0o600)
    _write(cfg / "tls" / "server.pem", "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n", 0o600)
    _write(cfg / "tls" / "server.key", "not even pem\n", 0o600)
    _write(cfg / "gitlab.token", "glpat-abcdefghijklmnopqrstuvwxyz\n", 0o600)
    _write(cfg / "legacy.env", f"DB_PASSWORD={PLAINTEXT_PASSWORD}\n")
    _write(cfg / "notes" / "pasted.txt", "see below\n" + PRIVATE_KEY_TEXT)
    _write(cfg / "tokens-store" / "gl-runner", "opaque-runner-token-value\n", 0o600)
    _write(cfg / "integrations.yaml", f"gitlab:\n  runner_token_ref: file:{cfg / 'tokens-store' / 'gl-runner'}\n")
    (cfg / "passwd-link").symlink_to("/etc/passwd")
    # --- encrypted credential store (binary blobs, included)
    credstore = root / "credstore.encrypted"
    _write(credstore / "hermclaw-db-password", b"\x00\x01Wb\x9c" + os.urandom(200), 0o600)
    _write(credstore / "api-admin-token", b"\x00\x02" + os.urandom(120), 0o600)
    # --- artifact store
    arts = root / "artifacts"
    job = uuid.uuid4().hex
    _write(arts / job / "diff.patch", "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n")
    _write(arts / job / "report.json", '{"ok": true}\n')
    _write(arts / job / "logs" / "pytest.log", "1 passed\n" * 200)
    _write(arts / "blobs" / "model-output.bin", os.urandom(300_000))
    (arts / "empty-dir").mkdir()
    staging = root / "staging"
    target = root / "terra-share"
    target.mkdir()
    work = root / "work"
    return BackupWorld(root, cfg, credstore, arts, staging, target, work, db_url)


async def seed(sessionmaker: Any, *, runs: int = 3) -> dict[str, int]:
    """Research records, deployment manifests and an event (row counts that the backup must reproduce)."""
    from hermclaw.events.store import append_event
    from hermclaw.persistence.models import Deployment, ResearchClaim, ResearchClaimSource, ResearchRun, ResearchSource

    async with sessionmaker() as session:
        for i in range(runs):
            run = ResearchRun(question=f"Which rsync options are safe? #{i}", status="done", queries=["rsync"], synthesis="ok")
            session.add(run)
            await session.flush()
            sources = [
                ResearchSource(research_run_id=run.id, title=f"src {j}", url=f"https://rsync.samba.org/{i}/{j}", domain="rsync.samba.org")
                for j in range(2)
            ]
            session.add_all(sources)
            claim = ResearchClaim(research_run_id=run.id, claim="--partial keeps partial files", confidence=0.9)
            session.add(claim)
            await session.flush()
            session.add(ResearchClaimSource(claim_id=claim.id, source_id=sources[0].id))
        session.add(Deployment(target="terra-60", kind="backup-share", status="done", manifest={"files": ["a", "b"], "note": "ünïcode"}))
        await append_event(session, "test.seed", source_type="test", payload={"n": runs})
        await session.commit()
    return table_counts(sessionmaker_url(sessionmaker))


def sessionmaker_url(sessionmaker: Any) -> str:
    return str(sessionmaker.kw["bind"].url.render_as_string(hide_password=False))


def table_counts(sa_url: str) -> dict[str, int]:
    from hermclaw.backup.pg import _LIST_TABLES_SQL

    with psycopg.connect(libpq_url(sa_url)) as conn:
        tables = conn.execute(_LIST_TABLES_SQL).fetchall()
        return {f"{s}.{t}": int(conn.execute(f'SELECT count(*) FROM "{s}"."{t}"').fetchone()[0]) for s, t in tables}  # type: ignore[index]


def temp_databases(prefix: str = TEST_PREFIX) -> list[str]:
    with psycopg.connect(ADMIN_URL) as conn:
        return [str(r[0]) for r in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (prefix + "%",)).fetchall()]


def events_of(sa_url: str, prefix: str = "backup.") -> list[tuple[str, dict[str, Any]]]:
    with psycopg.connect(libpq_url(sa_url)) as conn:
        rows = conn.execute("SELECT event_type, payload FROM events WHERE event_type LIKE %s ORDER BY sequence", (prefix + "%",)).fetchall()
    return [(str(t), dict(p or {})) for t, p in rows]


def create_database(name: str) -> str:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    base = ADMIN_URL.rsplit("/", 1)[0]
    return f"{base.replace('postgresql://', 'postgresql+psycopg://', 1)}/{name}"


def drop_database(name: str) -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# ================================================================================================ sshd
@dataclass
class Sshd:
    root: Path
    port: int
    user: str
    key: Path  # unrestricted key (full rsync path)
    rrsync_key: Path  # key restricted to rrsync <rrsync_dir>
    rrsync_dir: Path
    known_hosts: Path
    proc: subprocess.Popen[bytes]

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _keygen(path: Path) -> None:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def sshd_available() -> bool:
    return Path(SSHD).is_file() and shutil.which("ssh-keygen") is not None and shutil.which("rsync") is not None


def start_sshd(root: Path) -> Sshd:
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    user = pwd.getpwuid(os.geteuid()).pw_name
    for name in ("host_key", "client_key", "rrsync_key"):
        _keygen(root / name)
    rrsync_dir = root / "rrsync-share"
    rrsync_dir.mkdir()
    (root / "authorized_keys").write_text(
        (root / "client_key.pub").read_text() + f'restrict,command="{RRSYNC} {rrsync_dir}" ' + (root / "rrsync_key.pub").read_text(),
        encoding="utf-8",
    )
    (root / "authorized_keys").chmod(0o600)
    port = _free_port()
    config = root / "sshd_config"
    config.write_text(
        "\n".join(
            [
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {root / 'host_key'}",
                f"AuthorizedKeysFile {root / 'authorized_keys'}",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "PubkeyAuthentication yes",
                "UsePAM no",
                "StrictModes no",
                "PermitRootLogin prohibit-password",
                f"PidFile {root / 'sshd.pid'}",
                "LogLevel VERBOSE",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if os.geteuid() == 0:
        Path("/run/sshd").mkdir(mode=0o755, exist_ok=True)
    log = (root / "sshd.log").open("wb")
    proc = subprocess.Popen([SSHD, "-D", "-e", "-f", str(config)], stdout=log, stderr=log)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"sshd exited: {(root / 'sshd.log').read_text()[-500:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    host_pub = (root / "host_key.pub").read_text().split()
    known_hosts = root / "known_hosts"
    known_hosts.write_text(f"[127.0.0.1]:{port} {host_pub[0]} {host_pub[1]}\n", encoding="utf-8")
    return Sshd(root, port, user, root / "client_key", root / "rrsync_key", rrsync_dir, known_hosts, proc)
