"""P33 33.4 – rsync targets for real: rsync to a local path, and rsync over SSH to a private loopback ``sshd``
(dedicated key, own known_hosts, ``BatchMode``) whose backup key is restricted with ``rrsync`` – the production
setup of the Terra ``.60`` share. The LAN host itself is unreachable here (BLOCKER-001, see the live test)."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hermclaw.backup import RsyncTarget, create_backup, prune_backups, restore_test
from hermclaw.backup.backup import list_backups
from hermclaw.backup.config import RetentionPolicy, resolve_rsync_endpoint
from hermclaw.backup.errors import BACKUP_VERIFY_FAILED, BackupError
from hermclaw.backup.manifest import ARTIFACTS_TAR, DB_DUMP, MANIFEST, RESTORE_REPORT, read_manifest
from hermclaw.backup.transfer import RsyncStore
from tests.integration.test_backup_support import (
    BackupWorld,
    Sshd,
    make_world,
    seed,
    sshd_available,
    start_sshd,
    temp_databases,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def world(tmp_path: Path, db_url: str) -> BackupWorld:
    return make_world(tmp_path, db_url)


@pytest.fixture(scope="module")
def sshd(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Sshd]:
    if not sshd_available():
        pytest.skip("sshd/ssh-keygen/rsync not installed")
    server = start_sshd(tmp_path_factory.mktemp("backup-sshd"))
    yield server
    server.stop()


async def test_rsync_to_local_path_backup_list_restore_prune(world: BackupWorld, sessionmaker: Any) -> None:
    await seed(sessionmaker, runs=1)
    dest = world.root / "rsync-dest"
    dest.mkdir()
    target = RsyncTarget(path=str(dest))
    cfg = world.config(target=target, retention=RetentionPolicy(keep_last=1, keep_daily=0, keep_weekly=0, prune_after_backup=False))
    first = await create_backup(config=cfg, sessionmaker=sessionmaker)
    assert first.transfer.method == "rsync" and first.transfer.verified
    set_dir = dest / first.set_id
    manifest, _ = read_manifest(set_dir)
    assert manifest.tool_versions["rsync"].startswith("rsync")
    assert (set_dir / DB_DUMP).stat().st_mode & 0o777 == 0o600 and set_dir.stat().st_mode & 0o777 == 0o700
    assert not (set_dir / ".rsync-partial").exists()

    listed = await list_backups(cfg)
    assert [(s.set_id, s.complete) for s in listed] == [(first.set_id, True)]

    report = await restore_test(first.set_id, config=cfg)
    assert report.ok, report.failed_checks()
    assert json.loads((set_dir / RESTORE_REPORT).read_text())["ok"] is True

    second = await create_backup(config=cfg)
    pruned = await prune_backups(cfg)
    assert pruned.deleted == [first.set_id]
    assert sorted(p.name for p in dest.iterdir()) == [second.set_id]


async def test_rsync_checksum_verification_detects_a_modified_copy(world: BackupWorld) -> None:
    """The post-transfer ``--checksum`` dry-run reports content differences (same size, same mtime)."""
    dest = world.root / "rsync-dest"
    dest.mkdir()
    cfg = world.config(target=RsyncTarget(path=str(dest)))
    result = await create_backup(config=cfg)
    store = RsyncStore(cfg.target, resolve_rsync_endpoint(cfg.target, None), env="test")  # type: ignore[arg-type]
    # reconstruct the local copy from the target, then corrupt the target in place keeping size and mtime
    local = world.root / "local-copy" / result.set_id
    local.parent.mkdir()
    shutil.copytree(dest / result.set_id, local, copy_function=shutil.copy2)
    victim = dest / result.set_id / ARTIFACTS_TAR
    st = victim.stat()
    data = bytearray(victim.read_bytes())
    data[-100] ^= 0xFF
    victim.write_bytes(bytes(data))
    os.utime(victim, ns=(st.st_atime_ns, st.st_mtime_ns))
    with store._transport() as transport, pytest.raises(BackupError) as err:
        await store._verify(transport, local, result.set_id, data_only=False)
    assert err.value.code == BACKUP_VERIFY_FAILED and ARTIFACTS_TAR in err.value.message


async def test_rsync_over_ssh_with_rrsync_restricted_key(world: BackupWorld, sshd: Sshd, sessionmaker: Any) -> None:
    await seed(sessionmaker, runs=1)
    target = RsyncTarget(
        path=".",  # relative to the rrsync-restricted directory (authorized_keys command="rrsync <dir>")
        host="127.0.0.1",
        port=sshd.port,
        user=sshd.user,
        key_ref=f"file:{sshd.rrsync_key}",
        known_hosts=sshd.known_hosts,
    )
    cfg = world.config(target=target, retention=RetentionPolicy(keep_last=1, keep_daily=0, keep_weekly=0, prune_after_backup=True))
    first = await create_backup(config=cfg, sessionmaker=sessionmaker)
    assert first.transfer.method == "rsync" and first.transfer.verified
    assert first.location == f"{sshd.user}@127.0.0.1:./{first.set_id}"
    remote = sshd.rrsync_dir / first.set_id
    assert (remote / MANIFEST).is_file() and (remote / DB_DUMP).is_file()

    report = await restore_test("latest", config=cfg, sessionmaker=sessionmaker)
    assert report.ok, report.failed_checks()
    assert report.location.endswith(first.set_id) and report.location.startswith("rsync:")
    assert json.loads((remote / RESTORE_REPORT).read_text())["ok"] is True  # report put next to the remote set
    assert temp_databases() == []
    assert list(world.work.glob("fetch-*")) == []  # fetched copy removed

    second = await create_backup(config=cfg)
    assert second.pruned == [first.set_id] and second.prune_error is None
    assert sorted(p.name for p in sshd.rrsync_dir.iterdir()) == [second.set_id]


async def test_rsync_over_ssh_refuses_unknown_host_key(world: BackupWorld, sshd: Sshd) -> None:
    wrong = world.root / "known_hosts"
    wrong.write_text(f"[127.0.0.1]:{sshd.port} ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGd1ZXNzZWRrZXlndWVzc2Vka2V5Z3Vlc3NlZGtleQ\n")
    target = RsyncTarget(
        path=str(sshd.root / "nowhere"), host="127.0.0.1", port=sshd.port, user=sshd.user, key_ref=f"file:{sshd.key}", known_hosts=wrong
    )
    cfg = world.config(target=target)
    with pytest.raises(BackupError) as err:
        await create_backup(config=cfg)
    assert err.value.code == "BACKUP_TRANSFER_FAILED"
    assert list(world.staging.glob("hermclaw-*")) == []


async def test_rsync_over_ssh_unrestricted_key_absolute_path(world: BackupWorld, sshd: Sshd) -> None:
    dest = sshd.root / "share-abs"
    dest.mkdir()
    target = RsyncTarget(
        path=str(dest), host="127.0.0.1", port=sshd.port, user=sshd.user, key_ref=f"file:{sshd.key}", known_hosts=sshd.known_hosts
    )
    cfg = world.config(target=target)
    made = await create_backup(config=cfg)
    listed = await list_backups(cfg)
    assert [s.set_id for s in listed] == [made.set_id] and listed[0].complete
    verify_store = RsyncStore(target, resolve_rsync_endpoint(target, None), env="test")
    async with verify_store.materialize(made.set_id, world.work) as fetched:
        manifest, _ = read_manifest(fetched)
        assert manifest.set_id == made.set_id
        assert fetched.is_relative_to(world.work)
    assert list(world.work.glob("fetch-*")) == []
