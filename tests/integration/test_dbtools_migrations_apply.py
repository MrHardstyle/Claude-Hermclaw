"""db_migration_generate + db_migration_apply_sandbox (P27 27.3/27.4) end to end on real PostgreSQL and SQLite:
alembic (run through the project's own alembic, locally and through a real CommandExecutor), raw SQL in several
styles, a custom command, seeded sandboxes – upgrade → downgrade → upgrade verified with schema fingerprints, broken
downgrades and failing upgrades reported, the sandbox database always dropped, targets never changed."""

from __future__ import annotations

import json
import re
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.dbtools.errors import (
    DB_MIGRATION_FAILED,
    DB_MIGRATION_UNSUPPORTED,
    DB_PATH_INVALID,
    DB_TARGET_UNSUPPORTED,
)
from hermclaw.dbtools.runner import ExecutorMigrationRunner
from hermclaw.dbtools.targets import TargetKind
from hermclaw.tools.executors import SubprocessExecutor
from tests.integration.test_dbtools_support import (
    BASE_URL,
    SECRET_PW,
    command_runs,
    create_database,
    ctx,
    db_events,
    drop_database,
    make_tools,
    pg_dsn,
    sandbox_names,
    target,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")
NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

ENV_PY = """from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
"""

FIRST_REVISION = '''"""create items"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.create_table("items", sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String(50), nullable=False))


def downgrade():
    op.drop_table("items")
'''


def alembic_workspace(ws: Path) -> Path:
    app = ws / "service"
    (app / "migrations" / "versions").mkdir(parents=True)
    (app / "alembic.ini").write_text(
        "[alembic]\nscript_location = %(here)s/migrations\nsqlalchemy.url = postgresql+psycopg://placeholder@localhost/none\n",
        encoding="utf-8",
    )
    (app / "migrations" / "env.py").write_text(ENV_PY, encoding="utf-8")
    (app / "migrations" / "versions" / "0001_create_items.py").write_text(FIRST_REVISION, encoding="utf-8")
    return app


def fill_alembic(path: Path, upgrade: str, downgrade: str) -> None:
    """What the coder does with the skeleton: fill the bodies."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(def upgrade\(\) -> None:\n(?:    .*\n)*?)    pass\n", lambda m: m.group(1) + f"    {upgrade}\n", text, count=1)
    text = re.sub(r"(def downgrade\(\) -> None:\n(?:    .*\n)*?)    pass\n", lambda m: m.group(1) + f"    {downgrade}\n", text, count=1)
    path.write_text(text, encoding="utf-8")


def raw_sql_workspace(ws: Path, *, sqlite: bool = False) -> Path:
    d = ws / "db" / "migrations"
    d.mkdir(parents=True)
    ident = "INTEGER PRIMARY KEY" if sqlite else "serial PRIMARY KEY"
    (d / "001_create_accounts.sql").write_text(
        f"-- migrate:up\nCREATE TABLE accounts (id {ident}, email text NOT NULL);\nCREATE INDEX accounts_email ON accounts (email);\n\n"
        "-- migrate:down\nDROP INDEX accounts_email;\nDROP TABLE accounts;\n",
        encoding="utf-8",
    )
    (d / "002_add_label.sql").write_text(
        "-- migrate:up\nALTER TABLE accounts ADD COLUMN label text DEFAULT '100% ok; really';\n\n-- migrate:down\nALTER TABLE accounts DROP COLUMN label;\n",
        encoding="utf-8",
    )
    return ws / "db"


@pytest.fixture
def no_leftover_sandboxes() -> Iterator[None]:
    before = set(sandbox_names())
    yield
    assert set(sandbox_names()) - before == set(), "every sandbox database and role is dropped"


# --------------------------------------------------------------------------------------------- alembic
async def test_alembic_generate_fill_and_verify_on_postgres(tmp_path: Path, sessionmaker: Any, no_leftover_sandboxes: None) -> None:
    ws = tmp_path / "ws"
    alembic_workspace(ws)
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    c = ctx()
    gen = await tools.db_migration_generate(ws, "add price to items", path="service", ctx=c, now=NOW)
    assert gen.ok, gen.render()
    assert gen.data["framework"] == "alembic" and gen.data["down_revision"] == "0001"
    new_file = ws / gen.data["path"]
    assert new_file.parent == ws / "service" / "migrations" / "versions" and new_file.name.endswith("_add_price_to_items.py")
    fill_alembic(
        new_file, 'op.add_column("items", sa.Column("price", sa.Numeric(10, 2), nullable=True))', 'op.drop_column("items", "price")'
    )

    res = await tools.db_migration_apply_sandbox(ws, path="service", target_kind="postgresql", ctx=c)
    assert res.ok, res.render()
    assert [s["step"] for s in res.data["steps"]] == ["upgrade to predecessor", "upgrade", "downgrade", "upgrade again"]
    assert [s["to"] for s in res.data["steps"]] == ["0001", gen.data["revision"], "0001", gen.data["revision"]]
    assert all(ch["ok"] for ch in res.data["checks"]) and len(res.data["checks"]) == 2
    assert res.data["sandbox"]["sandbox"].startswith("hermclaw_sbx_")
    assert not list((ws / "service").rglob("__pycache__")), "no bytecode written into the workspace"
    runs = [r for r in await command_runs(sessionmaker, "db_migration") if r.job_id == c.job_id]
    assert [r.command for r in runs] == [
        "alembic upgrade 0001",
        f"alembic upgrade {gen.data['revision']}",
        "alembic downgrade 0001",
        f"alembic upgrade {gen.data['revision']}",
    ]
    assert all(r.exit_code == 0 and r.target == "local" for r in runs)
    events = await db_events(sessionmaker, job_id=c.job_id)
    assert [e["tool"] for e in events] == ["db_migration_generate", "db_migration_apply_sandbox"]
    assert events[1]["outcome"] == "ok" and events[1]["framework"] == "alembic" and len(events[1]["steps"]) == 4
    dumped = json.dumps(events, default=str) + json.dumps([(r.stdout_excerpt, r.stderr_excerpt) for r in runs])
    assert not re.search(r"postgres(?:ql)?(?:\+\w+)?://[^/@\s:]+:[^@\s]+@", dumped), "no credential-bearing URL is recorded"
    assert "PGPASSWORD" not in dumped


async def test_alembic_broken_downgrade_and_failing_upgrade_are_reported(
    tmp_path: Path, sessionmaker: Any, no_leftover_sandboxes: None
) -> None:
    ws = tmp_path / "ws"
    alembic_workspace(ws)
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    gen = await tools.db_migration_generate(ws, "add price", path="service", now=NOW)
    new_file = ws / gen.data["path"]
    fill_alembic(new_file, 'op.add_column("items", sa.Column("price", sa.Integer))', "pass  # forgot it")
    res = await tools.db_migration_apply_sandbox(ws, path="service")
    assert not res.ok and res.code == DB_MIGRATION_FAILED
    failed = [ch for ch in res.data["checks"] if not ch["ok"]]
    assert failed[0]["check"] == "downgrade restores the previous schema" and "+ column public.items.price" in failed[0]["diff"]
    assert "schema check failed" in res.message
    new_file.write_text(
        new_file.read_text(encoding="utf-8").replace('op.add_column("items", sa.Column("price", sa.Integer))', 'op.execute("SELEC broken")')
    )
    res = await tools.db_migration_apply_sandbox(ws, path="service")
    assert not res.ok and res.data["steps"][-1]["step"] == "upgrade" and not res.data["steps"][-1]["ok"]
    assert "syntax error" in res.data["steps"][-1]["detail"]


async def test_alembic_on_sqlite_through_the_sandbox_executor(tmp_path: Path, sessionmaker: Any) -> None:
    ws = tmp_path / "ws"
    alembic_workspace(ws)
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    gen = await tools.db_migration_generate(ws, "add sku", path="service", now=NOW)
    fill_alembic(ws / gen.data["path"], 'op.add_column("items", sa.Column("sku", sa.String(20)))', 'op.drop_column("items", "sku")')
    handle = WorkspaceHandle(uuid.uuid4(), uuid.uuid4(), ws, "job", "main", "0" * 40, "repo")
    runner = ExecutorMigrationRunner(SubprocessExecutor(), handle)
    c = ctx()
    res = await tools.db_migration_apply_sandbox(ws, path="service", target_kind="sqlite", runner=runner, ctx=c)
    assert res.ok, res.render()
    assert len(res.data["steps"]) == 4 and all(s["ok"] for s in res.data["steps"])
    assert not (ws / ".hermclaw-dbsandbox").exists(), "the SQLite sandbox inside the workspace is removed again"
    runs = [r for r in await command_runs(sessionmaker, "db_migration") if r.job_id == c.job_id]
    assert len(runs) == 4 and all(r.target == "sandbox" for r in runs)


async def test_alembic_seeded_from_a_target_only_applies_pending(tmp_path: Path, sessionmaker: Any, no_leftover_sandboxes: None) -> None:
    ws = tmp_path / "ws"
    alembic_workspace(ws)
    schema = (
        "CREATE TABLE items (id serial PRIMARY KEY, name varchar(50) NOT NULL);"
        "CREATE TABLE alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY);"
        "INSERT INTO alembic_version VALUES ('0001'); INSERT INTO items (name) VALUES ('kept');"
    )
    name = create_database(schema_sql=schema)
    try:
        tools = make_tools(tmp_path, [target("live", pg_dsn(name))], sessionmaker=sessionmaker)
        gen = await tools.db_migration_generate(ws, "add color", path="service", now=NOW)
        fill_alembic(ws / gen.data["path"], 'op.add_column("items", sa.Column("color", sa.Text))', 'op.drop_column("items", "color")')
        c = ctx()
        res = await tools.db_migration_apply_sandbox(ws, path="service", seed_schema_from="live", ctx=c)
        assert res.ok, res.render()
        assert res.data["seed"]["seeded_from"] == "live" and res.data["seed"]["bookkeeping_tables"] == ["public.alembic_version"]
        assert [s["step"] for s in res.data["steps"]] == ["upgrade", "downgrade", "upgrade again"], "0001 was already applied in the seed"
        with psycopg.connect(f"{BASE_URL}/{name}") as conn:
            cols = [r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'items'")]
            assert "color" not in cols, "the target itself is never migrated"
            assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("0001",)]
        dumps = [r for r in await command_runs(sessionmaker, "db_seed_schema") if r.job_id == c.job_id]
        assert len(dumps) == 2 and all("--schema-only" in r.command or "--data-only" in r.command for r in dumps)
        assert all(SECRET_PW not in r.command for r in dumps), "credentials travel in PGPASSWORD, not argv"
        events = await db_events(sessionmaker, job_id=c.job_id)
        assert events[-1]["target"] == "live" and events[-1]["environment"] == "production" and events[-1]["outcome"] == "ok"
    finally:
        drop_database(name)


# --------------------------------------------------------------------------------------------- raw SQL
async def test_raw_sql_postgres_all_scope_and_generation(tmp_path: Path, sessionmaker: Any, no_leftover_sandboxes: None) -> None:
    ws = tmp_path / "ws"
    raw_sql_workspace(ws)
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    res = await tools.db_migration_apply_sandbox(ws, path="db", scope="all")
    assert res.ok, res.render()
    assert [s["to"] for s in res.data["steps"]] == ["002_add_label", "base", "002_add_label"]
    gen = await tools.db_migration_generate(ws, "add accounts flags", path="db", now=NOW)
    assert gen.ok and gen.data["path"] == "db/migrations/003_add_accounts_flags.sql"
    (ws / gen.data["path"]).write_text(
        "-- migrate:up\nCREATE TABLE flags (account_id int REFERENCES accounts(id), flag text);\n-- migrate:down\nDROP TABLE flags;\n",
        encoding="utf-8",
    )
    res = await tools.db_migration_apply_sandbox(ws, path="db")
    assert res.ok, res.render()
    assert [s["step"] for s in res.data["steps"]] == ["upgrade to predecessor", "upgrade", "downgrade", "upgrade again"]


async def test_raw_sql_down_problems(tmp_path: Path, sessionmaker: Any, no_leftover_sandboxes: None) -> None:
    ws = tmp_path / "ws"
    d = raw_sql_workspace(ws) / "migrations"
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    (d / "002_add_label.sql").write_text(
        "-- migrate:up\nALTER TABLE accounts ADD COLUMN label text;\n-- migrate:down\n-- nothing\n", encoding="utf-8"
    )
    res = await tools.db_migration_apply_sandbox(ws, path="db")
    assert not res.ok and any(not ch["ok"] for ch in res.data["checks"])
    (d / "002_add_label.sql").write_text("ALTER TABLE accounts ADD COLUMN label text;\n", encoding="utf-8")  # no markers: up only
    res = await tools.db_migration_apply_sandbox(ws, path="db")
    assert res.ok and any("downgrade not verified" in w for w in res.data["warnings"])
    (d / "002_add_label.sql").write_text("-- migrate:up\nALTER TABLE nope ADD COLUMN x int;\n-- migrate:down\n", encoding="utf-8")
    res = await tools.db_migration_apply_sandbox(ws, path="db")
    assert not res.ok and 'relation "nope" does not exist' in res.message


async def test_raw_sql_non_transactional_migration(tmp_path: Path, sessionmaker: Any, no_leftover_sandboxes: None) -> None:
    ws = tmp_path / "ws"
    d = raw_sql_workspace(ws) / "migrations"
    (d / "003_concurrent_index.sql").write_text(
        "-- +goose NO TRANSACTION\n-- migrate:up transaction:false\nCREATE INDEX CONCURRENTLY accounts_label ON accounts (label);\n"
        "-- migrate:down transaction:false\nDROP INDEX CONCURRENTLY accounts_label;\n",
        encoding="utf-8",
    )
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    res = await tools.db_migration_apply_sandbox(ws, path="db")
    assert res.ok, res.render()


async def test_raw_sql_sqlite_and_file_escape_attempts(tmp_path: Path, sessionmaker: Any) -> None:
    ws = tmp_path / "ws"
    d = raw_sql_workspace(ws, sqlite=True) / "migrations"
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    res = await tools.db_migration_apply_sandbox(ws, path="db", target_kind="sqlite")
    assert res.ok, res.render()
    evil = tmp_path / "escaped.db"
    (d / "003_escape.sql").write_text(
        f"-- migrate:up\nATTACH DATABASE '{evil}' AS x;\nCREATE TABLE x.t (a);\n-- migrate:down\n", encoding="utf-8"
    )
    res = await tools.db_migration_apply_sandbox(ws, path="db", target_kind="sqlite")
    assert not res.ok and "not authorized" in res.message and not evil.exists()
    (d / "003_escape.sql").write_text(f"-- migrate:up\nVACUUM INTO '{evil}';\n-- migrate:down\n", encoding="utf-8")
    res = await tools.db_migration_apply_sandbox(ws, path="db", target_kind="sqlite")
    assert not res.ok and not evil.exists()
    assert not list((tmp_path / "sqlite-sandboxes").glob("hermclaw_sbx_*")), "SQLite sandboxes are removed"


async def test_raw_sql_seeded_with_bookkeeping(tmp_path: Path, sessionmaker: Any, no_leftover_sandboxes: None) -> None:
    ws = tmp_path / "ws"
    raw_sql_workspace(ws)
    schema = (
        "CREATE TABLE accounts (id serial PRIMARY KEY, email text NOT NULL); CREATE INDEX accounts_email ON accounts (email);"
        "CREATE TABLE schema_migrations (version varchar(255) PRIMARY KEY); INSERT INTO schema_migrations VALUES ('001');"
    )
    name = create_database(schema_sql=schema)
    try:
        tools = make_tools(tmp_path, [target("live", pg_dsn(name))], sessionmaker=sessionmaker)
        res = await tools.db_migration_apply_sandbox(ws, path="db", seed_schema_from="live")
        assert res.ok, res.render()
        assert [s["step"] for s in res.data["steps"]] == ["upgrade", "downgrade", "upgrade again"]
        assert res.data["steps"][0]["detail"] == "up 002_add_label"
    finally:
        drop_database(name)


# --------------------------------------------------------------------------------------------- custom command / refusals
async def test_custom_command_with_downgrade(tmp_path: Path, sessionmaker: Any) -> None:
    ws = tmp_path / "ws"
    (ws / "app").mkdir(parents=True)
    script = "import os, sqlite3; c = sqlite3.connect(os.environ['DATABASE_URL'].split(':///', 1)[1]); c.execute({sql!r}); c.commit()"
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    res = await tools.db_migration_apply_sandbox(
        ws,
        path="app",
        target_kind="sqlite",
        command=[sys.executable, "-c", script.format(sql="CREATE TABLE made (a int)")],
        downgrade_command=[sys.executable, "-c", script.format(sql="DROP TABLE made")],
    )
    assert res.ok, res.render()
    assert res.data["driver"] == "command" and [s["step"] for s in res.data["steps"]] == ["upgrade", "downgrade", "upgrade again"]
    res = await tools.db_migration_apply_sandbox(
        ws, path="app", target_kind="sqlite", command=[sys.executable, "-c", "raise SystemExit(3)"]
    )
    assert not res.ok and "exit 3" in res.message


async def test_apply_refusals(tmp_path: Path, sessionmaker: Any) -> None:
    ws = tmp_path / "ws"
    (ws / "dj" / "shop" / "migrations").mkdir(parents=True)
    (ws / "dj" / "manage.py").write_text("", encoding="utf-8")
    (ws / "dj" / "shop" / "migrations" / "__init__.py").write_text("", encoding="utf-8")
    raw_sql_workspace(ws)
    tools = make_tools(tmp_path, [target("lite", str(tmp_path / "x.db"), kind=TargetKind.sqlite)], sessionmaker=sessionmaker)
    c = ctx()
    assert (await tools.db_migration_apply_sandbox(ws, path="dj", ctx=c)).code == DB_MIGRATION_UNSUPPORTED
    assert (await tools.db_migration_apply_sandbox(ws, path="../etc", ctx=c)).code == DB_PATH_INVALID
    assert (await tools.db_migration_apply_sandbox(ws, path="db", target_kind="mysql", ctx=c)).code == DB_TARGET_UNSUPPORTED
    mismatch = await tools.db_migration_apply_sandbox(ws, path="db", target_kind="postgresql", seed_schema_from="lite", ctx=c)
    assert mismatch.code == DB_TARGET_UNSUPPORTED
    no_server = make_tools(tmp_path, [], sessionmaker=sessionmaker, sandbox=False)
    assert (await no_server.db_migration_apply_sandbox(ws, path="db", ctx=c)).code == "DB_SANDBOX_UNAVAILABLE"
    outcomes = [e["outcome"] for e in await db_events(sessionmaker, job_id=c.job_id)]
    assert outcomes == ["failed", "denied", "failed", "failed", "failed"]


async def test_alembic_env_ignoring_the_sandbox_url_is_detected(tmp_path: Path, sessionmaker: Any) -> None:
    """An env.py that connects to its own database must not pass as a verified migration."""
    ws = tmp_path / "ws"
    app = alembic_workspace(ws)
    elsewhere = tmp_path / "somebody-elses.db"
    (app / "migrations" / "env.py").write_text(
        ENV_PY.replace(
            'connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)',
            f"from sqlalchemy import create_engine\n    connectable = create_engine({f'sqlite:///{elsewhere}'!r}, poolclass=pool.NullPool)",
        ),
        encoding="utf-8",
    )
    tools = make_tools(tmp_path, [], sessionmaker=sessionmaker)
    res = await tools.db_migration_apply_sandbox(ws, path="service", target_kind="sqlite")
    assert not res.ok and "may have migrated a different database" in res.message, res.render()
