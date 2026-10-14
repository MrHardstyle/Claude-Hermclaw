"""Throw-away sandbox databases (P27 27.4/27.6).

PostgreSQL: on the configured sandbox server (admin DSN = secret reference; the role needs ``CREATEDB`` and
``CREATEROLE``) every sandbox gets a database **and** an isolated login role of the same name
``<prefix><12 hex>``: no superuser, no CREATEDB/CREATEROLE, connection limit, password valid for one day only, sent
as a SCRAM-SHA-256 verifier (the clear password never reaches the server log). The database is owned by that role,
``CONNECT`` is revoked from ``PUBLIC``, and a comment records creation time and label (orphan clean-up). Migrations
and restores run as the isolated role – project SQL never runs with the admin's privileges. Only names matching
``<prefix><12 hex>`` are ever dropped.

SQLite: a copy of the source file (online backup API) or an empty file in a private temp directory.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
from psycopg import sql

from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.dbtools.errors import DB_SANDBOX_FAILED, DB_SANDBOX_UNAVAILABLE, DbToolError
from hermclaw.dbtools.policy import DbPolicy
from hermclaw.dbtools.targets import ConnectionInfo, TargetKind, redact_dsn

log = get_logger(__name__)
COMMENT_PREFIX = "hermclaw-sandbox"
_COMMENT_RE = re.compile(r"created=(\S+)")
SCRAM_ITERATIONS = 4096
ROLE_VALIDITY = timedelta(days=1)


def scram_verifier(password: str, *, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS) -> str:
    """PostgreSQL ``SCRAM-SHA-256`` password verifier (RFC 5802/7677) – lets ``CREATE ROLE`` avoid the clear text."""
    salt = salt or os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = base64.b64encode
    return f"SCRAM-SHA-256${iterations}:{b64(salt).decode()}${b64(stored_key).decode()}:{b64(server_key).decode()}"


@dataclass(frozen=True, repr=False)
class SandboxDatabase:
    kind: TargetKind
    name: str
    conn: ConnectionInfo  # as the isolated role, reachable from the orchestrator
    client_conn: ConnectionInfo  # as the isolated role, as the migration command reaches it
    role: str | None = None
    directory: Path | None = None  # SQLite: private directory holding the file
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return f"SandboxDatabase({self.kind.value}:{self.name})"

    def describe(self) -> dict[str, Any]:
        return {"sandbox": self.name, "sandbox_kind": self.kind.value, "sandbox_server": self.conn.display()}


class PgSandboxServer:
    def __init__(
        self,
        admin: ConnectionInfo,
        *,
        policy: DbPolicy,
        client_host: str | None = None,
        client_port: int | None = None,
        connect_timeout: int = 10,
        redactor: Redactor = DEFAULT_REDACTOR,
    ) -> None:
        if admin.kind != TargetKind.postgresql:
            raise DbToolError("the sandbox server must be PostgreSQL", code=DB_SANDBOX_UNAVAILABLE)
        self.admin = admin
        self.policy = policy
        self.prefix = policy.sandbox_prefix
        self.client_host = client_host
        self.client_port = client_port
        self.connect_timeout = connect_timeout
        self.redactor = redactor

    async def _admin_connect(self, database: str | None = None) -> psycopg.AsyncConnection[Any]:
        conn = self.admin if database is None else self.admin.with_database(database)
        try:
            return await psycopg.AsyncConnection.connect(autocommit=True, **conn.psycopg_kwargs(connect_timeout=self.connect_timeout))
        except psycopg.Error as exc:
            raise DbToolError(f"sandbox server {self.admin.display()} is not reachable: {_msg(exc)}", code=DB_SANDBOX_UNAVAILABLE) from None

    async def create(self, *, label: str = "") -> SandboxDatabase:
        name = f"{self.prefix}{uuid.uuid4().hex[:12]}"
        self.policy.check_sandbox_name(name)
        password = secrets.token_urlsafe(24)
        self.redactor.add_literal(password)
        DEFAULT_REDACTOR.add_literal(password)
        ident = sql.Identifier(name)
        valid_until = (datetime.now(UTC) + ROLE_VALIDITY).strftime("%Y-%m-%d %H:%M:%S+00")
        comment = f"{COMMENT_PREFIX} created={datetime.now(UTC).isoformat()} label={re.sub(r'[^A-Za-z0-9_.:-]', '_', label)[:80]}"
        aconn = await self._admin_connect()
        created_role = created_db = False
        try:
            try:
                await aconn.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT "
                        "CONNECTION LIMIT 20 PASSWORD {} VALID UNTIL {}"
                    ).format(ident, sql.Literal(scram_verifier(password)), sql.Literal(valid_until))
                )
                created_role = True
                cur = await aconn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                row = await cur.fetchone()
                if not (row and row[0]):
                    await aconn.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(ident))
                await aconn.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(ident, ident))
                created_db = True
                await aconn.execute(sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC").format(ident))
                await aconn.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(ident, ident))
                await aconn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(ident, sql.Literal(comment)))
            except psycopg.Error as exc:
                await self._drop_with(aconn, name, drop_db=created_db, drop_role=created_role)
                raise DbToolError(f"sandbox database could not be created: {_msg(exc)}", code=DB_SANDBOX_FAILED) from None
        finally:
            await aconn.close()
        conn = self.admin.with_database(name, user=name, password=password)
        client = conn
        if self.client_host or self.client_port:
            client = replace(conn, host=self.client_host or conn.host, port=self.client_port or conn.port)
        log.info("sandbox database created", extra={"sandbox": name, "server": self.admin.display()})
        return SandboxDatabase(TargetKind.postgresql, name, conn, client, role=name)

    async def ensure_extensions(self, sandbox: SandboxDatabase, names: list[str]) -> list[str]:
        """Create extensions (as admin) in the sandbox; returns warnings for the ones that are not available."""
        warnings: list[str] = []
        if not names:
            return warnings
        aconn = await self._admin_connect(sandbox.name)
        try:
            for ext in names:
                try:
                    await aconn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(ext)))
                except psycopg.Error as exc:
                    warnings.append(f"extension {ext} is not available on the sandbox server: {_msg(exc)}")
        finally:
            await aconn.close()
        return warnings

    async def drop(self, sandbox: SandboxDatabase | str) -> bool:
        name = sandbox if isinstance(sandbox, str) else sandbox.name
        self.policy.check_sandbox_name(name)
        try:
            aconn = await self._admin_connect()
        except DbToolError:
            log.exception("sandbox database could not be dropped (server unreachable)", extra={"sandbox": name})
            return False
        try:
            return await self._drop_with(aconn, name, drop_db=True, drop_role=True)
        finally:
            await aconn.close()

    async def _drop_with(self, aconn: psycopg.AsyncConnection[Any], name: str, *, drop_db: bool, drop_role: bool) -> bool:
        self.policy.check_sandbox_name(name)
        ident = sql.Identifier(name)
        ok = True
        if drop_db:
            try:
                await aconn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(ident))
            except psycopg.Error as exc:
                ok = False
                log.error("sandbox database drop failed", extra={"sandbox": name, "error": _msg(exc)})
        if drop_role:
            try:
                await aconn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident))
            except psycopg.Error as exc:
                ok = False
                log.error("sandbox role drop failed", extra={"sandbox": name, "error": _msg(exc)})
        return ok

    async def list_sandboxes(self) -> list[tuple[str, datetime | None]]:
        aconn = await self._admin_connect()
        try:
            pattern = self.prefix.replace("_", r"\_") + "%"
            # databases *and* roles: a crash between CREATE ROLE and CREATE DATABASE leaves a role only
            cur = await aconn.execute(
                "SELECT n.name, shobj_description(d.oid, 'pg_database'), r.rolvaliduntil "
                "FROM (SELECT datname AS name FROM pg_database WHERE datname LIKE %s "
                "      UNION SELECT rolname FROM pg_roles WHERE rolname LIKE %s) n "
                "LEFT JOIN pg_database d ON d.datname = n.name LEFT JOIN pg_roles r ON r.rolname = n.name ORDER BY 1",
                (pattern, pattern),
            )
            rows = await cur.fetchall()
        finally:
            await aconn.close()
        out: list[tuple[str, datetime | None]] = []
        for name, comment, valid_until in rows:
            if not self.policy.is_sandbox_name(str(name)):
                continue
            created: datetime | None = None
            m = _COMMENT_RE.search(str(comment or ""))
            if m and str(comment).startswith(COMMENT_PREFIX):
                with contextlib.suppress(ValueError):
                    created = datetime.fromisoformat(m.group(1))
            if created is None and isinstance(valid_until, datetime):
                created = valid_until - ROLE_VALIDITY  # still being created (no comment yet) or a role-only leftover
            out.append((str(name), created))
        return out

    async def cleanup_orphans(self, *, max_age: timedelta, now: datetime | None = None) -> list[str]:
        """Drop sandbox databases (and roles) older than ``max_age`` – leftovers of crashed runs (P34 recovery)."""
        current = now or datetime.now(UTC)
        dropped: list[str] = []
        for name, created in await self.list_sandboxes():
            if created is not None and current - created < max_age:
                continue
            if await self.drop(name):
                dropped.append(name)
        return dropped


class SqliteSandboxes:
    def __init__(self, *, prefix: str = "hermclaw_sbx_", base_dir: Path | None = None) -> None:
        self.prefix = prefix
        self.base_dir = base_dir

    def create(self, *, source: Path | None = None, base_dir: Path | None = None) -> SandboxDatabase:
        directory_base = base_dir or self.base_dir
        if directory_base is not None:
            directory_base.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=self.prefix, dir=str(directory_base) if directory_base else None))
        path = directory / "sandbox.sqlite3"
        try:
            if source is not None:
                src = sqlite3.connect(f"file:{quote(str(source))}?mode=ro", uri=True)
                dst = sqlite3.connect(str(path))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
            else:
                sqlite3.connect(str(path)).close()
        except sqlite3.Error as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise DbToolError(f"SQLite sandbox could not be created: {exc}", code=DB_SANDBOX_FAILED) from None
        conn = ConnectionInfo(kind=TargetKind.sqlite, path=path)
        return SandboxDatabase(TargetKind.sqlite, directory.name, conn, conn, directory=directory)

    def cleanup_orphans(self, *, max_age: timedelta, now: datetime | None = None) -> list[str]:
        """Remove SQLite sandbox directories older than ``max_age`` (crashed runs) below the sandbox base directory."""
        current = (now or datetime.now(UTC)).timestamp()
        root = self.base_dir or Path(tempfile.gettempdir())
        removed: list[str] = []
        if not root.is_dir():
            return removed
        for entry in root.iterdir():
            if not entry.name.startswith(self.prefix) or entry.is_symlink() or not entry.is_dir():
                continue
            with contextlib.suppress(OSError):
                if current - entry.stat().st_mtime >= max_age.total_seconds():
                    shutil.rmtree(entry)
                    removed.append(entry.name)
        return removed

    def drop(self, sandbox: SandboxDatabase) -> bool:
        directory = sandbox.directory
        if directory is None or not directory.name.startswith(self.prefix):
            return False
        shutil.rmtree(directory, ignore_errors=True)
        parent = directory.parent
        if parent.name == ".hermclaw-dbsandbox":
            with contextlib.suppress(OSError):
                parent.rmdir()  # only when empty
        return not directory.exists()


def _msg(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    lines = str(primary or exc).strip().splitlines()
    return redact_dsn(lines[0] if lines else type(exc).__name__)[:300]


@contextlib.asynccontextmanager
async def pg_sandbox(server: PgSandboxServer, *, label: str = "") -> AsyncIterator[SandboxDatabase]:
    sandbox = await server.create(label=label)
    try:
        yield sandbox
    finally:
        await server.drop(sandbox)


__all__ = ["PgSandboxServer", "SandboxDatabase", "SqliteSandboxes", "pg_sandbox", "scram_verifier"]
