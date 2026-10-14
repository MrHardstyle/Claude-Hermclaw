"""Read-only queries against a target (P27 27.2).

Two independent layers:

1. static: :func:`hermclaw.dbtools.sqlguard.check_readonly` (one read statement, no data-modifying CTE, no
   ``INTO``/``COPY``/locking clause, no deny-listed function);
2. database: PostgreSQL sessions start with ``default_transaction_read_only=on`` and ``standard_conforming_strings=on``,
   every query runs in a transaction opened with ``SET TRANSACTION READ ONLY`` plus ``statement_timeout`` /
   ``lock_timeout`` and is **always rolled back**; SELECT-like statements run through a server-side cursor (only
   ``row_limit + 1`` rows are fetched). SQLite opens the file with ``mode=ro`` (URI), sets ``PRAGMA query_only``,
   installs an authorizer that allows only reads (and read-only PRAGMAs) and a progress handler enforcing the timeout.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import psycopg
import psycopg.errors

from hermclaw.dbtools.errors import (
    DB_CONNECT_FAILED,
    DB_QUERY_FAILED,
    DB_QUERY_TIMEOUT,
    DB_READ_ONLY_VIOLATION,
    DB_TARGET_UNSUPPORTED,
    DbToolError,
)
from hermclaw.dbtools.introspect import readonly_options
from hermclaw.dbtools.sqlguard import DENIED_FUNCTIONS, SQLITE_READ_PRAGMAS, CheckedStatement, check_readonly
from hermclaw.dbtools.targets import ConnectionInfo, TargetKind, redact_dsn

Params = Mapping[str, Any] | Sequence[Any] | None
# PRAGMAs whose argument is an object name (reading), not an assignment
SQLITE_ARG_PRAGMAS = frozenset(
    {"table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo", "foreign_key_list", "foreign_key_check"}
    | {"integrity_check", "quick_check"}
)
_SQLITE_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)


@dataclass
class QueryResult:
    columns: list[dict[str, Any]]
    rows: list[list[Any]]
    truncated: bool
    statement: str
    duration_ms: int = 0
    notices: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "statement": self.statement,
        }

    def render(self, limit: int = 12000) -> str:
        names = [c["name"] for c in self.columns]
        lines = [" | ".join(names)] if names else []
        for row in self.rows:
            lines.append(" | ".join("NULL" if v is None else (v if isinstance(v, str) else json.dumps(v, default=str)) for v in row))
        tail = f"({self.row_count} row{'s' if self.row_count != 1 else ''}{', truncated at the row limit' if self.truncated else ''})"
        text = "\n".join([*lines, tail])
        if len(text) > limit:
            cut = text[: limit - 80].rsplit("\n", 1)[0]
            text = f"{cut}\n…[output truncated at {limit} chars] {tail}"
        return text


def json_safe(value: Any, max_chars: int) -> Any:
    """Driver values → JSON-safe, length-bounded cells."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "…"
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        text = "\\x" + raw[: max_chars // 2].hex()
        return text + ("…" if len(raw) > max_chars // 2 else "")
    if isinstance(value, Decimal | uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, dict | list | tuple):
        try:
            text = json.dumps(value, default=str)
        except (TypeError, ValueError):
            text = str(value)
        if len(text) <= max_chars:
            return json.loads(text) if isinstance(value, dict | list) else text
        return text[:max_chars] + "…"
    text = str(value)
    return text if len(text) <= max_chars else text[:max_chars] + "…"


def _encode_param(value: Any) -> Any:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list):
        return [_encode_param(v) for v in value]
    raise DbToolError(f"query parameters must be JSON scalars or lists, got {type(value).__name__}", code=DB_QUERY_FAILED)


def _params(params: Params) -> Any:
    if params is None:
        return None
    if isinstance(params, Mapping):
        return {str(k): _encode_param(v) for k, v in params.items()}
    if isinstance(params, str | bytes):
        raise DbToolError("query parameters must be a list or an object", code=DB_QUERY_FAILED)
    return [_encode_param(v) for v in params]


def _pg_message(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    text = str(primary or exc).strip().splitlines()
    return redact_dsn(text[0] if text else type(exc).__name__)[:500]


async def query_postgres(
    conn: ConnectionInfo,
    checked: CheckedStatement,
    params: Params,
    *,
    row_limit: int,
    timeout_seconds: float,
    connect_timeout: int = 10,
    max_cell_chars: int = 2000,
) -> QueryResult:
    ms = max(1, int(timeout_seconds * 1000))
    bound = _params(params)
    started = time.monotonic()
    kwargs = conn.psycopg_kwargs(options=readonly_options(ms), connect_timeout=connect_timeout)
    try:
        async with asyncio.timeout(timeout_seconds + connect_timeout + 5):
            try:
                aconn = await psycopg.AsyncConnection.connect(autocommit=False, **kwargs)
            except psycopg.Error as exc:
                raise DbToolError(f"cannot connect to {conn.display()}: {_pg_message(exc)}", code=DB_CONNECT_FAILED) from None
            try:
                async with aconn.cursor() as cur:
                    await cur.execute("SET TRANSACTION READ ONLY")
                    await cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(ms),))
                    await cur.execute("SELECT set_config('lock_timeout', %s, true)", (str(min(ms, 10_000)),))
                if checked.server_cursor:
                    async with aconn.cursor(name=f"hermclaw_ro_{uuid.uuid4().hex[:12]}") as scur:
                        await scur.execute(checked.sql, bound)
                        raw_rows = await scur.fetchmany(row_limit + 1)
                        description = scur.description
                else:
                    async with aconn.cursor() as ccur:
                        await ccur.execute(checked.sql, bound)
                        description = ccur.description
                        raw_rows = await ccur.fetchmany(row_limit + 1) if description else []
            finally:
                with contextlib.suppress(Exception):
                    await aconn.rollback()
                with contextlib.suppress(Exception):
                    await aconn.close()
    except TimeoutError:
        raise DbToolError(f"query did not finish within {timeout_seconds:g}s", code=DB_QUERY_TIMEOUT) from None
    except psycopg.errors.QueryCanceled:
        raise DbToolError(f"query cancelled by statement_timeout ({timeout_seconds:g}s)", code=DB_QUERY_TIMEOUT) from None
    except psycopg.errors.ReadOnlySqlTransaction as exc:
        raise DbToolError(f"read-only transaction refused the statement: {_pg_message(exc)}", code=DB_READ_ONLY_VIOLATION) from None
    except psycopg.Error as exc:
        raise DbToolError(f"query failed: {_pg_message(exc)}", code=DB_QUERY_FAILED) from None
    columns = [{"name": c.name, "type": getattr(c, "type_display", None) or str(c.type_code)} for c in (description or [])]
    truncated = len(raw_rows) > row_limit
    rows = [[json_safe(v, max_cell_chars) for v in r] for r in raw_rows[:row_limit]]
    return QueryResult(columns, rows, truncated, checked.statement, int((time.monotonic() - started) * 1000))


def _sqlite_authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _src: str | None) -> int:
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, _SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        name = (arg2 or "").lower()
        return sqlite3.SQLITE_DENY if name in DENIED_FUNCTIONS else sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        if name in SQLITE_READ_PRAGMAS and (arg2 is None or name in SQLITE_ARG_PRAGMAS):
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def query_sqlite_sync(
    conn: ConnectionInfo, checked: CheckedStatement, params: Params, *, row_limit: int, timeout_seconds: float, max_cell_chars: int = 2000
) -> QueryResult:
    assert conn.path is not None
    started = time.monotonic()
    deadline = started + timeout_seconds
    try:
        db = sqlite3.connect(f"file:{quote(str(conn.path))}?mode=ro", uri=True, timeout=min(timeout_seconds, 30.0), check_same_thread=False)
    except sqlite3.Error as exc:
        raise DbToolError(f"cannot open {conn.display()}: {exc}", code=DB_CONNECT_FAILED) from None
    try:
        db.execute("PRAGMA query_only = ON")
        db.set_authorizer(_sqlite_authorizer)
        db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        bound = _params(params)
        cur = db.execute(checked.sql, bound if bound is not None else ())
        description = cur.description
        raw_rows = cur.fetchmany(row_limit + 1) if description else []
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "interrupted" in msg and time.monotonic() > deadline:
            raise DbToolError(f"query did not finish within {timeout_seconds:g}s", code=DB_QUERY_TIMEOUT) from None
        if "readonly" in msg or "read-only" in msg or "not authorized" in msg or "query_only" in msg:
            raise DbToolError(f"read-only connection refused the statement: {msg}", code=DB_READ_ONLY_VIOLATION) from None
        raise DbToolError(f"query failed: {msg[:500]}", code=DB_QUERY_FAILED) from None
    except sqlite3.DatabaseError as exc:
        msg = str(exc)
        code = DB_READ_ONLY_VIOLATION if "not authorized" in msg else DB_QUERY_FAILED
        raise DbToolError(f"query failed: {msg[:500]}", code=code) from None
    except (sqlite3.Error, ValueError, OverflowError) as exc:
        raise DbToolError(f"query failed: {str(exc)[:500]}", code=DB_QUERY_FAILED) from None
    finally:
        db.close()
    columns = [{"name": d[0], "type": None} for d in (description or [])]
    truncated = len(raw_rows) > row_limit
    rows = [[json_safe(v, max_cell_chars) for v in r] for r in raw_rows[:row_limit]]
    return QueryResult(columns, rows, truncated, checked.statement, int((time.monotonic() - started) * 1000))


async def query_readonly(
    conn: ConnectionInfo,
    sql: str,
    params: Params = None,
    *,
    row_limit: int = 100,
    timeout_seconds: float = 30,
    connect_timeout: int = 10,
    max_cell_chars: int = 2000,
) -> QueryResult:
    """Validate and run one read-only statement. Raises ``DbPolicyError`` (rejected) or ``DbToolError``."""
    if conn.kind == TargetKind.postgresql:
        checked = check_readonly(sql, "postgresql")
        return await query_postgres(
            conn, checked, params, row_limit=row_limit, timeout_seconds=timeout_seconds, connect_timeout=connect_timeout, max_cell_chars=max_cell_chars
        )
    if conn.kind == TargetKind.sqlite:
        checked = check_readonly(sql, "sqlite")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    query_sqlite_sync, conn, checked, params, row_limit=row_limit, timeout_seconds=timeout_seconds, max_cell_chars=max_cell_chars
                ),
                timeout_seconds + 15,
            )
        except TimeoutError:
            raise DbToolError(f"query did not finish within {timeout_seconds:g}s", code=DB_QUERY_TIMEOUT) from None
    raise DbToolError(f"{conn.kind.value} targets are not supported", code=DB_TARGET_UNSUPPORTED)


__all__ = ["QueryResult", "json_safe", "query_postgres", "query_readonly", "query_sqlite_sync"]
