"""P33 disaster-recovery restore (``restore_full``) and the CLI / ops wrappers end to end (real PostgreSQL)."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermclaw.backup import create_backup, restore_full
from hermclaw.backup.errors import BACKUP_VERIFY_FAILED, RESTORE_TARGET_NOT_EMPTY, BackupError
from hermclaw.backup.manifest import CONFIG_TAR, read_manifest
from tests.integration.test_backup_support import (
    BackupWorld,
    create_database,
    drop_database,
    events_of,
    make_world,
    seed,
    table_counts,
    temp_databases,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def world(tmp_path: Path, db_url: str) -> BackupWorld:
    return make_world(tmp_path, db_url)


@pytest.fixture
def empty_db() -> Any:
    name = f"hcbk_restore_{uuid.uuid4().hex[:8]}"
    url = create_database(name)
    yield url
    drop_database(name)


async def test_restore_full_into_empty_database_and_files(world: BackupWorld, sessionmaker: Any, db_url: str, empty_db: str) -> None:
    await seed(sessionmaker, runs=2)
    cfg = world.config()
    made = await create_backup(config=cfg)
    manifest, _ = read_manifest(world.target_dir / made.set_id)

    # a non-empty database is refused (the live Hermclaw DB here) – nothing is touched
    with pytest.raises(BackupError) as err:
        await restore_full(made.set_id, db_url, config=cfg)
    assert err.value.code == RESTORE_TARGET_NOT_EMPTY

    files = world.root / "dr-files"
    report = await restore_full(made.set_id, empty_db, config=cfg, files_dest=files, sessionmaker=sessionmaker)
    assert report.ok, report.failed_checks()
    assert report.kind == "restore_full" and report.rows_restored == made.rows
    restored = table_counts(empty_db)
    assert restored == manifest.database.tables
    assert (files / "config" / "hosts.yaml").is_file() and (files / "records" / "deployments.jsonl").is_file()
    assert (files / "credstore" / "credstore.encrypted" / "api-admin-token").is_file()
    assert not (files / "config" / "secrets").exists() and not (files / "config" / "ssh" / "id_ed25519").exists()
    assert any(p.name == "diff.patch" for p in (files / "artifacts").rglob("*"))
    assert ("backup.restore.completed") in [t for t, _ in events_of(db_url)]

    # the restored database is now non-empty: only with force (--clean --if-exists), files dest must be empty too
    with pytest.raises(BackupError) as err:
        await restore_full(made.set_id, empty_db, config=cfg)
    assert err.value.code == RESTORE_TARGET_NOT_EMPTY
    with pytest.raises(BackupError) as err:
        await restore_full(made.set_id, empty_db, config=cfg, files_dest=files, force=False)
    assert err.value.code == RESTORE_TARGET_NOT_EMPTY
    again = await restore_full(made.set_id, empty_db, config=cfg, force=True)
    assert again.ok, again.failed_checks()
    assert any("--clean" in w for w in again.warnings)
    assert table_counts(empty_db) == manifest.database.tables


async def test_restore_full_refuses_a_corrupted_set(world: BackupWorld, empty_db: str) -> None:
    cfg = world.config()
    made = await create_backup(config=cfg)
    tar = world.target_dir / made.set_id / CONFIG_TAR
    tar.write_bytes(tar.read_bytes()[:-20])
    with pytest.raises(BackupError) as err:
        await restore_full(made.set_id, empty_db, config=cfg)
    assert err.value.code == BACKUP_VERIFY_FAILED
    assert table_counts(empty_db) == {}  # untouched


# ================================================================================================ CLI
def _env(world: BackupWorld, cfg_file: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HERMCLAW_BACKUP_CONFIG": str(cfg_file),
            "HERMCLAW_DATABASE_URL": world.db_url,
            "HERMCLAW_CONFIG_DIR": str(world.config_dir),
            "HERMCLAW_ENV": "test",
            "HERMCLAW_PYTHON": sys.executable,
            "HERMCLAW_LOG_LEVEL": "WARNING",
        }
    )
    return env


def _run(argv: list[str], env: dict[str, str]) -> tuple[int, dict[str, Any], str]:
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=300, check=False)
    try:
        doc = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        pytest.fail(f"stdout is not JSON: {proc.stdout[:500]} / {proc.stderr[-500:]}")
    return proc.returncode, doc, proc.stderr


def _cfg_file(world: BackupWorld, **overrides: Any) -> Path:
    path = world.root / "backup.yaml"
    path.write_text(yaml.safe_dump({"backup": world.config(**overrides).model_dump(mode="json", exclude_none=True)}), encoding="utf-8")
    return path


async def test_cli_and_wrapper_scripts_end_to_end(world: BackupWorld, sessionmaker: Any) -> None:
    await seed(sessionmaker, runs=1)
    env = _env(world, _cfg_file(world))
    backup = str(ROOT / "scripts" / "ops" / "hermclaw-backup")
    rtest = str(ROOT / "scripts" / "ops" / "hermclaw-restore-test")

    code, created, err = _run([backup], env)  # no arguments = create (what the systemd unit runs)
    assert code == 0, err[-1500:]
    set_id = created["set_id"]
    assert created["transfer"]["verified"] is True and (world.target_dir / set_id).is_dir()

    code, listed, _ = _run([backup, "list"], env)
    assert code == 0 and [s["set_id"] for s in listed["sets"]] == [set_id]

    code, verified, _ = _run([sys.executable, "-m", "hermclaw.backup", "verify", "--latest"], env)
    assert code == 0 and verified["ok"] is True and verified["deep"] is True

    code, report, err = _run([rtest], env)  # default: the newest complete set
    assert code == 0, err[-1500:]
    assert report["ok"] is True and report["set_id"] == set_id and report["database_dropped"] is True

    code, pruned, _ = _run([backup, "prune", "--dry-run"], env)
    assert code == 0 and pruned["dry_run"] is True and pruned["keep"] == [set_id]

    # tampered set: verify and restore-test exit 1 with the reason in the JSON document
    tar = world.target_dir / set_id / CONFIG_TAR
    data = bytearray(tar.read_bytes())
    data[50] ^= 0x55
    tar.write_bytes(bytes(data))
    code, verified, _ = _run([backup, "verify", set_id, "--quick"], env)
    assert code == 1 and verified["ok"] is False and any("sha256" in p for p in verified["problems"])
    code, report, _ = _run([rtest, set_id], env)
    assert code == 1 and report["ok"] is False and f"checksum:{CONFIG_TAR}" in [c["name"] for c in report["checks"] if not c["ok"]]
    assert temp_databases() == []

    # restore-full refuses the non-empty live database before it even fetches the set
    code, doc, _ = _run([backup, "restore-full", "--set", set_id, "--target-db-url", world.db_url], env)
    assert code == 3 and doc["error"]["code"] == "RESTORE_TARGET_NOT_EMPTY"
    kinds = [t for t, _ in events_of(world.db_url)]
    assert "backup.completed" in kinds and "backup.restore_test.passed" in kinds and "backup.restore_test.failed" in kinds


async def test_cli_exit_codes_for_config_errors_and_lock(world: BackupWorld) -> None:
    bad = world.root / "bad.yaml"
    bad.write_text("backup:\n  target: {kind: local, path: relative/dir}\n", encoding="utf-8")
    env = _env(world, bad)
    code, doc, _ = _run([sys.executable, "-m", "hermclaw.backup", "create"], env)
    assert code == 2 and doc["error"]["code"] == "BACKUP_CONFIG_INVALID"

    env = _env(world, _cfg_file(world))
    lock = world.config().lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, doc, _ = _run([sys.executable, "-m", "hermclaw.backup", "create", "--wait", "0", "--no-events"], env)
        assert code == 75 and doc["error"]["code"] == "BACKUP_LOCKED"
    finally:
        os.close(fd)
    code, doc, _ = _run([sys.executable, "-m", "hermclaw.backup", "create", "--no-events", "--target-dir", str(world.root / "other")], env)
    assert code == 3 and "does not exist" in doc["error"]["message"]  # an unmounted share is never created implicitly
    assert not (world.root / "other").exists()
