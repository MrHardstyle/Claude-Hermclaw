"""db_backup (27.5) and db_restore_test (27.6) against real PostgreSQL 16 (pg_dump/pg_restore) and SQLite."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import select, update

from hermclaw.dbtools.backup import compare_snapshots, filter_toc, find_pg_tool
from hermclaw.dbtools.errors import (
    DB_ARTIFACT_INVALID,
    DB_ARTIFACT_UNKNOWN,
    DB_CONNECT_FAILED,
    DB_RESTORE_MISMATCH,
    DB_SANDBOX_UNAVAILABLE,
)
from hermclaw.dbtools.targets import TargetKind
from hermclaw.persistence.models import Artifact
from tests.integration.test_dbtools_support import (
    BASE_URL,
    SECRET_PW,
    SHOP_SCHEMA,
    command_runs,
    create_database,
    ctx,
    db_events,
    drop_database,
    make_sqlite,
    make_tools,
    pg_dsn,
    sandbox_names,
    target,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(scope="module")
def shop_db() -> Iterator[str]:
    name = create_database(schema_sql=SHOP_SCHEMA)
    yield name
    drop_database(name)


async def _artifact(sessionmaker: Any, artifact_id: str) -> Artifact:
    async with sessionmaker() as session:
        row: Artifact = (await session.execute(select(Artifact).where(Artifact.id == uuid.UUID(artifact_id)))).scalar_one()
    return row


async def test_postgres_backup_and_restore_test(tmp_path: Path, sessionmaker: Any, shop_db: str) -> None:
    before = set(sandbox_names())
    tools = make_tools(tmp_path, [target("shop", pg_dsn(shop_db))], sessionmaker=sessionmaker)
    c = ctx(job_id=None)
    res = await tools.db_backup("shop", ctx=c)
    assert res.ok, res.render()
    info = res.data
    assert info["format"] == "pg_custom" and info["consistent"] is True and info["extensions"] == ["pg_trgm"]
    row = await _artifact(sessionmaker, info["artifact_id"])
    path = Path(row.path)
    assert row.kind == "backup" and path.parent == tmp_path / "backups" / "shop" and path.suffix == ".dump"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert row.sha256 == info["sha256"] and row.size_bytes == path.stat().st_size and len(row.sha256) == 64
    meta = row.metadata_["dbtools"]
    assert meta["tables"] == {"inventory.items": 2, "public.audit_log": 0, "public.customers": 30, "public.orders": 50}
    assert meta["target"] == "shop" and meta["environment"] == "production" and SECRET_PW not in json.dumps(meta)
    assert not list(path.parent.glob("*.partial"))
    dumps = [r for r in await command_runs(sessionmaker, "db_backup") if r.command and str(path) in r.command]
    assert dumps and "--snapshot=" in dumps[0].command and dumps[0].exit_code == 0
    assert all(SECRET_PW not in (r.command + (r.stderr_excerpt or "")) for r in dumps)

    restored = await tools.db_restore_test(info["artifact_id"], ctx=c)
    assert restored.ok, restored.render()
    assert restored.data["tables"] == 4 and restored.data["count_mismatches"] == [] and restored.data["checksum_ok"]
    assert any("COMMENT - EXTENSION pg_trgm" in w for w in restored.data["warnings"])
    assert set(sandbox_names()) == before, "restore sandbox dropped"
    restores = [r for r in await command_runs(sessionmaker, "db_restore") if str(path) in r.command]
    assert any("--no-owner" in r.command and "--no-privileges" in r.command and "--exit-on-error" in r.command for r in restores)
    events = await db_events(sessionmaker, target="shop")
    kinds = [(e["tool"], e["outcome"]) for e in events if e.get("artifact_id") == info["artifact_id"]]
    assert ("db_backup", "ok") in kinds and ("db_restore_test", "ok") in kinds
    assert SECRET_PW not in json.dumps(events, default=str)


async def test_counts_and_dump_share_one_snapshot_under_concurrent_writes(tmp_path: Path, sessionmaker: Any) -> None:
    """Rows inserted while the backup runs are neither counted nor dumped – the restore test still matches."""
    name = create_database(schema_sql="CREATE TABLE ticks (id bigserial PRIMARY KEY, at timestamptz DEFAULT now());")
    stop = threading.Event()
    written = 0

    def writer() -> None:
        nonlocal written
        with psycopg.connect(f"{BASE_URL}/{name}", autocommit=True) as conn:
            while not stop.is_set():
                conn.execute("INSERT INTO ticks DEFAULT VALUES")
                written += 1

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        while written < 200:
            await asyncio.sleep(0.01)
        tools = make_tools(tmp_path, [target("busy", pg_dsn(name))], sessionmaker=sessionmaker)
        c = ctx(job_id=None)
        res = await tools.db_backup("busy", ctx=c)
        assert res.ok and res.data["consistent"], res.render()
        restored = await tools.db_restore_test(res.data["artifact_id"], ctx=c)
        assert restored.ok, restored.render()
        stop.set()
        thread.join(10)
        with psycopg.connect(f"{BASE_URL}/{name}") as conn:
            row = conn.execute("SELECT count(*) FROM ticks").fetchone()
        assert row is not None and row[0] > res.data["rows"], "writes continued during the backup"
    finally:
        stop.set()
        thread.join(10)
        drop_database(name)


async def test_restore_detects_tampering_and_mismatches(tmp_path: Path, sessionmaker: Any, shop_db: str) -> None:
    tools = make_tools(tmp_path, [target("shop", pg_dsn(shop_db))], sessionmaker=sessionmaker)
    c = ctx(job_id=None)
    info = (await tools.db_backup("shop", ctx=c)).data
    row = await _artifact(sessionmaker, info["artifact_id"])
    # recorded snapshot differs from the restored data -> mismatch reported per table
    meta = dict(row.metadata_)
    meta["dbtools"] = {**meta["dbtools"], "tables": {**meta["dbtools"]["tables"], "public.orders": 51, "public.ghost": 1}}
    async with sessionmaker() as session, session.begin():
        await session.execute(update(Artifact).where(Artifact.id == row.id).values(metadata_=meta))
    res = await tools.db_restore_test(info["artifact_id"], ctx=c)
    assert not res.ok and res.code == DB_RESTORE_MISMATCH
    assert res.data["count_mismatches"] == [{"table": "public.orders", "expected": 51, "actual": 50}]
    assert res.data["missing_tables"] == ["public.ghost"] and "expected 51 rows, got 50" in res.text
    # a changed file is refused before anything is restored
    with Path(row.path).open("r+b") as handle:
        handle.seek(100)
        handle.write(b"\x00\x01\x02")
    res = await tools.db_restore_test(info["artifact_id"], ctx=c)
    assert res.code == DB_ARTIFACT_INVALID and "checksum" in res.message


async def test_restore_refuses_foreign_artifacts(tmp_path: Path, sessionmaker: Any) -> None:
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    c = ctx(job_id=None)
    assert (await tools.db_restore_test(str(uuid.uuid4()), ctx=c)).code == DB_ARTIFACT_UNKNOWN
    assert (await tools.db_restore_test("../../etc/passwd", ctx=c)).code == DB_ARTIFACT_INVALID
    outside = tmp_path / "outside.dump"
    outside.write_bytes(b"x")
    ids = []
    async with sessionmaker() as session, session.begin():
        for kind, meta, path in (("log", {}, outside), ("backup", {"dbtools": {"format": "pg_custom"}}, Path("/etc/passwd"))):
            art = Artifact(id=uuid.uuid4(), kind=kind, name="x", path=str(path), sha256="0" * 64, size_bytes=1, metadata_=meta)
            session.add(art)
            ids.append(str(art.id))
    for artifact_id in ids:
        res = await tools.db_restore_test(artifact_id, ctx=c)
        assert res.code == DB_ARTIFACT_INVALID, res.render()


async def test_sqlite_backup_and_restore(tmp_path: Path, sessionmaker: Any) -> None:
    db = make_sqlite(tmp_path / "books.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO authors (name) VALUES ('Dee')")
    conn.commit()
    tools = make_tools(tmp_path, [target("books", str(db), kind=TargetKind.sqlite)], sessionmaker=sessionmaker, sandbox=False)
    c = ctx(job_id=None)
    try:
        res = await tools.db_backup("books", ctx=c)
    finally:
        conn.close()
    assert res.ok, res.render()
    row = await _artifact(sessionmaker, res.data["artifact_id"])
    assert row.media_type == "application/vnd.sqlite3" and row.metadata_["dbtools"]["tables"] == {"main.authors": 4, "main.books": 3}
    restored = await tools.db_restore_test(res.data["artifact_id"], ctx=c)
    assert restored.ok and restored.data["integrity_check"] == "ok", restored.render()
    assert not list((tmp_path).glob("**/hermclaw_sbx_*"))


async def test_backup_failures_leave_nothing_behind(tmp_path: Path, sessionmaker: Any, shop_db: str) -> None:
    tools = make_tools(tmp_path, [target("gone", pg_dsn(f"hc_missing_{uuid.uuid4().hex[:6]}"))], sessionmaker=sessionmaker, sandbox=False)
    res = await tools.db_backup("gone", ctx=ctx(job_id=None))
    assert res.code == DB_CONNECT_FAILED and SECRET_PW not in res.render()
    assert not [p for p in (tmp_path / "backups").rglob("*") if p.is_file()]
    # a PostgreSQL dump cannot be restore-tested without a sandbox server
    ok_tools = make_tools(tmp_path, [target("shop", pg_dsn(shop_db))], sessionmaker=sessionmaker, sandbox=False)
    backup = await ok_tools.db_backup("shop", ctx=ctx(job_id=None))
    assert backup.ok
    assert (await ok_tools.db_restore_test(backup.data["artifact_id"])).code == DB_SANDBOX_UNAVAILABLE


async def test_toc_filter_and_helpers() -> None:
    listing = (
        ";\n; Archive created at 2026-10-14\n"
        "2; 3079 16384 EXTENSION - pg_trgm \n"
        "3453; 0 0 COMMENT - EXTENSION pg_trgm \n"
        "214; 1259 16390 TABLE public customers postgres\n"
        "300; 3466 16500 EVENT TRIGGER - audit_ddl postgres\n"
    )
    toc = filter_toc(listing)
    assert toc.extensions == ["pg_trgm"]
    assert [ln for ln in toc.lines if "TABLE public customers" in ln] and not [ln for ln in toc.lines if "COMMENT - EXTENSION" in ln]
    assert len(toc.dropped) == 2
    cmp = compare_snapshots({"a": 1, "b": None, "c": 2}, {"a": 1, "b": 5, "d": 0})
    assert not cmp["ok"] and cmp["missing_tables"] == ["c"] and cmp["extra_tables"] == ["d"] and cmp["not_compared"] == ["b"]
    assert os.path.basename(find_pg_tool("pg_dump", min_major=16)) == "pg_dump"
