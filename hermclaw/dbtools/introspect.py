"""Schema introspection of a target database (P27 27.1).

SQLAlchemy reflection (batched ``get_multi_*``) on a synchronous engine in a worker thread. PostgreSQL connections are
opened with ``default_transaction_read_only=on`` and a ``statement_timeout``; SQLite files are opened with
``mode=ro`` (URI). The result is a JSON-safe description (schemas → tables with columns/types/nullability/defaults,
primary keys, indexes, unique/check constraints, foreign keys; views and materialized views) plus a compact text
rendering for the model and a fingerprint used by the sandbox migration check.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
from urllib.parse import quote

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL, Engine, Inspector
from sqlalchemy.engine.reflection import ObjectKind
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from hermclaw.dbtools.errors import DB_INTROSPECT_FAILED, DB_QUERY_TIMEOUT, DB_TARGET_UNSUPPORTED, DbToolError
from hermclaw.dbtools.targets import ConnectionInfo, TargetKind

SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast"})
MAX_VIEW_DEFINITION_CHARS = 2000


def readonly_options(statement_timeout_ms: int) -> str:
    """libpq ``options`` enforcing read-only sessions with a statement timeout (and standard string literals)."""
    return (
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={max(1, statement_timeout_ms)} "
        f"-c idle_in_transaction_session_timeout={max(1000, statement_timeout_ms * 2)} "
        "-c standard_conforming_strings=on"
    )


def make_engine(conn: ConnectionInfo, *, read_only: bool, timeout_seconds: float, connect_timeout: int = 10) -> Engine:
    if conn.kind == TargetKind.sqlite:
        assert conn.path is not None
        mode = "ro" if read_only else "rw"
        url = URL.create("sqlite+pysqlite", database=f"file:{quote(str(conn.path))}", query={"mode": mode, "uri": "true"})
        return create_engine(url, poolclass=NullPool, connect_args={"timeout": max(1.0, min(timeout_seconds, 30.0))})
    if conn.kind != TargetKind.postgresql:
        raise DbToolError(f"{conn.kind.value} targets are not supported", code=DB_TARGET_UNSUPPORTED)
    ms = int(timeout_seconds * 1000)
    options = readonly_options(ms) if read_only else f"-c statement_timeout={ms}"
    user_options = conn.extra().get("options", "")
    return create_engine(
        conn.sqlalchemy_url(),
        poolclass=NullPool,
        connect_args={"options": f"{user_options} {options}".strip(), "connect_timeout": connect_timeout},
    )


def _type_name(engine: Engine, col_type: Any) -> str:
    try:
        return str(col_type.compile(dialect=engine.dialect))
    except Exception:
        return str(col_type)


def _key(schema: str | None, name: str) -> tuple[str | None, str]:
    return (schema, name)


def describe_sync(engine: Engine, *, schemas: list[str] | None = None, max_tables: int = 1000) -> dict[str, Any]:
    """Blocking introspection (call in a thread)."""
    with engine.connect() as connection:
        insp: Inspector = inspect(connection)
        dialect = engine.dialect.name
        version = ".".join(str(p) for p in (engine.dialect.server_version_info or ()))
        if dialect == "sqlite":
            schema_names: list[str | None] = [None]
        else:
            available = [s for s in insp.get_schema_names() if s not in SYSTEM_SCHEMAS and not s.startswith(("pg_toast", "pg_temp"))]
            wanted = [s for s in available if s in schemas] if schemas else available
            schema_names = list(wanted)
        out_schemas: list[dict[str, Any]] = []
        total = 0
        truncated = False
        for schema in schema_names:
            table_names = sorted(insp.get_table_names(schema=schema))
            if dialect == "sqlite":
                table_names = [t for t in table_names if not t.startswith("sqlite_")]
            room = max_tables - total
            if room <= 0:
                truncated = truncated or bool(table_names)
                break
            if len(table_names) > room:
                table_names, truncated = table_names[:room], True
            total += len(table_names)
            tables = _describe_tables(engine, insp, schema, table_names) if table_names else []
            views = _describe_views(insp, schema, dialect)
            out_schemas.append({"name": schema or "main", "tables": tables, "views": views})
        return {
            "dialect": dialect,
            "server_version": version,
            "schemas": out_schemas,
            "table_count": total,
            "view_count": sum(len(s["views"]) for s in out_schemas),
            "truncated": truncated,
        }


def _describe_tables(engine: Engine, insp: Inspector, schema: str | None, names: list[str]) -> list[dict[str, Any]]:
    kind = ObjectKind.TABLE
    cols = insp.get_multi_columns(schema=schema, filter_names=names, kind=kind)
    pks = insp.get_multi_pk_constraint(schema=schema, filter_names=names, kind=kind)
    idxs = insp.get_multi_indexes(schema=schema, filter_names=names, kind=kind)
    fks = insp.get_multi_foreign_keys(schema=schema, filter_names=names, kind=kind)
    uqs = insp.get_multi_unique_constraints(schema=schema, filter_names=names, kind=kind)
    try:
        cks = insp.get_multi_check_constraints(schema=schema, filter_names=names, kind=kind)
    except NotImplementedError:
        cks = {}
    try:
        comments = insp.get_multi_table_comment(schema=schema, filter_names=names, kind=kind)
    except NotImplementedError:
        comments = {}
    tables: list[dict[str, Any]] = []
    for name in names:
        key = _key(schema, name)
        columns = [
            {
                "name": c["name"],
                "type": _type_name(engine, c["type"]),
                "nullable": bool(c.get("nullable", True)),
                "default": None if c.get("default") is None else str(c.get("default")),
                **({"comment": c["comment"]} if c.get("comment") else {}),
                **({"identity": True} if c.get("identity") else {}),
                **({"computed": str(c["computed"].get("sqltext"))} if c.get("computed") else {}),
            }
            for c in cols.get(key, [])
        ]
        pk = pks.get(key) or {}
        pk_cols = list(pk.get("constrained_columns") or [])
        indexes = [
            {
                "name": ix.get("name"),
                "columns": [col if col is not None else str(expr) for col, expr in _index_columns(ix)],
                "unique": bool(ix.get("unique")),
            }
            for ix in idxs.get(key, [])
        ]
        foreign_keys = [
            {
                "name": fk.get("name"),
                "columns": list(fk.get("constrained_columns") or []),
                "referred_schema": fk.get("referred_schema"),
                "referred_table": fk.get("referred_table"),
                "referred_columns": list(fk.get("referred_columns") or []),
                **({"options": {k: v for k, v in (fk.get("options") or {}).items() if v}} if fk.get("options") else {}),
            }
            for fk in fks.get(key, [])
        ]
        table: dict[str, Any] = {
            "name": name,
            "columns": columns,
            "primary_key": {"name": pk.get("name"), "columns": pk_cols} if pk_cols else None,
            "indexes": sorted(indexes, key=lambda i: str(i["name"])),
            "unique_constraints": sorted(
                ({"name": u.get("name"), "columns": list(u.get("column_names") or [])} for u in uqs.get(key, [])),
                key=lambda u: (str(u["name"]), u["columns"]),
            ),
            "foreign_keys": sorted(foreign_keys, key=lambda f: (str(f["name"]), f["columns"])),
            "check_constraints": sorted(
                ({"name": ck.get("name"), "sqltext": str(ck.get("sqltext"))} for ck in cks.get(key, [])),
                key=lambda ck: (str(ck["name"]), ck["sqltext"]),
            ),
        }
        comment = (comments.get(key) or {}).get("text")
        if comment:
            table["comment"] = comment
        tables.append(table)
    return tables


def _index_columns(ix: Any) -> list[tuple[str | None, Any]]:
    names = list(ix.get("column_names") or [])
    exprs = list(ix.get("expressions") or [])
    if len(exprs) < len(names):
        exprs += [None] * (len(names) - len(exprs))
    return list(zip(names, exprs, strict=False))


def _describe_views(insp: Inspector, schema: str | None, dialect: str) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    names = [(v, False) for v in insp.get_view_names(schema=schema)]
    if dialect == "postgresql":
        try:
            names += [(v, True) for v in insp.get_materialized_view_names(schema=schema)]
        except NotImplementedError:
            pass
    for name, materialized in sorted(names):
        try:
            definition = insp.get_view_definition(name, schema=schema) or ""
        except (NotImplementedError, SQLAlchemyError):
            definition = ""
        definition = " ".join(str(definition).split())
        if len(definition) > MAX_VIEW_DEFINITION_CHARS:
            definition = definition[:MAX_VIEW_DEFINITION_CHARS] + "…"
        views.append({"name": name, "materialized": materialized, "definition": definition})
    return views


async def describe(
    conn: ConnectionInfo,
    *,
    schemas: list[str] | None = None,
    timeout_seconds: float = 120,
    connect_timeout: int = 10,
    max_tables: int = 1000,
    read_only: bool = True,
) -> dict[str, Any]:
    """Introspect ``conn`` (read-only); raises :class:`DbToolError` with ``DB_INTROSPECT_FAILED``/``DB_QUERY_TIMEOUT``."""
    if conn.kind == TargetKind.sqlite and (conn.path is None or not conn.path.is_file()):
        raise DbToolError(f"SQLite database file {conn.path} does not exist", code=DB_INTROSPECT_FAILED)

    def work() -> dict[str, Any]:
        engine = make_engine(conn, read_only=read_only, timeout_seconds=timeout_seconds, connect_timeout=connect_timeout)
        try:
            return describe_sync(engine, schemas=schemas, max_tables=max_tables)
        finally:
            engine.dispose()

    try:
        return await asyncio.wait_for(asyncio.to_thread(work), timeout_seconds + connect_timeout + 5)
    except TimeoutError:
        raise DbToolError(f"introspection did not finish within {timeout_seconds:.0f}s", code=DB_QUERY_TIMEOUT) from None
    except SQLAlchemyError as exc:
        message = str(getattr(exc, "orig", None) or exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        code = DB_QUERY_TIMEOUT if "statement timeout" in message else DB_INTROSPECT_FAILED
        raise DbToolError(f"introspection failed: {message[:500]}", code=code) from None


def fingerprint(description: dict[str, Any]) -> str:
    """Stable digest of the schema structure (tables, columns, keys, indexes, views) – not of the data."""
    canonical = json.dumps(description.get("schemas", []), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def table_names(description: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for schema in description.get("schemas", []):
        prefix = "" if schema["name"] in ("main", "public") else f"{schema['name']}."
        out += [prefix + t["name"] for t in schema.get("tables", [])]
    return sorted(out)


def render(description: dict[str, Any], *, title: str = "", limit: int = 12000) -> str:
    """Compact text for the model: one line per column, keys and indexes indented."""
    head = f"{title}: " if title else ""
    lines = [
        f"{head}{description.get('dialect')} {description.get('server_version') or ''}".rstrip()
        + f" – {description.get('table_count', 0)} tables, {description.get('view_count', 0)} views"
        + (" (truncated)" if description.get("truncated") else "")
    ]
    for schema in description.get("schemas", []):
        for table in schema.get("tables", []):
            pk = set((table.get("primary_key") or {}).get("columns") or [])
            lines.append(f"TABLE {schema['name']}.{table['name']}" + (f" -- {table['comment']}" if table.get("comment") else ""))
            for col in table["columns"]:
                bits = [col["type"], "NULL" if col["nullable"] else "NOT NULL"]
                if col["name"] in pk:
                    bits.append("PK")
                if col.get("default") is not None:
                    bits.append(f"DEFAULT {col['default']}")
                lines.append(f"  {col['name']} {' '.join(bits)}")
            for fk in table.get("foreign_keys", []):
                ref = f"{fk['referred_schema']}.{fk['referred_table']}" if fk.get("referred_schema") else fk["referred_table"]
                lines.append(f"  FK ({', '.join(fk['columns'])}) -> {ref}({', '.join(fk['referred_columns'])})")
            for ix in table.get("indexes", []):
                lines.append(f"  INDEX {ix['name']} ({', '.join(str(c) for c in ix['columns'])}){' UNIQUE' if ix['unique'] else ''}")
            for uq in table.get("unique_constraints", []):
                lines.append(f"  UNIQUE {uq['name']} ({', '.join(uq['columns'])})")
        for view in schema.get("views", []):
            kind = "MATERIALIZED VIEW" if view["materialized"] else "VIEW"
            lines.append(f"{kind} {schema['name']}.{view['name']}: {view['definition'][:300]}")
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit - 40] + f"\n…[{len(text) - limit + 40} chars truncated]"
    return text


__all__ = ["describe", "describe_sync", "fingerprint", "make_engine", "readonly_options", "render", "table_names"]
