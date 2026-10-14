"""The real database tool provider behind the tool engine (``hermclaw.tools.ops``): dispatch of all six tools,
capability passed through, workspace side effects of db_migration_generate audited against the scope, factory
used by ``hermclaw.runtime.ops`` reading ``databases.yaml``."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from hermclaw.contracts.tools import DB_TOOLS, ToolName
from hermclaw.core.config import CapabilityConfig, load_config
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.core.settings import REPO_ROOT, Settings
from hermclaw.dbtools.provider import DbOpsProvider, build_provider
from hermclaw.dbtools.targets import TargetKind
from hermclaw.tools.context import ToolPermissions
from hermclaw.tools.executors import SubprocessExecutor
from hermclaw.tools.ops import OpsCall
from hermclaw.tools.registry import DbMigrationApplyArgs, DbMigrationGenerateArgs, DbQueryArgs, DbRestoreTestArgs, DbTargetArgs
from tests.integration.test_dbtools_support import command_runs, db_events, make_sqlite, make_tools, target
from tests.integration.test_tools_support import make_harness, scope

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _call(tool: ToolName, args: Any, ws: Path, *, capability: str = "database", job_id: uuid.UUID | None = None) -> OpsCall:
    handle = WorkspaceHandle(uuid.uuid4(), job_id or uuid.uuid4(), ws, "job", "main", "0" * 40, "repo")
    return OpsCall(tool, args, handle, capability, job_id, None, None, 1)


async def test_provider_dispatches_every_db_tool(tmp_path: Path, sessionmaker: Any) -> None:
    db = make_sqlite(tmp_path / "books.db")
    ws = tmp_path / "ws"
    (ws / "migrations").mkdir(parents=True)
    (ws / "migrations" / "001_init.sql").write_text("-- migrate:up\nCREATE TABLE t (id INTEGER);\n-- migrate:down\nDROP TABLE t;\n")
    tools = make_tools(tmp_path, [target("books", str(db), kind=TargetKind.sqlite)], sessionmaker=sessionmaker)
    provider = DbOpsProvider(tools, executor=SubprocessExecutor(), output_chars=4000)
    assert all(provider.provides(t) for t in DB_TOOLS) and not provider.provides(ToolName.ssh_read)

    intro = await provider.call(_call(ToolName.db_introspect, DbTargetArgs(target="books"), ws))
    assert intro.ok and "TABLE main.books" in intro.output and intro.data["schema"]["table_count"] == 2
    query = await provider.call(
        _call(ToolName.db_query_readonly, DbQueryArgs(target="books", sql="SELECT name FROM authors ORDER BY id", row_limit=2), ws)
    )
    assert query.ok and query.data["rows"] == [["Ann"], ["Bob"]] and query.data["truncated"]
    bad = await provider.call(_call(ToolName.db_query_readonly, DbQueryArgs(target="books", sql="DELETE FROM books"), ws))
    assert not bad.ok and bad.error_code == "DB_QUERY_REJECTED" and "FAILED" in bad.output
    gen = await provider.call(_call(ToolName.db_migration_generate, DbMigrationGenerateArgs(description="add tags"), ws))
    assert gen.ok and gen.data["path"] == "migrations/002_add_tags.sql"
    (ws / gen.data["path"]).write_text("-- migrate:up\nCREATE TABLE tags (n TEXT);\n-- migrate:down\nDROP TABLE tags;\n")
    applied = await provider.call(_call(ToolName.db_migration_apply_sandbox, DbMigrationApplyArgs(target_kind="sqlite"), ws))
    assert applied.ok, applied.output
    backup = await provider.call(_call(ToolName.db_backup, DbTargetArgs(target="books"), ws))
    assert backup.ok, backup.output
    restore = await provider.call(_call(ToolName.db_restore_test, DbRestoreTestArgs(artifact_id=backup.data["artifact_id"]), ws))
    assert restore.ok and restore.data["tables"] == 2
    denied = await provider.call(_call(ToolName.db_backup, DbTargetArgs(target="books"), ws, capability="coding"))
    assert not denied.ok and denied.error_code == "DB_CAPABILITY_DENIED"
    mismatch = await provider.call(_call(ToolName.db_backup, DbQueryArgs(target="books", sql="SELECT 1"), ws))
    assert mismatch.error_code == "OPS_TOOL_UNAVAILABLE"


async def test_alembic_apply_goes_through_the_workspace_executor(tmp_path: Path, sessionmaker: Any) -> None:
    from tests.integration.test_dbtools_migrations_apply import alembic_workspace

    ws = tmp_path / "ws"
    alembic_workspace(ws)
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    provider = DbOpsProvider(tools, executor=SubprocessExecutor())
    job = uuid.uuid4()
    res = await provider.call(
        _call(ToolName.db_migration_apply_sandbox, DbMigrationApplyArgs(path="service", target_kind="sqlite"), ws, job_id=job)
    )
    assert res.ok, res.output
    runs = [r for r in await command_runs(sessionmaker, "db_migration") if r.job_id == job]
    assert runs and all(r.target == "sandbox" for r in runs), "project code runs through the CommandExecutor"


async def test_tool_engine_with_the_real_provider(sessionmaker: Any, tmp_repo: Path, tmp_path: Path) -> None:
    (tmp_repo / "migrations").mkdir()
    (tmp_repo / "migrations" / "0001_init.sql").write_text("-- migrate:up\nCREATE TABLE a (id int);\n-- migrate:down\nDROP TABLE a;\n")
    db = make_sqlite(tmp_path / "books.db")
    tools = make_tools(tmp_path, [target("books", str(db), kind=TargetKind.sqlite)], sessionmaker=sessionmaker)
    h = await make_harness(
        sessionmaker,
        tmp_repo,
        contract=scope(target_paths=["app.py"], allowed_new_paths=["migrations/*.sql"]),
        permissions=ToolPermissions.for_step(kind="database", capability=CapabilityConfig(name="database", worker_kind="execution")),
        step_fields={"kind": "database", "capability": "database"},
    )
    h.engine.ops, h.engine.capability = DbOpsProvider(tools), "database"
    q = await h.call("db_query_readonly", target="books", sql="SELECT count(*) FROM books")
    assert q.ok and "3" in q.output
    gen = await h.call("db_migration_generate", description="add email")
    assert gen.ok and gen.mutated_paths == ["migrations/0002_add_email.sql"]
    applied = await h.call("db_migration_apply_sandbox", target_kind="sqlite")
    assert applied.ok, applied.output
    assert applied.mutated_paths == [], "the sandbox leaves no trace in the workspace"
    events = await db_events(sessionmaker, job_id=h.step.job_id)
    assert [e["tool"] for e in events] == ["db_query_readonly", "db_migration_generate", "db_migration_apply_sandbox"]
    assert all(e["capability"] == "database" for e in events)


async def test_build_provider_reads_databases_yaml(tmp_path: Path, sessionmaker: Any) -> None:
    db = make_sqlite(tmp_path / "books.db")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for name in ("hosts", "models", "policies", "capabilities", "logging"):
        (cfg_dir / f"{name}.example.yaml").write_text((REPO_ROOT / "config" / f"{name}.example.yaml").read_text())
    (cfg_dir / "databases.yaml").write_text(
        f"targets:\n  - {{name: books, dsn_ref: 'literal:{db}', kind: sqlite, environment: staging}}\nlimits: {{max_row_limit: 2}}\n"
    )
    settings = Settings(config_dir=cfg_dir, data_dir=tmp_path / "data", env="test")
    provider = build_provider(load_config(cfg_dir), sessionmaker, settings)
    assert provider.tools.registry.names() == ["books"] and provider.tools.backup_dir == tmp_path / "data" / "artifacts" / "db-backups"
    res = await provider.call(
        _call(ToolName.db_query_readonly, DbQueryArgs(target="books", sql="SELECT * FROM books", row_limit=10), tmp_path)
    )
    assert res.ok and res.data["row_count"] == 2, "row limit is capped by the configuration"
    (cfg_dir / "databases.yaml").unlink()
    empty = build_provider(load_config(cfg_dir), sessionmaker, settings)
    assert empty.tools.registry.names() == []
