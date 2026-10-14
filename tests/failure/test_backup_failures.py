"""P33 failure injection: unmounted share, unreachable/failing database tools, locks, interrupted transfers,
cancellation, crashed restore tests, crafted archives/dumps with matching checksums, secret leakage."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest

from hermclaw.backup import LocalTarget, RsyncTarget, create_backup, restore_test
from hermclaw.backup import runner as backup_runner
from hermclaw.backup import transfer as backup_transfer
from hermclaw.backup.backup import list_backups
from hermclaw.backup.config import RestoreTestConfig
from hermclaw.backup.errors import BackupError
from hermclaw.backup.lock import backup_lock
from hermclaw.backup.manifest import ARTIFACTS_TAR, CONFIG_TAR, DB_DUMP, GLOBALS, MANIFEST, RESTORE_REPORT, sha256_file
from hermclaw.backup.transfer import _under_mount
from tests.integration.test_backup_support import (
    ADMIN_URL,
    TEST_PREFIX,
    BackupWorld,
    events_of,
    make_world,
    seed,
    temp_databases,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def world(tmp_path: Path, db_url: str) -> BackupWorld:
    return make_world(tmp_path, db_url)


def _fake_bin(world: BackupWorld, **scripts: str) -> Path:
    """pg_bin_dir with the real client tools, some replaced by shell scripts."""
    from hermclaw.backup.pg import find_pg_tool

    real = Path(find_pg_tool("pg_dump")).parent  # /usr/lib/postgresql/<major>/bin (not the Debian wrapper)
    fake = world.root / "pgbin"
    fake.mkdir()
    for tool in ("pg_dump", "pg_dumpall", "pg_restore", "psql"):
        target = fake / tool
        if tool in scripts:
            target.write_text("#!/bin/sh\n" + scripts[tool], encoding="utf-8")
            target.chmod(0o755)
        else:
            target.symlink_to(real / tool)
    return fake


def _alive(pid: int) -> bool:
    """Running (zombies of orphans reparented to a non-reaping container init count as dead)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


def _rewrite_manifest(set_dir: Path, name: str, **changes: Any) -> None:
    """Make the manifest match a crafted file (simulates an attacker/bug that kept checksums consistent)."""
    doc = json.loads((set_dir / MANIFEST).read_text())
    digest, size = sha256_file(set_dir / name)
    for entry in doc["files"]:
        if entry["name"] == name:
            entry["sha256"], entry["size"] = digest, size
    for key, value in changes.items():
        doc[key].update(value)
    (set_dir / MANIFEST).write_text(json.dumps(doc))


# ================================================================================================ backup side
async def test_unmounted_share_fails_without_creating_it(world: BackupWorld, sessionmaker: Any) -> None:
    missing = world.root / "mnt" / "terra-backup"
    cfg = world.config(target=LocalTarget(path=missing))
    with pytest.raises(BackupError) as err:
        await create_backup(config=cfg, sessionmaker=sessionmaker)
    assert err.value.code == "BACKUP_TRANSFER_FAILED" and "not mounted" in err.value.message
    assert not missing.exists() and list(world.staging.glob("hermclaw-*")) == []
    failed = [p for t, p in events_of(world.db_url) if t == "backup.failed"]
    assert failed and failed[-1]["code"] == "BACKUP_TRANSFER_FAILED"


async def test_require_mount_refuses_a_plain_directory(world: BackupWorld) -> None:
    if _under_mount(world.target_dir):
        pytest.skip("the temp dir is on a separate mount in this environment")
    cfg = world.config(target=LocalTarget(path=world.target_dir, require_mount=True))
    with pytest.raises(BackupError, match="not on a mounted file system"):
        await create_backup(config=cfg)
    assert list(world.target_dir.iterdir()) == []
    assert _under_mount(Path("/proc/self"))  # a real mount point is accepted


async def test_create_missing_target_directory_when_allowed(world: BackupWorld) -> None:
    target = world.root / "new-share" / "hermclaw"
    made = await create_backup(config=world.config(target=LocalTarget(path=target, create_missing=True)))
    assert (target / made.set_id / MANIFEST).is_file() and target.stat().st_mode & 0o777 == 0o700


async def test_unreachable_database(world: BackupWorld) -> None:
    cfg = world.config(database_url="postgresql+psycopg://postgres@127.0.0.1:1/hermclaw")
    with pytest.raises(BackupError) as err:
        await create_backup(config=cfg)
    assert err.value.code == "BACKUP_DB_CONNECT_FAILED"
    assert list(world.target_dir.iterdir()) == [] and list(world.staging.glob("hermclaw-*")) == []


async def test_pg_dump_failure_is_reported_redacted(world: BackupWorld) -> None:
    script = "echo 'pg_dump: error: connection to postgresql://hermclaw:TopSecretPw@db/hermclaw failed' >&2\nexit 1\n"
    cfg = world.config(pg_bin_dir=_fake_bin(world, pg_dump=script))
    with pytest.raises(BackupError) as err:
        await create_backup(config=cfg)
    assert err.value.code == "BACKUP_DUMP_FAILED"
    assert "TopSecretPw" not in err.value.message and "REDACTED" in err.value.message
    assert list(world.target_dir.iterdir()) == []


async def test_globals_not_permitted_are_skipped_gracefully(world: BackupWorld) -> None:
    script = "echo 'pg_dumpall: error: query failed: ERROR:  permission denied for table pg_authid' >&2\nexit 1\n"
    cfg = world.config(pg_bin_dir=_fake_bin(world, pg_dumpall=script))
    made = await create_backup(config=cfg)
    assert made.globals_included is False
    set_dir = world.target_dir / made.set_id
    assert not (set_dir / GLOBALS).exists()
    manifest = json.loads((set_dir / MANIFEST).read_text())
    assert manifest["globals"]["included"] is False and "permission denied" in manifest["globals"]["reason"]
    report = await restore_test(made.set_id, config=cfg)
    assert report.ok, report.failed_checks()


async def test_concurrent_operations_are_serialised_by_the_lock(world: BackupWorld) -> None:
    cfg = world.config()
    async with backup_lock(cfg.lock_path(), wait_seconds=0, what="test"):
        with pytest.raises(BackupError) as err:
            await create_backup(config=cfg)
        assert err.value.code == "BACKUP_LOCKED" and "op=test" in err.value.message
        with pytest.raises(BackupError):
            await restore_test("latest", config=cfg)

    waiting = cfg.model_copy(update={"lock_wait_seconds": 30})

    async def hold() -> None:
        async with backup_lock(cfg.lock_path(), wait_seconds=0, what="holder"):
            await asyncio.sleep(0.6)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.1)
    started = time.monotonic()
    made = await create_backup(config=waiting)
    await holder
    assert time.monotonic() - started >= 0.4 and (world.target_dir / made.set_id).is_dir()


async def test_interrupted_copy_to_the_share_leaves_nothing(world: BackupWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real = backup_transfer._copy_private

    def flaky(src: Path, dest: Path, *, atomic: bool = False) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        real(src, dest, atomic=atomic)

    monkeypatch.setattr(backup_transfer, "_copy_private", flaky)
    with pytest.raises(BackupError) as err:
        await create_backup(config=world.config())
    assert err.value.code == "BACKUP_TRANSFER_FAILED" and "No space left" in err.value.message
    assert list(world.target_dir.iterdir()) == [] and list(world.staging.glob("hermclaw-*")) == []


async def test_cancellation_kills_the_dump_and_cleans_up(world: BackupWorld) -> None:
    pidfile = world.root / "fake-dump.pid"
    script = f"echo $$ > {pidfile}\nexec sleep 60\n"
    cfg = world.config(pg_bin_dir=_fake_bin(world, pg_dump=script))
    task = asyncio.create_task(create_backup(config=cfg))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    pid = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert not _alive(pid)
    assert list(world.staging.glob("hermclaw-*")) == [] and list(world.target_dir.iterdir()) == []
    async with backup_lock(cfg.lock_path(), wait_seconds=0, what="after-cancel"):  # lock released
        pass


async def test_runner_timeout_kills_the_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    result = await backup_runner.run(
        ["/bin/sh", "-c", f"sleep 60 & echo $! > {marker}; wait"], env=backup_runner.base_env(), limit_seconds=0.5
    )
    assert result.timed_out and not result.ok
    await asyncio.sleep(0.2)
    assert not _alive(int(marker.read_text()))


async def test_missing_rsync_binary(world: BackupWorld) -> None:
    dest = world.root / "rsync-dest"
    dest.mkdir()
    cfg = world.config(target=RsyncTarget(path=str(dest), rsync_binary="/nonexistent/rsync"))
    with pytest.raises(BackupError) as err:
        await create_backup(config=cfg)
    assert err.value.code == "BACKUP_TOOL_MISSING"


async def test_password_never_in_argv_events_or_logs(
    world: BackupWorld, sessionmaker: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    password = "Pw-For-Backup-Test-9f3"
    url = world.db_url.replace("postgres@", f"postgres:{password}@", 1)  # trust auth: accepted, then ignored
    seen: list[tuple[tuple[str, ...], dict[str, str]]] = []
    real_run = backup_runner.run

    async def spy(argv: Any, *, env: Any, limit_seconds: float, **kw: Any) -> Any:
        passfile = env.get("PGPASSFILE")
        if passfile:
            assert Path(passfile).stat().st_mode & 0o777 == 0o600
            assert password in Path(passfile).read_text()
        seen.append((tuple(argv), dict(env)))
        return await real_run(argv, env=env, limit_seconds=limit_seconds, **kw)

    monkeypatch.setattr(backup_runner, "run", spy)
    monkeypatch.setenv("PGPASSWORD", "inherited-must-not-leak")
    caplog.set_level(logging.DEBUG)
    made = await create_backup(config=world.config(database_url=url), sessionmaker=sessionmaker)
    dumps = [(a, e) for a, e in seen if any(part.startswith("--file=") for part in a)]
    assert len(dumps) == 2
    for argv, env in seen:
        assert not any(password in part for part in argv)
        assert "PGPASSWORD" not in env
    assert all("PGPASSFILE" in env and not Path(env["PGPASSFILE"]).exists() for _, env in dumps)  # removed again
    manifest = (world.target_dir / made.set_id / MANIFEST).read_text()
    assert password not in manifest and password not in json.dumps(events_of(world.db_url))
    assert password not in caplog.text


# ================================================================================================ restore side
async def test_stale_temp_database_of_a_crashed_run_is_dropped(world: BackupWorld) -> None:
    import psycopg

    cfg = world.config()
    made = await create_backup(config=cfg)
    stale = f"{TEST_PREFIX}_deadbeef"
    unrelated = f"{TEST_PREFIX}_keep_me"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{stale}"')
        conn.execute(f'CREATE DATABASE "{unrelated}"')
    try:
        report = await restore_test(made.set_id, config=cfg)
        assert report.ok
        assert temp_databases() == [unrelated]  # only <prefix>_<8 hex> names are treated as leftovers
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{unrelated}"')
            conn.execute(f'DROP DATABASE IF EXISTS "{stale}"')


async def test_restore_test_with_unreachable_restore_server(world: BackupWorld) -> None:
    cfg = world.config()
    made = await create_backup(config=cfg)
    broken = cfg.model_copy(
        update={
            "restore_test": RestoreTestConfig(
                admin_url="postgresql://postgres@127.0.0.1:1/postgres", db_prefix=TEST_PREFIX, work_dir=world.work
            )
        }
    )
    report = await restore_test(made.set_id, config=broken)
    assert not report.ok and report.error and "cannot connect" in report.error
    assert json.loads((world.target_dir / made.set_id / RESTORE_REPORT).read_text())["ok"] is False


async def test_truncated_tarball_with_matching_checksum_is_detected(world: BackupWorld) -> None:
    cfg = world.config()
    made = await create_backup(config=cfg)
    set_dir = world.target_dir / made.set_id
    data = (set_dir / ARTIFACTS_TAR).read_bytes()
    (set_dir / ARTIFACTS_TAR).write_bytes(data[: len(data) // 2])
    _rewrite_manifest(set_dir, ARTIFACTS_TAR)
    report = await restore_test(made.set_id, config=cfg)
    archive = report.check(f"archive:{ARTIFACTS_TAR}")
    assert archive is not None and not archive.ok and "unreadable" in archive.detail
    assert report.check(f"checksum:{ARTIFACTS_TAR}").ok  # type: ignore[union-attr]
    assert not report.ok


async def test_crafted_config_tarball_with_absolute_and_traversal_members(world: BackupWorld) -> None:
    cfg = world.config()
    made = await create_backup(config=cfg)
    set_dir = world.target_dir / made.set_id
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w:gz") as tar:
        for name in ("config/ok.yaml", "/tmp/hermclaw-evil-abs", "../hermclaw-evil-up", "config/link"):
            info = tarfile.TarInfo(name)
            if name.endswith("link"):
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/shadow"
                tar.addfile(info)
            else:
                payload = b"x"
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
    (set_dir / CONFIG_TAR).write_bytes(evil.getvalue())
    _rewrite_manifest(set_dir, CONFIG_TAR, config={"files": 3, "root_files": {"config": 1}})
    report = await restore_test(made.set_id, config=cfg)
    archive = report.check(f"archive:{CONFIG_TAR}")
    assert archive is not None and not archive.ok
    assert "absolute member path" in archive.detail and "escapes the archive" in archive.detail
    extract = report.check("config_extract")
    assert extract is not None and not extract.ok and "refusing to extract" in extract.detail
    assert not Path("/tmp/hermclaw-evil-abs").exists() and not (world.work.parent / "hermclaw-evil-up").exists()
    assert not report.ok


async def test_garbage_dump_with_matching_checksum_is_not_restored(world: BackupWorld) -> None:
    cfg = world.config()
    made = await create_backup(config=cfg)
    set_dir = world.target_dir / made.set_id
    (set_dir / DB_DUMP).write_bytes(b"PGDMP" + os.urandom(4096))
    _rewrite_manifest(set_dir, DB_DUMP)
    report = await restore_test(made.set_id, config=cfg)
    catalog = report.check("dump_catalog")
    assert catalog is not None and not catalog.ok
    skipped = report.check("database_restore")
    assert skipped is not None and not skipped.ok and report.database is None
    assert temp_databases() == []


async def test_incomplete_set_without_manifest(world: BackupWorld, sessionmaker: Any) -> None:
    cfg = world.config()
    made = await create_backup(config=cfg)
    (world.target_dir / made.set_id / MANIFEST).unlink()
    with pytest.raises(BackupError) as err:  # 'latest' only considers complete sets
        await restore_test("latest", config=cfg, sessionmaker=sessionmaker)
    assert err.value.code == "BACKUP_NOT_FOUND"
    failed = [p for t, p in events_of(world.db_url) if t == "backup.restore_test.failed"]
    assert failed and failed[-1]["code"] == "BACKUP_NOT_FOUND"  # the weekly timer's failure is visible as an event
    report = await restore_test(made.set_id, config=cfg)
    assert not report.ok and report.failed_checks() == ["manifest"]


async def test_prune_reports_deletion_errors_without_raising(world: BackupWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermclaw.backup import prune_backups
    from hermclaw.backup.config import RetentionPolicy

    cfg = world.config(retention=RetentionPolicy(keep_last=1, keep_daily=0, keep_weekly=0, prune_after_backup=False))
    await create_backup(config=cfg)
    await create_backup(config=cfg)

    def denied(path: Any, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("hermclaw.backup.transfer.shutil.rmtree", denied)
    result = await prune_backups(cfg)
    assert result.deleted == [] and len(result.errors) == 1 and "Permission denied" in result.errors[0]
    assert len(list(world.target_dir.iterdir())) == 2


async def test_backup_of_an_empty_artifact_store_and_missing_credstore(world: BackupWorld, sessionmaker: Any) -> None:
    await seed(sessionmaker, runs=1)
    shutil.rmtree(world.artifacts_dir)
    made = await create_backup(config=world.config(credstore_dirs=[]))
    manifest = json.loads((world.target_dir / made.set_id / MANIFEST).read_text())
    assert manifest["artifacts"]["missing"] == [str(world.artifacts_dir)]
    assert manifest["artifacts"]["root_files"]["records"] == 5
    report = await restore_test(made.set_id, config=world.config(credstore_dirs=[]))
    assert report.ok, report.failed_checks()
    with pytest.raises(BackupError, match="not a directory"):
        await create_backup(config=world.config(credstore_dirs=[world.root / "no-credstore.encrypted"]))


async def test_symlinked_set_on_the_share_is_not_followed(world: BackupWorld, tmp_path: Path) -> None:
    cfg = world.config()
    made = await create_backup(config=cfg)
    elsewhere = tmp_path / "elsewhere"
    (world.target_dir / made.set_id).rename(elsewhere)
    (world.target_dir / made.set_id).symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(BackupError) as err:
        await restore_test(made.set_id, config=cfg)
    assert err.value.code == "BACKUP_NOT_FOUND"
    assert not (elsewhere / RESTORE_REPORT).exists()
    assert [s.set_id for s in await list_backups(cfg)] == []  # symlinks are not listed as sets either


async def test_staging_inside_the_target_is_refused(world: BackupWorld) -> None:
    """The stale-staging cleanup must never be able to delete real sets on a local target."""
    existing = await create_backup(config=world.config())
    for staging in (world.target_dir, world.target_dir / "staging", world.root):
        with pytest.raises(BackupError) as err:
            await create_backup(config=world.config(staging_dir=staging))
        assert err.value.code == "BACKUP_CONFIG_INVALID"
    assert (world.target_dir / existing.set_id / MANIFEST).is_file()


async def test_unreadable_config_entries_are_skipped_and_reported(world: BackupWorld) -> None:
    if os.geteuid() == 0:
        # root reads everything: simulate the service user with a file the open() refuses
        real_open = os.open

        def guarded(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if str(path).endswith("root-only.yaml"):
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, flags, *args, **kwargs)

        (world.config_dir / "root-only.yaml").write_text("x: 1\n")
        mp = pytest.MonkeyPatch()
        mp.setattr("hermclaw.backup.archive.os.open", guarded)
    else:  # pragma: no cover - non-root CI
        (world.config_dir / "root-only.yaml").write_text("x: 1\n")
        (world.config_dir / "root-only.yaml").chmod(0)
        mp = pytest.MonkeyPatch()
    try:
        made = await create_backup(config=world.config())
    finally:
        mp.undo()
    manifest = json.loads((world.target_dir / made.set_id / MANIFEST).read_text())
    assert "config/root-only.yaml (permission denied)" in manifest["config"]["skipped"]
    report = await restore_test(made.set_id, config=world.config())
    assert report.ok, report.failed_checks()


async def test_unexpected_database_error_during_comparison_still_reports(world: BackupWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    from hermclaw.backup import restore as restore_mod

    cfg = world.config()
    made = await create_backup(config=cfg)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(restore_mod, "compare_with_manifest", broken)
    report = await restore_test(made.set_id, config=cfg)
    assert not report.ok and report.error and "server closed the connection" in report.error
    assert report.database_dropped is True and temp_databases() == []
    assert json.loads((world.target_dir / made.set_id / RESTORE_REPORT).read_text())["ok"] is False
