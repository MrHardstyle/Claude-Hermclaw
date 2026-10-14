"""db_introspect (27.1) and db_query_readonly (27.2) against real PostgreSQL 16 and SQLite databases.

Read-only enforcement is checked on both layers: the static guard (denied events) and the database itself
(``SET TRANSACTION READ ONLY`` / ``mode=ro``) with statements that pass or bypass the guard."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from hermclaw.dbtools.errors import (
    DB_CONNECT_FAILED,
    DB_QUERY_REJECTED,
    DB_QUERY_TIMEOUT,
    DB_READ_ONLY_VIOLATION,
    DbToolError,
)
from hermclaw.dbtools.query import query_postgres, query_sqlite_sync
from hermclaw.dbtools.sqlguard import CheckedStatement
from hermclaw.dbtools.targets import DbLimits, TargetKind, parse_dsn
from tests.integration.test_dbtools_support import (
    BASE_URL,
    SECRET_PW,
    SHOP_SCHEMA,
    create_database,
    ctx,
    db_events,
    drop_database,
    make_sqlite,
    make_tools,
    pg_dsn,
    target,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(scope="module")
def shop_db() -> Iterator[str]:
    name = create_database(schema_sql=SHOP_SCHEMA)
    yield name
    drop_database(name)


def _tools(tmp_path: Path, sessionmaker: Any, shop_db: str, **kw: Any) -> Any:
    sqlite_path = make_sqlite(tmp_path / "books.db")
    return make_tools(
        tmp_path,
        [target("shop", pg_dsn(shop_db)), target("books", str(sqlite_path), kind=TargetKind.sqlite)],
        sessionmaker=sessionmaker,
        sandbox=False,
        **kw,
    )


def _count(db: str, table: str) -> int:
    with psycopg.connect(f"{BASE_URL}/{db}") as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert row is not None
        return int(row[0])


# --------------------------------------------------------------------------------------------- 27.1
async def test_introspect_postgres_fixture_schema(tmp_path: Path, sessionmaker: Any, shop_db: str) -> None:
    tools = _tools(tmp_path, sessionmaker, shop_db)
    c = ctx()
    res = await tools.db_introspect("shop", ctx=c)
    assert res.ok, res.render()
    schema = res.data["schema"]
    tables = {f"{s['name']}.{t['name']}": t for s in schema["schemas"] for t in s["tables"]}
    assert {"public.customers", "public.orders", "public.audit_log", "inventory.items"} <= set(tables)
    cols = {c["name"]: c for c in tables["public.customers"]["columns"]}
    assert cols["email"]["type"] == "TEXT" and cols["email"]["nullable"] is False
    assert cols["name"]["type"] == "VARCHAR(100)" and cols["name"]["nullable"] is True
    assert "nextval" in str(cols["id"]["default"]) and cols["created_at"]["default"] == "now()"
    assert cols["score"]["type"] == "NUMERIC(5, 2)"
    assert tables["public.customers"]["primary_key"]["columns"] == ["id"]
    assert tables["public.customers"]["comment"] == "shop customers"
    assert tables["public.customers"]["unique_constraints"][0]["columns"] == ["email"]
    orders = tables["public.orders"]
    assert orders["foreign_keys"][0]["referred_table"] == "customers" and orders["foreign_keys"][0]["columns"] == ["customer_id"]
    assert orders["foreign_keys"][0]["options"] == {"ondelete": "CASCADE"}
    assert {"orders_customer_idx"} <= {i["name"] for i in orders["indexes"]}
    assert any(ck["sqltext"].replace(" ", "").startswith("total>=") for ck in orders["check_constraints"])
    assert {c["name"]: c for c in orders["columns"]}["id"].get("identity") is True
    views = {v["name"]: v for s in schema["schemas"] for v in s["views"]}
    assert views["big_orders"]["materialized"] is False and "total > (100)" in views["big_orders"]["definition"]
    assert views["order_totals"]["materialized"] is True
    assert "FK (customer_id) -> public.customers(id)" in res.text and "TABLE inventory.items" in res.text
    again = await tools.db_introspect("shop", ctx=c)
    assert again.data["fingerprint"] == res.data["fingerprint"], "fingerprint is stable"
    events = await db_events(sessionmaker, job_id=c.job_id)
    assert [e["outcome"] for e in events] == ["ok", "ok"] and events[0]["table_count"] >= 4
    assert events[0]["server"].startswith("postgresql://postgres@") and SECRET_PW not in json.dumps(events, default=str)


async def test_introspect_sqlite(tmp_path: Path, sessionmaker: Any, shop_db: str) -> None:
    tools = _tools(tmp_path, sessionmaker, shop_db)
    res = await tools.db_introspect("books", ctx=ctx())
    assert res.ok, res.render()
    tables = {t["name"]: t for t in res.data["schema"]["schemas"][0]["tables"]}
    assert set(tables) == {"authors", "books"}
    books = tables["books"]
    assert books["foreign_keys"][0]["referred_table"] == "authors"
    assert {c["name"]: c for c in books["columns"]}["title"]["default"] == "'untitled'"
    assert "books_author" in {i["name"] for i in books["indexes"]}
    assert res.data["schema"]["schemas"][0]["views"][0]["name"] == "prolific"


async def test_introspect_unreachable_target_never_leaks_the_password(tmp_path: Path, sessionmaker: Any) -> None:
    tools = make_tools(tmp_path, [target("gone", pg_dsn(f"hc_missing_{uuid.uuid4().hex[:6]}"))], sessionmaker=sessionmaker, sandbox=False)
    c = ctx()
    res = await tools.db_introspect("gone", ctx=c)
    assert not res.ok and res.code == "DB_INTROSPECT_FAILED" and SECRET_PW not in res.render()
    unknown = await tools.db_introspect("nope", ctx=c)
    assert unknown.code == "DB_TARGET_UNKNOWN"
    assert SECRET_PW not in json.dumps(await db_events(sessionmaker, job_id=c.job_id), default=str)


# --------------------------------------------------------------------------------------------- 27.2
async def test_query_readonly_rows_params_and_limits(tmp_path: Path, sessionmaker: Any, shop_db: str) -> None:
    tools = _tools(tmp_path, sessionmaker, shop_db, limits=DbLimits(max_cell_chars=16))
    c = ctx()
    res = await tools.db_query_readonly(
        "shop", "SELECT id, email, score FROM customers WHERE id <= %(max)s ORDER BY id", {"max": 5}, row_limit=3, ctx=c
    )
    assert res.ok and res.data["truncated"] is True and res.data["row_count"] == 3
    assert [col["name"] for col in res.data["columns"]] == ["id", "email", "score"]
    assert res.data["rows"][0] == [1, "c1@example.org", "1.00"]
    assert "truncated at the row limit" in res.text
    full = await tools.db_query_readonly("shop", "SELECT count(*) AS n FROM orders", ctx=c)
    assert full.data["rows"] == [[50]] and full.data["truncated"] is False
    capped = await tools.db_query_readonly("shop", "SELECT repeat('x', 100) AS long_text", ctx=c)
    assert capped.data["rows"][0][0] == "x" * 16 + "…"
    for sql in ("EXPLAIN SELECT * FROM orders WHERE customer_id = 3", "SHOW default_transaction_read_only", "TABLE inventory.items"):
        res = await tools.db_query_readonly("shop", sql, ctx=c)
        assert res.ok, (sql, res.render())
    assert (await tools.db_query_readonly("shop", "SHOW default_transaction_read_only", ctx=c)).data["rows"] == [["on"]]
    events = await db_events(sessionmaker, job_id=c.job_id)
    assert events[0]["row_count"] == 3 and events[0]["truncated"] is True and "rows" not in events[0], "no user data in events"


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO customers (email) VALUES ('evil@x')",
        "UPDATE customers SET name = 'x'",
        "DELETE FROM orders",
        "DROP TABLE orders",
        "ALTER TABLE customers ADD COLUMN pwned int",
        "TRUNCATE audit_log",
        "WITH gone AS (DELETE FROM orders RETURNING *) SELECT count(*) FROM gone",
        "SELECT 1; DELETE FROM orders",
        "SELECT * INTO stolen FROM customers",
        "COPY customers TO '/tmp/customers.csv'",
        "SELECT pg_terminate_backend(pg_backend_pid())",
        "SELECT nextval('customers_id_seq')",
        "EXPLAIN ANALYZE DELETE FROM orders",
    ],
)
async def test_write_attempts_are_rejected_and_audited(tmp_path: Path, sessionmaker: Any, shop_db: str, sql: str) -> None:
    tools = _tools(tmp_path, sessionmaker, shop_db)
    c = ctx()
    res = await tools.db_query_readonly("shop", sql, ctx=c)
    assert not res.ok and res.code == DB_QUERY_REJECTED, res.render()
    events = await db_events(sessionmaker, job_id=c.job_id)
    assert len(events) == 1 and events[0]["outcome"] == "denied" and events[0]["_severity"] == "warning"
    assert events[0]["sql"] == sql and events[0]["environment"] == "production"
    assert _count(shop_db, "orders") == 50 and _count(shop_db, "customers") == 30


async def test_database_enforces_read_only_beyond_the_guard(tmp_path: Path, sessionmaker: Any, shop_db: str) -> None:
    tools = _tools(tmp_path, sessionmaker, shop_db)
    c = ctx()
    # passes the static guard (user function), the read-only transaction refuses the INSERT inside it
    res = await tools.db_query_readonly("shop", "SELECT log_visit()", ctx=c)
    assert not res.ok and res.code == DB_READ_ONLY_VIOLATION, res.render()
    assert _count(shop_db, "audit_log") == 0
    # bypassing the guard entirely: the database layer alone still refuses writes
    conn = parse_dsn(pg_dsn(shop_db), TargetKind.postgresql)
    for raw in ("INSERT INTO audit_log DEFAULT VALUES", "UPDATE customers SET name = 'x'", "CREATE TABLE pwned (a int)"):
        with pytest.raises(DbToolError) as exc:
            await query_postgres(conn, CheckedStatement(raw, "X", False, ()), None, row_limit=10, timeout_seconds=5)
        assert exc.value.code == DB_READ_ONLY_VIOLATION, raw
    assert _count(shop_db, "audit_log") == 0 and _count(shop_db, "customers") == 30
    events = await db_events(sessionmaker, job_id=c.job_id)
    assert events[0]["outcome"] == "failed" and events[0]["code"] == DB_READ_ONLY_VIOLATION


async def test_statement_timeout(tmp_path: Path, sessionmaker: Any, shop_db: str) -> None:
    tools = _tools(tmp_path, sessionmaker, shop_db)
    c = ctx()
    started = time.monotonic()
    res = await tools.db_query_readonly("shop", "SELECT pg_sleep(5)", timeout=0.5, ctx=c)
    assert not res.ok and res.code == DB_QUERY_TIMEOUT and time.monotonic() - started < 4
    events = await db_events(sessionmaker, job_id=c.job_id)
    assert events[0]["outcome"] == "timed_out"
    # the server is not left with the sleeping backend
    with psycopg.connect(f"{BASE_URL}/{shop_db}") as conn:
        row = conn.execute("SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(5)%' AND pid <> pg_backend_pid()").fetchone()
        assert row is not None and row[0] == 0


async def test_sqlite_query_and_enforcement(tmp_path: Path, sessionmaker: Any, shop_db: str) -> None:
    tools = _tools(tmp_path, sessionmaker, shop_db)
    c = ctx()
    res = await tools.db_query_readonly("books", "SELECT title FROM books WHERE author_id = ? ORDER BY id", [1], ctx=c)
    assert res.ok and res.data["rows"] == [["One"], ["Two"]]
    pragma = await tools.db_query_readonly("books", "PRAGMA table_info(books)", ctx=c)
    assert pragma.ok and [r[1] for r in pragma.data["rows"]] == ["id", "author_id", "title", "isbn"]
    for sql in ("DELETE FROM books", "ATTACH DATABASE '/tmp/x.db' AS x", "PRAGMA journal_mode = DELETE", "SELECT load_extension('x')"):
        assert (await tools.db_query_readonly("books", sql, ctx=c)).code == DB_QUERY_REJECTED, sql
    conn = parse_dsn(str(tmp_path / "books.db"), TargetKind.sqlite)
    for raw in ("DELETE FROM books", "CREATE TABLE pwned (a)", "PRAGMA writable_schema = 1"):
        with pytest.raises(DbToolError) as exc:
            query_sqlite_sync(conn, CheckedStatement(raw, "X", False, ()), None, row_limit=5, timeout_seconds=5)
        assert exc.value.code == DB_READ_ONLY_VIOLATION, raw
    count = await tools.db_query_readonly("books", "SELECT count(*) FROM books", ctx=c)
    assert count.data["rows"] == [[3]]
    slow = await tools.db_query_readonly(
        "books", "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c", timeout=0.3, ctx=c
    )
    assert slow.code == DB_QUERY_TIMEOUT


async def test_unreachable_query_target(tmp_path: Path, sessionmaker: Any) -> None:
    tools = make_tools(tmp_path, [target("gone", pg_dsn(f"hc_missing_{uuid.uuid4().hex[:6]}"))], sessionmaker=sessionmaker, sandbox=False)
    res = await tools.db_query_readonly("gone", "SELECT 1", ctx=ctx())
    assert res.code == DB_CONNECT_FAILED and SECRET_PW not in res.render()


async def test_percent_signs_without_params_and_secret_masking(tmp_path: Path, sessionmaker: Any, shop_db: str) -> None:
    tools = _tools(tmp_path, sessionmaker, shop_db)
    c = ctx()
    like = await tools.db_query_readonly("shop", "SELECT count(*) FROM customers WHERE email LIKE 'c1%'", ctx=c)
    assert like.ok and like.data["rows"] == [[11]], like.render()
    # a registered secret (the target's DSN password) never reaches the model, even as query output
    leak = await tools.db_query_readonly("shop", f"SELECT '{SECRET_PW}' AS pw, 'postgresql://u:{SECRET_PW}@h/db' AS url", ctx=c)
    assert leak.ok and SECRET_PW not in leak.render() and SECRET_PW not in json.dumps(leak.to_dict())
    assert SECRET_PW not in json.dumps(await db_events(sessionmaker, job_id=c.job_id), default=str)


async def test_cancelled_query_is_still_audited(tmp_path: Path, sessionmaker: Any, shop_db: str) -> None:
    import asyncio

    tools = _tools(tmp_path, sessionmaker, shop_db)
    c = ctx()
    task = asyncio.create_task(tools.db_query_readonly("shop", "SELECT pg_sleep(10)", ctx=c))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    events = await db_events(sessionmaker, job_id=c.job_id)
    assert len(events) == 1 and events[0]["code"] == "CANCELLED" and events[0]["outcome"] == "failed"
    with psycopg.connect(f"{BASE_URL}/{shop_db}") as conn:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(10)%' AND pid <> pg_backend_pid()"
            ).fetchone()
            if row is not None and row[0] == 0:
                break
            time.sleep(0.2)
        assert row is not None and row[0] == 0, "the cancelled query does not keep running on the server"
