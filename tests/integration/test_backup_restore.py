"""P33 end-to-end against the real local PostgreSQL 16: backup → local target (simulated Terra .60 share) →
verification → restore test in a clean temporary database → retention; tampering is detected."""

from __future__ import annotations

import gzip
import json
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from hermclaw.backup import create_backup, prune_backups, restore_test, verify_set_dir
from hermclaw.backup.backup import list_backups
from hermclaw.backup.config import RetentionPolicy
from hermclaw.backup.errors import BACKUP_VERIFY_FAILED, BackupError
from hermclaw.backup.manifest import ARTIFACTS_TAR, CONFIG_TAR, DB_DUMP, GLOBALS, MANIFEST, MANIFEST_SIG, RESTORE_REPORT, read_manifest
from tests.integration.test_backup_support import (
    PLAINTEXT_PASSWORD,
    BackupWorld,
    events_of,
    make_world,
    seed,
    table_counts,
    temp_databases,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def world(tmp_path: Path, db_url: str) -> BackupWorld:
    return make_world(tmp_path, db_url)


def _members(tarball: Path) -> list[str]:
    with tarfile.open(tarball, "r:gz") as tar:
        return tar.getnames()


def _payload_text(tarball: Path) -> bytes:
    with gzip.open(tarball, "rb") as handle:
        return handle.read()


async def test_backup_to_local_target_and_restore_test_pass(world: BackupWorld, sessionmaker: Any, db_url: str) -> None:
    live_before = await seed(sessionmaker)
    cfg = world.config()
    result = await create_backup(config=cfg, sessionmaker=sessionmaker)

    # --- 33.4: set on the target, complete, verified, staging removed
    set_dir = world.target_dir / result.set_id
    assert set_dir.is_dir() and not (world.target_dir / f"{result.set_id}.partial").exists()
    assert sorted(p.name for p in set_dir.iterdir()) == sorted([DB_DUMP, GLOBALS, CONFIG_TAR, ARTIFACTS_TAR, MANIFEST])
    assert result.transfer.verified and result.transfer.method == "local"
    assert list(world.staging.glob("hermclaw-*")) == []
    assert (set_dir / DB_DUMP).stat().st_mode & 0o077 == 0  # 0600 on the share

    # --- 33.1: manifest snapshot equals the database at backup time (events only grow afterwards)
    manifest, signature = read_manifest(set_dir)
    assert signature == "unsigned"
    assert manifest.database.snapshot_consistent
    assert manifest.database.alembic_revision
    assert "vector" in manifest.database.extensions
    assert manifest.globals.included and not manifest.globals.role_passwords
    live_after = table_counts(db_url)
    assert set(manifest.database.tables) == set(live_after)
    for table in ("public.research_runs", "public.research_sources", "public.research_claims", "public.deployments"):
        assert manifest.database.tables[table] == live_before[table] == live_after[table]
    assert manifest.database.tables["public.events"] >= live_before["public.events"]
    assert {f.name for f in manifest.files} == {DB_DUMP, GLOBALS, CONFIG_TAR, ARTIFACTS_TAR}
    assert result.rows == sum(manifest.database.tables.values())

    # --- 33.2: config without plaintext secrets, encrypted credstore included
    members = _members(set_dir / CONFIG_TAR)
    assert "config/hosts.yaml" in members and "config/policies.yaml" in members
    assert "config/ssh/known_hosts" in members and "config/ssh/backup_ed25519.pub" in members
    assert "config/tls/server.crt" in members and "config/nested/deep/limits.yaml" in members
    assert "credstore/credstore.encrypted/hermclaw-db-password" in members
    assert "credstore/credstore.encrypted/api-admin-token" in members
    for leaked in (
        "config/secrets",
        "config/secrets/hermclaw-db-password",
        "config/ssh/id_ed25519",
        "config/ssh/backup_ed25519",
        "config/tls/server.pem",
        "config/tls/server.key",
        "config/gitlab.token",
        "config/legacy.env",
        "config/notes/pasted.txt",
        "config/tokens-store/gl-runner",
        "config/passwd-link",
    ):
        assert leaked not in members, leaked
    raw = _payload_text(set_dir / CONFIG_TAR)
    assert PLAINTEXT_PASSWORD.encode() not in raw and b"PRIVATE KEY-----" not in raw and b"glpat-" not in raw
    excluded = "\n".join(manifest.config.excluded)
    assert "legacy.env (plaintext secret)" in excluded and "pasted.txt (private key)" in excluded
    assert "gl-runner (secret reference)" in excluded and "config/secrets/" in excluded
    assert any("passwd-link" in s for s in manifest.config.skipped)
    assert result.config_excluded == manifest.config.excluded

    # --- 33.3: artifacts + research records + deployment manifests
    members = _members(set_dir / ARTIFACTS_TAR)
    assert any(m.endswith("/diff.patch") for m in members) and "artifacts/blobs/model-output.bin" in members
    assert "artifacts/empty-dir" in members
    exports = {r.table: r.rows for r in manifest.records}
    assert exports == {
        "research_runs": live_before["public.research_runs"],
        "research_sources": live_before["public.research_sources"],
        "research_claims": live_before["public.research_claims"],
        "research_claim_sources": live_before["public.research_claim_sources"],
        "deployments": live_before["public.deployments"],
    }
    with tarfile.open(set_dir / ARTIFACTS_TAR, "r:gz") as tar:
        handle = tar.extractfile("records/deployments.jsonl")
        assert handle is not None
        deployments = [json.loads(line) for line in handle.read().decode().splitlines()]
    assert any(d["manifest"]["note"] == "ünïcode" and d["target"] == "terra-60" for d in deployments)

    # --- verification of the stored set
    verify, _ = await verify_set_dir(set_dir)
    assert verify.ok, verify.problems
    assert verify.dump is not None and verify.dump.table_data_entries >= len(manifest.database.tables) - 1

    # --- 33.5 / 33.6: restore test in a clean temporary database
    report = await restore_test(result.set_id, config=cfg, sessionmaker=sessionmaker)
    assert report.ok, [c for c in report.checks if not c.ok]
    assert report.kind == "restore_test" and report.set_id == result.set_id
    assert report.database and report.database.startswith("hcbk_test_") and report.database_dropped is True
    assert temp_databases() == []
    assert report.tables_restored == report.tables_expected == len(manifest.database.tables)
    assert report.rows_restored == report.rows_expected == result.rows
    assert all(t.ok for t in report.tables)
    assert report.alembic_actual == report.alembic_expected == manifest.database.alembic_revision
    names = {c.name for c in report.checks}
    for required in (
        "manifest",
        f"checksum:{DB_DUMP}",
        f"checksum:{CONFIG_TAR}",
        f"archive:{ARTIFACTS_TAR}",
        "dump_catalog",
        "config_extract",
        "restore_environment",
        "pg_restore",
        "tables_present",
        "row_counts",
        "alembic_revision",
        "extensions_present",
        "records_export",
        "restore_environment_dropped",
    ):
        assert required in names, required
    stored = json.loads((set_dir / RESTORE_REPORT).read_text())
    assert stored["ok"] is True and stored["set_id"] == result.set_id and stored["rows_restored"] == result.rows
    assert (set_dir / RESTORE_REPORT).stat().st_mode & 0o077 == 0

    # --- events + no secret in any event payload
    kinds = [t for t, _ in events_of(db_url)]
    assert "backup.started" in kinds and "backup.completed" in kinds and "backup.restore_test.passed" in kinds
    assert PLAINTEXT_PASSWORD not in json.dumps(events_of(db_url))


async def test_tampered_tarball_and_dump_fail_verification_and_restore_test(world: BackupWorld, sessionmaker: Any) -> None:
    await seed(sessionmaker, runs=1)
    cfg = world.config()
    result = await create_backup(config=cfg)
    set_dir = world.target_dir / result.set_id

    # flip one byte in the middle of the artifacts tarball
    data = bytearray((set_dir / ARTIFACTS_TAR).read_bytes())
    data[len(data) // 2] ^= 0xFF
    (set_dir / ARTIFACTS_TAR).write_bytes(bytes(data))
    verify, _ = await verify_set_dir(set_dir)
    assert not verify.ok and verify.failed(ARTIFACTS_TAR) and not verify.failed(CONFIG_TAR)
    assert any("sha256 mismatch" in p for p in verify.problems)

    report = await restore_test(result.set_id, config=cfg, sessionmaker=sessionmaker)
    assert not report.ok
    failed = report.failed_checks()
    assert f"checksum:{ARTIFACTS_TAR}" in failed
    # the dump itself is fine: the database part is still restored and compared
    assert report.check("row_counts") is not None and report.check("row_counts").ok  # type: ignore[union-attr]
    assert temp_databases() == []
    stored = json.loads((set_dir / RESTORE_REPORT).read_text())
    assert stored["ok"] is False and f"checksum:{ARTIFACTS_TAR}" in [c["name"] for c in stored["checks"] if not c["ok"]]

    # a corrupted dump is never fed to pg_restore
    dump = bytearray((set_dir / DB_DUMP).read_bytes())
    dump[100] ^= 0x01
    (set_dir / DB_DUMP).write_bytes(bytes(dump))
    report = await restore_test(result.set_id, config=cfg)
    assert not report.ok and f"checksum:{DB_DUMP}" in report.failed_checks()
    skipped = report.check("database_restore")
    assert skipped is not None and not skipped.ok and "skipped" in skipped.detail
    assert report.database is None and temp_databases() == []
    kinds = [t for t, _ in events_of(world.db_url)]
    assert "backup.restore_test.failed" in kinds


async def test_restore_test_detects_row_count_mismatch(world: BackupWorld) -> None:
    """The comparison is real: a manifest that claims other row counts fails the restore test."""
    cfg = world.config()
    result = await create_backup(config=cfg)
    set_dir = world.target_dir / result.set_id
    doc = json.loads((set_dir / MANIFEST).read_text())
    doc["database"]["tables"]["public.deployments"] += 5
    doc["database"]["alembic_revision"] = "0000_not_the_revision"
    (set_dir / MANIFEST).write_text(json.dumps(doc))
    report = await restore_test(set_dir, config=cfg)  # a set directory directly (no target lookup)
    assert not report.ok
    assert {"row_counts", "alembic_revision"} <= set(report.failed_checks())
    bad = [t for t in report.tables if not t.ok]
    assert [t.table for t in bad] == ["public.deployments"] and bad[0].expected == bad[0].actual + 5  # type: ignore[operator]
    assert report.database_dropped is True and temp_databases() == []


async def test_signed_manifest_detects_tampering(world: BackupWorld) -> None:
    cfg = world.config(manifest_signing_key_ref="literal:backup-signing-key-for-tests")
    result = await create_backup(config=cfg)
    assert result.signed
    set_dir = world.target_dir / result.set_id
    assert (set_dir / MANIFEST_SIG).is_file()
    key = b"backup-signing-key-for-tests"
    _, state = read_manifest(set_dir, signing_key=key)
    assert state == "valid"
    report = await restore_test(result.set_id, config=cfg)
    assert report.ok and report.signature == "valid"
    # recompute a "consistent" manifest after tampering: without the key the signature cannot follow
    doc = json.loads((set_dir / MANIFEST).read_text())
    doc["database"]["tables"]["public.events"] = 0
    (set_dir / MANIFEST).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    with pytest.raises(BackupError) as err:
        read_manifest(set_dir, signing_key=key)
    assert err.value.code == BACKUP_VERIFY_FAILED
    report = await restore_test(result.set_id, config=cfg)
    assert not report.ok and report.failed_checks() == ["manifest"]
    (set_dir / MANIFEST_SIG).unlink()
    with pytest.raises(BackupError, match="signature missing"):
        read_manifest(set_dir, signing_key=key)


async def test_retention_prune_keeps_daily_and_weekly(world: BackupWorld, sessionmaker: Any) -> None:
    cfg = world.config(retention=RetentionPolicy(keep_last=1, keep_daily=2, keep_weekly=2, prune_after_backup=False))
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)  # a Wednesday (ISO week 42)
    ids: dict[str, str] = {}
    for label, delta in (("d0", 0), ("d0b", 0), ("d1", 1), ("d3", 3), ("d9", 9), ("d20", 20)):
        made = await create_backup(config=cfg, now=now - timedelta(days=delta, minutes=1 if label == "d0" else 0))
        ids[label] = made.set_id
    # an interrupted transfer (stale) and a fresh one (maybe still running)
    stale = world.target_dir / f"hermclaw-{(now - timedelta(days=2)):%Y%m%dT%H%M%SZ}-abcdef.partial"
    stale.mkdir()
    fresh = world.target_dir / f"hermclaw-{now:%Y%m%dT%H%M%SZ}-fedcba.partial"
    fresh.mkdir()
    dry = await prune_backups(cfg, dry_run=True, now=now)
    assert dry.deleted == [] and len(list(world.target_dir.iterdir())) == 8
    pruned = await prune_backups(cfg, sessionmaker=sessionmaker, now=now)
    remaining = {p.name for p in world.target_dir.iterdir()}
    # keep_last: d0b; daily (2): newest of 14.10. (d0b) and 13.10. (d1); weekly (2): newest of W42 (d0b) and of W41
    # (d3 = Sunday 11.10. beats d9 = Monday 05.10.); d0 (older set of the same day), d9 and d20 (W39) go
    assert remaining == {ids["d0b"], ids["d1"], ids["d3"], fresh.name}
    assert pruned.reasons[ids["d3"]] == ["weekly 2026-W41"]
    assert stale.name not in remaining and fresh.name in remaining
    assert set(pruned.deleted) == {p for p in (*ids.values(), stale.name) if p not in remaining}
    assert pruned.errors == []
    listed = await list_backups(cfg)
    assert next(s.set_id for s in listed if s.complete) == ids["d0b"]
    assert any(t == "backup.pruned" for t, _ in events_of(world.db_url))


async def test_prune_after_backup_protects_the_new_set(world: BackupWorld) -> None:
    cfg = world.config(retention=RetentionPolicy(keep_last=1, keep_daily=0, keep_weekly=0, prune_after_backup=True))
    first = await create_backup(config=cfg)
    second = await create_backup(config=cfg)
    assert second.pruned == [first.set_id]
    assert [p.name for p in world.target_dir.iterdir()] == [second.set_id]


async def test_restore_test_latest_picks_newest_complete_set(world: BackupWorld) -> None:
    cfg = world.config()
    await create_backup(config=cfg, now=datetime.now(UTC) - timedelta(hours=2))
    newest = await create_backup(config=cfg)
    shutil.copytree(world.target_dir / newest.set_id, world.target_dir / "not-a-set")
    report = await restore_test("latest", config=cfg)
    assert report.ok and report.set_id == newest.set_id
