"""Helpers for the database tool suites (P27): throw-away PostgreSQL target databases, SQLite files, a configured
:class:`DbTools` instance and event/command_runs readers. No tests here."""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy import select

from hermclaw.contracts.events import EventType
from hermclaw.dbtools.runner import LocalMigrationRunner
from hermclaw.dbtools.targets import DatabaseTarget, DbLimits, DbToolsConfig, SandboxServerConfig, TargetEnvironment, TargetKind
from hermclaw.dbtools.tools import DbTools
from hermclaw.persistence.models import CommandRun, Event
from hermclaw.security.secrets import SecretStore

ADMIN_URL = os.environ.get("HERMCLAW_TEST_ADMIN_URL", "postgresql://postgres@127.0.0.1:5432/postgres")
BASE_URL = ADMIN_URL.rsplit("/", 1)[0]
#: a password that must never show up in events, command_runs or tool output (trust auth ignores it)
SECRET_PW = "Sup3r-Secret-Pw-4711"

SHOP_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA inventory;
CREATE TABLE customers (
    id serial PRIMARY KEY,
    email text NOT NULL UNIQUE,
    name varchar(100),
    created_at timestamptz NOT NULL DEFAULT now(),
    score numeric(5,2) DEFAULT 0
);
COMMENT ON TABLE customers IS 'shop customers';
CREATE TABLE orders (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id int NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    total numeric(10,2) NOT NULL CHECK (total >= 0),
    note text
);
CREATE INDEX orders_customer_idx ON orders (customer_id);
CREATE INDEX customers_name_trgm ON customers USING gin (name gin_trgm_ops);
CREATE TABLE inventory.items (sku text PRIMARY KEY, qty int NOT NULL DEFAULT 0);
CREATE TABLE audit_log (id serial PRIMARY KEY, at timestamptz DEFAULT now());
CREATE VIEW big_orders AS SELECT id, total FROM orders WHERE total > 100;
CREATE MATERIALIZED VIEW order_totals AS SELECT customer_id, sum(total) AS total FROM orders GROUP BY customer_id;
CREATE FUNCTION log_visit() RETURNS int LANGUAGE sql AS 'INSERT INTO audit_log DEFAULT VALUES RETURNING id';
INSERT INTO customers (email, name, score) SELECT 'c' || g || '@example.org', 'Customer ' || g, g % 100 FROM generate_series(1, 30) g;
INSERT INTO orders (customer_id, total, note) SELECT (g % 30) + 1, g * 7.5, 'order ' || g FROM generate_series(1, 50) g;
INSERT INTO inventory.items VALUES ('A-1', 3), ('B-2', 0);
REFRESH MATERIALIZED VIEW order_totals;
"""

SQLITE_SCHEMA = """
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL, born INTEGER);
CREATE TABLE books (
    id INTEGER PRIMARY KEY,
    author_id INTEGER NOT NULL REFERENCES authors(id),
    title TEXT NOT NULL DEFAULT 'untitled',
    isbn TEXT UNIQUE
);
CREATE INDEX books_author ON books(author_id);
CREATE VIEW prolific AS SELECT author_id, count(*) AS n FROM books GROUP BY author_id;
INSERT INTO authors (name, born) VALUES ('Ann', 1950), ('Bob', 1960), ('Cid', NULL);
INSERT INTO books (author_id, title, isbn) VALUES (1, 'One', 'i1'), (1, 'Two', 'i2'), (2, 'Three', 'i3');
"""


def create_database(prefix: str = "hc_dbt_", schema_sql: str | None = None) -> str:
    name = f"{prefix}{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    if schema_sql:
        with psycopg.connect(f"{BASE_URL}/{name}", autocommit=True) as conn:
            conn.execute(schema_sql.encode())
    return name


def drop_database(name: str) -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@contextmanager
def temp_database(schema_sql: str | None = None, prefix: str = "hc_dbt_") -> Iterator[str]:
    name = create_database(prefix, schema_sql)
    try:
        yield name
    finally:
        drop_database(name)


def pg_dsn(name: str, *, password: str | None = SECRET_PW) -> str:
    """URL of database ``name`` as user postgres with a (trust-ignored) password that must stay secret."""
    host = BASE_URL.split("://", 1)[1].split("@")[-1]
    return f"postgresql://postgres:{password}@{host}/{name}" if password else f"postgresql://postgres@{host}/{name}"


def make_sqlite(path: Path, schema_sql: str = SQLITE_SCHEMA) -> Path:
    db = sqlite3.connect(path)
    db.executescript(schema_sql)
    db.commit()
    db.close()
    return path


def sandbox_names() -> list[str]:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        rows = conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'hermclaw\\_sbx\\_%' ORDER BY 1").fetchall()
        roles = conn.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'hermclaw\\_sbx\\_%'").fetchall()
    return [str(r[0]) for r in rows] + [f"role:{r[0]}" for r in roles]


def target(
    name: str, dsn: str, *, kind: TargetKind = TargetKind.postgresql, env: TargetEnvironment = TargetEnvironment.production
) -> DatabaseTarget:
    return DatabaseTarget(name=name, dsn_ref=f"literal:{dsn}", kind=kind, environment=env, read_only=True)


def make_tools(
    tmp_path: Path,
    targets: list[DatabaseTarget],
    *,
    sessionmaker: Any = None,
    sandbox: bool = True,
    limits: DbLimits | None = None,
    **config: Any,
) -> DbTools:
    cfg = DbToolsConfig(
        targets=targets,
        sandbox=SandboxServerConfig(admin_dsn_ref=f"literal:{ADMIN_URL}") if sandbox else None,
        limits=limits or DbLimits(),
        backup_dir=tmp_path / "backups",
        sqlite_sandbox_dir=tmp_path / "sqlite-sandboxes",
        migration_python=sys.executable,
        **config,
    )
    return DbTools.from_config(
        cfg,
        artifacts_dir=tmp_path / "artifacts",
        sessionmaker=sessionmaker,
        secrets=SecretStore(env="test"),
        migration_runner=LocalMigrationRunner(),
    )


async def db_events(
    sessionmaker: Any, *, tool: str | None = None, job_id: uuid.UUID | None = None, target: str | None = None
) -> list[dict[str, Any]]:
    """``db.tool`` event payloads (plus ``_severity``/``_job_id``), oldest first."""
    stmt = select(Event).where(Event.event_type == EventType.DB_TOOL).order_by(Event.sequence)
    if job_id is not None:
        stmt = stmt.where(Event.job_id == job_id)
    async with sessionmaker() as session:
        rows = (await session.execute(stmt)).scalars().all()
    payloads = [{**dict(r.payload or {}), "_severity": r.severity, "_job_id": r.job_id} for r in rows]
    return [p for p in payloads if (tool is None or p.get("tool") == tool) and (target is None or p.get("target") == target)]


def ctx(capability: str = "database", **kw: Any) -> Any:
    from hermclaw.dbtools.policy import DbCallContext

    return DbCallContext(job_id=kw.pop("job_id", uuid.uuid4()), capability=capability, actor=kw.pop("actor", "agent"), **kw)


async def command_runs(sessionmaker: Any, classification: str | None = None) -> list[CommandRun]:
    async with sessionmaker() as session:
        rows = (await session.execute(select(CommandRun).order_by(CommandRun.created_at))).scalars().all()
    return [r for r in rows if classification is None or r.classification == classification]
