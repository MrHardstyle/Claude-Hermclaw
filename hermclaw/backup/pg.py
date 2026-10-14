"""PostgreSQL connection handling for backup/restore.

The Hermclaw ``database_url`` (SQLAlchemy form, e.g. ``postgresql+psycopg://hermclaw@/hermclaw?host=/run/postgresql``)
is split into libpq parameters. The client tools receive them as ``PGHOST``/``PGPORT``/``PGUSER``/``PGDATABASE``
environment variables and the password through ``PGPASSFILE`` (0600 file in a private 0700 temp dir that is removed
afterwards) – argv never carries credentials, so they cannot show up in ``ps``, logs, events or command records.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from hermclaw.backup.errors import BACKUP_CONFIG_INVALID, BACKUP_TOOL_MISSING, BackupError
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.security.secrets import SecretStore

PG_LIB_DIR = Path("/usr/lib/postgresql")
PG_TOOLS = ("pg_dump", "pg_dumpall", "pg_restore", "psql")
# URL query parameters -> libpq environment variables
_PARAM_ENV: dict[str, str] = {
    "host": "PGHOST",
    "port": "PGPORT",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslcrl": "PGSSLCRL",
    "application_name": "PGAPPNAME",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "gssencmode": "PGGSSENCMODE",
    "options": "PGOPTIONS",
}
_DBNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")
_LIST_TABLES_SQL = """
SELECT n.nspname, c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
ORDER BY 1, 2
"""
_TOC_LINE_RE = re.compile(r"^\s*\d+;\s+\d+\s+\d+\s+(?P<rest>.*)$")


@dataclass(frozen=True)
class PgConn:
    """libpq connection parameters. ``password`` is kept out of ``repr``."""

    dbname: str
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    params: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_url(cls, url: str, *, password: str | None = None) -> PgConn:
        try:
            parsed = make_url(url)
        except ArgumentError:
            raise BackupError("database URL is not a valid URL", code=BACKUP_CONFIG_INVALID) from None
        if not parsed.drivername.startswith("postgresql"):
            raise BackupError("backup supports PostgreSQL database URLs only", code=BACKUP_CONFIG_INVALID)
        if not parsed.database:
            raise BackupError("database URL has no database name", code=BACKUP_CONFIG_INVALID)
        params: dict[str, str] = {}
        for key, value in parsed.query.items():
            if key not in _PARAM_ENV:
                raise BackupError(f"unsupported database URL parameter '{key}'", code=BACKUP_CONFIG_INVALID)
            params[key] = ",".join(value) if isinstance(value, tuple) else str(value)
        host = params.pop("host", None) or parsed.host
        port_s = params.pop("port", None)
        port = parsed.port or (int(port_s) if port_s and port_s.isdigit() else None)
        return cls(
            dbname=parsed.database,
            host=host,
            port=port,
            user=parsed.username,
            password=password if password is not None else parsed.password,
            params=tuple(sorted(params.items())),
        )

    def with_db(self, dbname: str) -> PgConn:
        return replace(self, dbname=dbname)

    def with_password(self, password: str | None) -> PgConn:
        return replace(self, password=password)

    def display(self) -> str:
        """Credential-free description for logs/reports."""
        where = self.host or "default socket"
        port = f":{self.port}" if self.port else ""
        return f"{self.user or '<os user>'}@{where}{port}/{self.dbname}"

    def connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"dbname": self.dbname}
        if self.host:
            kwargs["host"] = self.host
        if self.port:
            kwargs["port"] = self.port
        if self.user:
            kwargs["user"] = self.user
        if self.password is not None:
            kwargs["password"] = self.password
        kwargs.update(dict(self.params))
        return kwargs

    def libpq_env(self, passfile: Path | None = None) -> dict[str, str]:
        env: dict[str, str] = {"PGDATABASE": self.dbname}
        if self.host:
            env["PGHOST"] = self.host
        if self.port:
            env["PGPORT"] = str(self.port)
        if self.user:
            env["PGUSER"] = self.user
        for key, value in self.params:
            env[_PARAM_ENV[key]] = value
        if passfile is not None:
            env["PGPASSFILE"] = str(passfile)
        return env

    async def connect(self, *, autocommit: bool = False) -> psycopg.AsyncConnection[Any]:
        kwargs = self.connect_kwargs()
        kwargs.setdefault("connect_timeout", 15)
        kwargs.setdefault("application_name", "hermclaw-backup")
        try:
            return await psycopg.AsyncConnection.connect(autocommit=autocommit, **kwargs)
        except psycopg.Error as exc:
            raise BackupError(f"cannot connect to {self.display()}: {_first_line(exc)}", code="BACKUP_DB_CONNECT_FAILED") from None


def resolve_conn(url: str, password_ref: str | None, *, store: SecretStore | None = None) -> PgConn:
    password = (store or SecretStore()).resolve(password_ref) if password_ref else None
    conn = PgConn.from_url(url, password=password)
    if conn.password:
        DEFAULT_REDACTOR.add_literal(conn.password)
    return conn


def _escape_pgpass(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


@contextlib.contextmanager
def pg_tool_env(conn: PgConn) -> Iterator[dict[str, str]]:
    """libpq environment for a client tool; the password goes into a private passfile removed on exit."""
    if conn.password is None:
        yield conn.libpq_env()
        return
    if "\n" in conn.password or "\r" in conn.password:
        raise BackupError("database password must not contain line breaks", code=BACKUP_CONFIG_INVALID)
    directory = Path(tempfile.mkdtemp(prefix="hermclaw-pgpass-"))
    try:
        directory.chmod(0o700)
        passfile = directory / "pgpass"
        fd = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"*:*:*:*:{_escape_pgpass(conn.password)}\n")
        yield conn.libpq_env(passfile)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ================================================================================================ binaries
def installed_pg_majors(lib_dir: Path = PG_LIB_DIR) -> list[int]:
    if not lib_dir.is_dir():
        return []
    return sorted((int(p.name) for p in lib_dir.iterdir() if p.name.isdigit() and (p / "bin").is_dir()), reverse=True)


def find_pg_tool(tool: str, *, bin_dir: Path | None = None, min_major: int | None = None, lib_dir: Path = PG_LIB_DIR) -> str:
    """Configured dir, else the newest installed major ≥ the server's (pg_dump must not be older), else PATH."""
    if tool not in PG_TOOLS:
        raise ValueError(tool)
    if bin_dir is not None:
        candidate = bin_dir / tool
        if not candidate.is_file():
            raise BackupError(f"{tool} not found in pg_bin_dir {bin_dir}", code=BACKUP_TOOL_MISSING)
        return str(candidate)
    for major in installed_pg_majors(lib_dir):
        if min_major is not None and major < min_major:
            continue
        candidate = lib_dir / str(major) / "bin" / tool
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(tool)
    if found is None:
        raise BackupError(f"{tool} is not installed (PostgreSQL client tools are required)", code=BACKUP_TOOL_MISSING)
    return found


# ================================================================================================ catalog
async def server_major(aconn: psycopg.AsyncConnection[Any]) -> tuple[int, str]:
    cur = await aconn.execute("SELECT current_setting('server_version_num')::int, current_setting('server_version')")
    row = await cur.fetchone()
    if row is None:
        raise BackupError("cannot read the server version", code="BACKUP_DB_CONNECT_FAILED")
    return int(row[0]) // 10000, str(row[1])


async def list_tables(aconn: psycopg.AsyncConnection[Any]) -> list[tuple[str, str]]:
    cur = await aconn.execute(_LIST_TABLES_SQL)
    return [(str(s), str(t)) for s, t in await cur.fetchall()]


async def count_tables(aconn: psycopg.AsyncConnection[Any], tables: list[tuple[str, str]]) -> dict[str, int]:
    """Exact ``count(*)`` of every table (in the caller's transaction/snapshot)."""
    counts: dict[str, int] = {}
    for schema, table in tables:
        cur = await aconn.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
        row = await cur.fetchone()
        counts[f"{schema}.{table}"] = int(row[0]) if row else 0
    return counts


async def list_extensions(aconn: psycopg.AsyncConnection[Any]) -> list[str]:
    cur = await aconn.execute("SELECT extname FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY 1")
    return [str(r[0]) for r in await cur.fetchall()]


async def alembic_revision(aconn: psycopg.AsyncConnection[Any]) -> str | None:
    cur = await aconn.execute("SELECT to_regclass('public.alembic_version') IS NOT NULL")
    row = await cur.fetchone()
    if not row or not row[0]:
        return None
    cur = await aconn.execute("SELECT version_num FROM public.alembic_version ORDER BY 1")
    revs = [str(r[0]) for r in await cur.fetchall()]
    return ",".join(revs) if revs else None


async def database_is_empty(aconn: psycopg.AsyncConnection[Any]) -> bool:
    return not await list_tables(aconn)


def valid_dbname(name: str) -> str:
    if not _DBNAME_RE.match(name):
        raise BackupError(f"invalid database name {name!r}", code=BACKUP_CONFIG_INVALID)
    return name


# ================================================================================================ TOC
@dataclass(frozen=True)
class FilteredToc:
    listing: str
    extensions: list[str]
    skipped: list[str]


def filter_toc(listing: str) -> FilteredToc:
    """Comment out ``EXTENSION`` and ``COMMENT … EXTENSION`` entries of ``pg_restore -l`` output.

    Extensions are created up front by the restore (``CREATE EXTENSION IF NOT EXISTS`` or via the template DB);
    ``COMMENT ON EXTENSION`` needs extension ownership and would abort ``--exit-on-error`` for non-superusers.
    """
    out: list[str] = []
    extensions: list[str] = []
    skipped: list[str] = []
    for line in listing.splitlines():
        match = _TOC_LINE_RE.match(line)
        if match:
            parts = match.group("rest").split()
            if len(parts) >= 3 and parts[0] == "EXTENSION":
                extensions.append(parts[2])
                skipped.append(match.group("rest").strip())
                out.append(";" + line)
                continue
            if len(parts) >= 4 and parts[0] == "COMMENT" and parts[2] == "EXTENSION":
                skipped.append(match.group("rest").strip())
                out.append(";" + line)
                continue
        out.append(line)
    return FilteredToc(listing="\n".join(out) + "\n", extensions=extensions, skipped=skipped)


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return DEFAULT_REDACTOR.text(text[0][:300]) if text else type(exc).__name__
