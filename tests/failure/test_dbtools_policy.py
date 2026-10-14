"""Database tool policy (P27 27.7): capability gate, production targets only read, migrations only in sandbox
databases created (and dropped) by the tools, isolated sandbox roles, every refusal audited with redacted payloads,
audit/secret failures contained."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from hermclaw.core.settings import get_settings
from hermclaw.dbtools.errors import DB_CAPABILITY_DENIED, DB_POLICY_DENIED, DB_TARGET_UNSUPPORTED, DbPolicyError, DbToolError
from hermclaw.dbtools.policy import DbAuditor, DbCallContext, DbOperation, DbPolicy
from hermclaw.dbtools.runner import LocalMigrationRunner
from hermclaw.dbtools.targets import DatabaseTarget, TargetEnvironment, TargetKind, parse_dsn
from tests.integration.test_dbtools_support import (
    ADMIN_URL,
    BASE_URL,
    SECRET_PW,
    ctx,
    db_events,
    make_sqlite,
    make_tools,
    pg_dsn,
    sandbox_names,
    target,
    temp_database,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _target(env: TargetEnvironment, read_only: bool, kind: TargetKind = TargetKind.postgresql) -> DatabaseTarget:
    return DatabaseTarget(name=f"t-{env.value}", dsn_ref="literal:x", kind=kind, environment=env, read_only=read_only)


async def test_target_policy_matrix() -> None:
    policy = DbPolicy()
    reads = (DbOperation.introspect, DbOperation.query, DbOperation.backup, DbOperation.seed_schema)
    writes = (DbOperation.migration_apply, DbOperation.restore_test)
    for env, ro in ((TargetEnvironment.production, True), (TargetEnvironment.staging, True), (TargetEnvironment.staging, False)):
        t = _target(env, ro)
        for op in reads:
            policy.check_target(op, t)  # reading is always allowed
        for op in writes:
            with pytest.raises(DbPolicyError) as exc:
                policy.check_target(op, t)
            assert exc.value.code == DB_POLICY_DENIED
    policy.check_target(DbOperation.migration_apply, _target(TargetEnvironment.sandbox, False))
    with pytest.raises(DbToolError) as unsupported:
        policy.check_target(DbOperation.query, _target(TargetEnvironment.staging, True, TargetKind.mysql))
    assert unsupported.value.code == DB_TARGET_UNSUPPORTED


async def test_caller_policy_and_sandbox_names() -> None:
    policy = DbPolicy()
    policy.check_caller(DbOperation.query, DbCallContext(capability="database"))
    policy.check_caller(DbOperation.query, DbCallContext(actor="operator"))
    for capability in (None, "coding", "ssh", "DATABASE"):
        with pytest.raises(DbPolicyError) as exc:
            policy.check_caller(DbOperation.backup, DbCallContext(capability=capability))
        assert exc.value.code == DB_CAPABILITY_DENIED
    assert policy.is_sandbox_name("hermclaw_sbx_0123456789ab")
    for name in (
        "postgres",
        "hermclaw",
        "hermclaw_sbx_",
        "hermclaw_sbx_0123456789abc",
        "hermclaw_sbx_0123456789AB",
        "x_hermclaw_sbx_0123456789ab",
    ):
        assert not policy.is_sandbox_name(name), name


async def test_wrong_capability_is_refused_for_every_tool_and_audited(tmp_path: Path, sessionmaker: Any) -> None:
    db = make_sqlite(tmp_path / "x.db")
    (tmp_path / "ws" / "migrations").mkdir(parents=True)
    tools = make_tools(tmp_path, [target("lite", str(db), kind=TargetKind.sqlite)], sessionmaker=sessionmaker)
    c = ctx(capability="coding")
    before = set(sandbox_names())
    results = [
        await tools.db_introspect("lite", ctx=c),
        await tools.db_query_readonly("lite", "SELECT 1", ctx=c),
        await tools.db_migration_generate(tmp_path / "ws", "add table", ctx=c),
        await tools.db_migration_apply_sandbox(tmp_path / "ws", ctx=c),
        await tools.db_backup("lite", ctx=c),
        await tools.db_restore_test(str(uuid.uuid4()), ctx=c),
    ]
    assert all(r.code == DB_CAPABILITY_DENIED for r in results), [r.render() for r in results]
    assert not list((tmp_path / "ws" / "migrations").iterdir()) and not (tmp_path / "backups").exists()
    assert set(sandbox_names()) == before
    events = await db_events(sessionmaker, job_id=c.job_id)
    assert [e["outcome"] for e in events] == ["denied"] * 6
    assert {e["tool"] for e in events} == {op.value for op in DbOperation} - {"db_seed_schema", "db_sandbox_cleanup"}
    assert all(e["capability"] == "coding" and e["_severity"] == "warning" for e in events)


async def test_mysql_and_missing_secrets_fail_cleanly(tmp_path: Path, sessionmaker: Any) -> None:
    tools = make_tools(
        tmp_path,
        [
            DatabaseTarget(name="my", dsn_ref="literal:mysql://u:p@h/db", kind=TargetKind.mysql),
            DatabaseTarget(name="nosecret", dsn_ref="cred:hermclaw-test-does-not-exist"),
        ],
        sessionmaker=sessionmaker,
        sandbox=False,
    )
    c = ctx()
    assert (await tools.db_query_readonly("my", "SELECT 1", ctx=c)).code == DB_TARGET_UNSUPPORTED
    missing = await tools.db_introspect("nosecret", ctx=c)
    assert not missing.ok and missing.code == "SECRET_MISSING"
    assert [e["outcome"] for e in await db_events(sessionmaker, job_id=c.job_id)] == ["failed", "failed"]


async def test_credentials_never_reach_events_or_results(tmp_path: Path, sessionmaker: Any) -> None:
    dsn = f"postgresql://postgres:{SECRET_PW}@127.0.0.1:1/nowhere"  # nothing listens on port 1
    tools = make_tools(tmp_path, [target("closed", dsn)], sessionmaker=sessionmaker, sandbox=False)
    c = ctx()
    results = [
        await tools.db_introspect("closed", ctx=c),
        await tools.db_query_readonly("closed", f"SELECT '{dsn}'", ctx=c),
        await tools.db_backup("closed", ctx=ctx(job_id=None)),
    ]
    assert all(not r.ok for r in results)
    rendered = " ".join(r.render() + json.dumps(r.to_dict(), default=str) for r in results)
    assert SECRET_PW not in rendered
    events = await db_events(sessionmaker, target="closed")
    dumped = json.dumps(events, default=str)
    assert events and SECRET_PW not in dumped and "postgresql://postgres@127.0.0.1:1/nowhere" in dumped


async def test_audit_failures_do_not_break_tool_calls(tmp_path: Path) -> None:
    class BrokenSessionmaker:
        def __call__(self) -> Any:
            raise RuntimeError("database down")

    db = make_sqlite(tmp_path / "x.db")
    tools = make_tools(tmp_path, [target("lite", str(db), kind=TargetKind.sqlite)], sandbox=False)
    tools.auditor = DbAuditor(BrokenSessionmaker())  # type: ignore[arg-type]
    res = await tools.db_query_readonly("lite", "SELECT count(*) FROM books", ctx=ctx())
    assert res.ok and res.data["rows"] == [[3]]


async def test_sandbox_role_is_isolated_and_only_sandbox_names_are_dropped(tmp_path: Path, sessionmaker: Any) -> None:
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    server = tools.sandbox_server()
    sandbox = await server.create(label="isolation")
    try:
        assert sandbox.conn.user == sandbox.name and sandbox.conn.password and sandbox.conn.password not in repr(sandbox)
        with psycopg.connect(autocommit=True, **sandbox.conn.psycopg_kwargs()) as conn:
            role = conn.execute("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()
            assert role == (False, False, False)
            owner = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()").fetchone()
            assert owner == (sandbox.name,)
            for statement in (
                "CREATE ROLE intruder",
                "COPY (SELECT 1) TO '/tmp/hc_dbtools_copy_escape'",
                "ALTER SYSTEM SET work_mem = '1GB'",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
        for name in ("postgres", "template1", "hermclaw_sbx_zz", f"hermclaw_sbx_{'0' * 12}x"):
            with pytest.raises(DbPolicyError):
                await server.drop(name)
    finally:
        assert await server.drop(sandbox)
    assert sandbox.name not in sandbox_names() and f"role:{sandbox.name}" not in sandbox_names()


async def test_orphan_cleanup_drops_only_old_tool_sandboxes(tmp_path: Path, sessionmaker: Any) -> None:
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    server = tools.sandbox_server()
    fresh = await server.create(label="fresh")
    with temp_database(prefix="hermclaw_sbx_keep_") as foreign:
        try:
            res = await tools.cleanup_orphan_sandboxes(max_age=timedelta(hours=1))
            assert res.ok and fresh.name not in res.data["dropped"], "young sandboxes belong to running jobs"
            res = await tools.cleanup_orphan_sandboxes(max_age=timedelta(hours=1), now=datetime.now(UTC) + timedelta(hours=2))
            assert fresh.name in res.data["dropped"] and foreign not in res.data["dropped"]
            with psycopg.connect(ADMIN_URL) as conn:
                assert conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (foreign,)).fetchone() == (1,)
        finally:
            await server.drop(fresh)
    events = await db_events(sessionmaker, tool="db_sandbox_cleanup")
    assert events and events[-1]["actor"] == "runtime"


async def test_local_migration_commands_are_refused_in_production(
    tmp_path: Path, sessionmaker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    (ws / "app").mkdir(parents=True)
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    monkeypatch.setenv("HERMCLAW_ENV", "production")
    get_settings.cache_clear()
    try:
        res = await tools.db_migration_apply_sandbox(
            ws, path="app", target_kind="sqlite", command=["true"], runner=LocalMigrationRunner(), ctx=ctx()
        )
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert not res.ok and "sandbox in production" in res.message


async def test_seed_reads_production_but_never_writes_it(tmp_path: Path, sessionmaker: Any) -> None:
    """A production target may seed a sandbox (read); a migration that would affect it only changes the sandbox."""
    ws = tmp_path / "ws"
    d = ws / "migrations"
    d.mkdir(parents=True)
    (d / "001_drop_everything.sql").write_text("-- migrate:up\nDROP TABLE precious;\n-- migrate:down\nCREATE TABLE precious (id int);\n")
    with temp_database("CREATE TABLE precious (id int); INSERT INTO precious VALUES (1);") as name:
        tools = make_tools(tmp_path, [target("prod", pg_dsn(name))], sessionmaker=sessionmaker)
        res = await tools.db_migration_apply_sandbox(ws, seed_schema_from="prod", ctx=ctx())
        assert res.ok, res.render()
        with psycopg.connect(f"{BASE_URL}/{name}") as conn:
            assert conn.execute("SELECT count(*) FROM precious").fetchone() == (1,)
    conn_info = parse_dsn(pg_dsn("x"), TargetKind.postgresql)
    assert SECRET_PW not in conn_info.display()


async def test_orphan_cleanup_handles_role_only_leftovers_and_sqlite_dirs(tmp_path: Path, sessionmaker: Any) -> None:
    import os

    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    role = f"hermclaw_sbx_{uuid.uuid4().hex[:12]}"
    valid = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S+00")
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {role} LOGIN VALID UNTIL '{valid}'")  # crash right after CREATE ROLE
    base = tmp_path / "sqlite-sandboxes"
    base.mkdir()
    old_dir, new_dir, foreign = base / "hermclaw_sbx_old1", base / "hermclaw_sbx_new1", base / "other_dir"
    for d in (old_dir, new_dir, foreign):
        d.mkdir()
    stale = (datetime.now(UTC) - timedelta(hours=3)).timestamp()
    os.utime(old_dir, (stale, stale))
    os.utime(foreign, (stale, stale))
    try:
        res = await tools.cleanup_orphan_sandboxes(max_age=timedelta(hours=1))
        assert role not in res.data["dropped"], "a role created moments ago may belong to a sandbox being created"
        assert res.data["sqlite_removed"] == ["hermclaw_sbx_old1"] and new_dir.exists() and foreign.exists()
        res = await tools.cleanup_orphan_sandboxes(max_age=timedelta(hours=1), now=datetime.now(UTC) + timedelta(hours=2))
        assert role in res.data["dropped"]
        assert f"role:{role}" not in sandbox_names()
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f"DROP ROLE IF EXISTS {role}")
