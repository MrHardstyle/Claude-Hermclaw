"""Backups of target databases and restore tests (P27 27.5/27.6); schema seeding of sandbox databases (27.4).

Backup (``db_backup``): PostgreSQL ``pg_dump --format=custom`` with the credentials in ``PGPASSWORD`` (environment, never
argv). The dump runs on a snapshot exported by a ``REPEATABLE READ READ ONLY`` transaction (``pg_export_snapshot`` +
``pg_dump --snapshot``) in which the row counts of every table are taken – the recorded counts describe exactly the
dumped data. SQLite uses the online backup API inside one read transaction. The file goes to the artifact store
(``<backup_dir>/<target>/<UTC timestamp>-<id>.<ext>``, mode 0600) and an ``artifacts`` row (``kind=backup``, sha256,
metadata: tables + row counts, extensions, server version, consistency flag).

Restore test (``db_restore_test``): checksum check, then ``pg_restore --no-owner --no-privileges --exit-on-error`` into a
throw-away sandbox database as its isolated role (extensions pre-created by the sandbox admin, ``COMMENT ON
EXTENSION``/event triggers filtered from the TOC because they need ownership/superuser), then table list + exact row
counts compared with the snapshot of the backup. The sandbox is always dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.logging import get_logger
from hermclaw.dbtools.errors import (
    DB_ARTIFACT_INVALID,
    DB_ARTIFACT_UNKNOWN,
    DB_BACKUP_FAILED,
    DB_CONNECT_FAILED,
    DB_RESTORE_FAILED,
    DB_SANDBOX_FAILED,
    DB_SANDBOX_UNAVAILABLE,
    DB_TARGET_UNSUPPORTED,
    DbToolError,
)
from hermclaw.dbtools.migrations import BOOKKEEPING_TABLES
from hermclaw.dbtools.policy import DbAuditor, DbCallContext
from hermclaw.dbtools.runner import ProcessResult
from hermclaw.dbtools.sandbox import PgSandboxServer, SandboxDatabase, SqliteSandboxes
from hermclaw.dbtools.targets import ConnectionInfo, DbLimits, ResolvedTarget, TargetKind, redact_dsn
from hermclaw.persistence.models import Artifact

log = get_logger(__name__)
BACKUP_KIND = "backup"
FORMAT_PG = "pg_custom"
FORMAT_SQLITE = "sqlite"
PG_LIB_DIR = Path("/usr/lib/postgresql")
_TOC_RE = re.compile(r"^\s*\d+;\s+\d+\s+\d+\s+(?P<rest>.*)$")
_EXT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")
_LIST_TABLES_SQL = """
SELECT n.nspname, c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
ORDER BY 1, 2
"""


# ============================================================================================ tool binaries
def installed_pg_majors(lib_dir: Path = PG_LIB_DIR) -> list[int]:
    if not lib_dir.is_dir():
        return []
    return sorted((int(p.name) for p in lib_dir.iterdir() if p.name.isdigit() and (p / "bin").is_dir()), reverse=True)


def find_pg_tool(tool: str, *, min_major: int | None = None, bin_dir: Path | None = None, lib_dir: Path = PG_LIB_DIR) -> str:
    """Path of ``pg_dump``/``pg_restore``: configured dir, else the newest installed major ≥ the server's, else PATH."""
    if tool not in ("pg_dump", "pg_restore", "psql"):
        raise ValueError(tool)
    if bin_dir is not None:
        candidate = bin_dir / tool
        if not candidate.is_file():
            raise DbToolError(f"{tool} not found in the configured pg_bin_dir", code=DB_SANDBOX_UNAVAILABLE)
        return str(candidate)
    for major in installed_pg_majors(lib_dir):
        if min_major is not None and major < min_major:
            continue
        candidate = lib_dir / str(major) / "bin" / tool
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(tool)
    if found is None:
        raise DbToolError(f"{tool} is not installed (PostgreSQL client tools are required)", code=DB_SANDBOX_UNAVAILABLE)
    return found


# ============================================================================================ row counts
@dataclass
class TableSnapshot:
    tables: dict[str, int | None]  # qualified name -> exact row count (None: not counted)
    consistent: bool  # counts were taken on the snapshot of the dump
    server_version: str = ""
    skipped: int = 0  # tables beyond max_count_tables (listed, not counted)

    def to_dict(self) -> dict[str, Any]:
        return {"tables": self.tables, "consistent": self.consistent, "server_version": self.server_version, "skipped": self.skipped}


async def pg_list_tables(aconn: psycopg.AsyncConnection[Any]) -> list[tuple[str, str]]:
    cur = await aconn.execute(_LIST_TABLES_SQL)
    return [(str(s), str(t)) for s, t in await cur.fetchall()]


async def pg_count_tables(
    aconn: psycopg.AsyncConnection[Any], tables: Sequence[tuple[str, str]], *, max_tables: int
) -> tuple[dict[str, int | None], int]:
    """Exact ``count(*)`` per table, each in a savepoint (a timeout leaves ``None`` and keeps the snapshot)."""
    counts: dict[str, int | None] = {}
    skipped = 0
    for k, (schema, table) in enumerate(tables):
        name = f"{schema}.{table}"
        if k >= max_tables:
            counts[name] = None
            skipped += 1
            continue
        try:
            async with aconn.transaction():
                cur = await aconn.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
                row = await cur.fetchone()
                counts[name] = int(row[0]) if row else None
        except psycopg.Error as exc:
            log.warning("row count failed", extra={"table": name, "error": redact_dsn(str(exc))[:200]})
            counts[name] = None
    return counts, skipped


def sqlite_counts(db: sqlite3.Connection, *, max_tables: int) -> tuple[dict[str, int | None], int]:
    names = [
        str(r[0])
        for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")
    ]
    counts: dict[str, int | None] = {}
    skipped = 0
    for k, name in enumerate(names):
        if k >= max_tables:
            counts[f"main.{name}"] = None
            skipped += 1
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        row = db.execute(f"SELECT count(*) FROM {quoted}").fetchone()
        counts[f"main.{name}"] = int(row[0]) if row else None
    return counts, skipped


def compare_snapshots(expected: dict[str, int | None], actual: dict[str, int | None]) -> dict[str, Any]:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatches = [
        {"table": t, "expected": expected[t], "actual": actual[t]}
        for t in sorted(set(expected) & set(actual))
        if expected[t] is not None and actual[t] is not None and expected[t] != actual[t]
    ]
    not_compared = sorted(t for t in set(expected) & set(actual) if expected[t] is None or actual[t] is None)
    return {
        "ok": not missing and not extra and not mismatches,
        "tables": len(expected),
        "missing_tables": missing,
        "extra_tables": extra,
        "count_mismatches": mismatches,
        "not_compared": not_compared,
    }


# ============================================================================================ files / artifacts
def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _safe_component(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:100] or "target"


class ArtifactStore:
    """``artifacts`` rows for database backups; files live below ``base_dir``."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None, base_dir: Path) -> None:
        self.sessionmaker = sessionmaker
        self.base_dir = base_dir

    def _require(self) -> async_sessionmaker[AsyncSession]:
        if self.sessionmaker is None:
            raise DbToolError("the artifact store (database) is not configured", code=DB_BACKUP_FAILED)
        return self.sessionmaker

    async def add(
        self, *, name: str, path: Path, media_type: str, sha256: str, size: int, metadata: dict[str, Any], ctx: DbCallContext
    ) -> uuid.UUID:
        maker = self._require()
        artifact_id = uuid.uuid4()
        async with maker() as session, session.begin():
            session.add(
                Artifact(
                    id=artifact_id,
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    kind=BACKUP_KIND,
                    name=name[:300],
                    path=str(path),
                    media_type=media_type,
                    size_bytes=size,
                    sha256=sha256,
                    metadata_=metadata,
                )
            )
        return artifact_id

    async def get_backup(self, artifact_id: str | uuid.UUID) -> Artifact:
        maker = self._require()
        try:
            key = artifact_id if isinstance(artifact_id, uuid.UUID) else uuid.UUID(str(artifact_id).strip())
        except ValueError:
            raise DbToolError("artifact id must be a UUID returned by db_backup", code=DB_ARTIFACT_INVALID) from None
        async with maker() as session:
            row = (await session.execute(select(Artifact).where(Artifact.id == key))).scalar_one_or_none()
        if row is None:
            raise DbToolError(f"backup artifact {key} does not exist", code=DB_ARTIFACT_UNKNOWN)
        if row.kind != BACKUP_KIND or not isinstance((row.metadata_ or {}).get("dbtools"), dict):
            raise DbToolError(f"artifact {key} is not a database backup of the DB tools", code=DB_ARTIFACT_INVALID)
        return row

    def checked_path(self, row: Artifact) -> Path:
        """The artifact file – must lie below the backup directory (a tampered row cannot point elsewhere)."""
        base = self.base_dir.resolve()
        path = Path(row.path).resolve()
        if base not in path.parents:
            raise DbToolError("backup artifact path is outside the backup directory", code=DB_ARTIFACT_INVALID)
        if not path.is_file():
            raise DbToolError("backup artifact file is missing", code=DB_ARTIFACT_INVALID)
        return path


# ============================================================================================ TOC handling
@dataclass
class RestoreList:
    lines: list[str]
    extensions: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def filter_toc(listing: str) -> RestoreList:
    """Restore list without entries an unprivileged owner cannot restore; collects the extensions of the dump."""
    out = RestoreList([])
    for line in listing.splitlines():
        m = _TOC_RE.match(line)
        if not m:
            out.lines.append(line)
            continue
        rest = m.group("rest")
        parts = rest.split()
        if rest.startswith("EXTENSION - ") and len(parts) >= 3:
            if _EXT_NAME_RE.match(parts[2]):
                out.extensions.append(parts[2])
            out.lines.append(line)
            continue
        if rest.startswith("COMMENT - EXTENSION ") or rest.startswith("EVENT TRIGGER "):
            out.dropped.append(rest.strip()[:120])
            continue
        out.lines.append(line)
    return out


# ============================================================================================ service
@dataclass
class BackupService:
    auditor: DbAuditor
    store: ArtifactStore
    limits: DbLimits = field(default_factory=DbLimits)
    pg_bin_dir: Path | None = None
    sqlite_sandboxes: SqliteSandboxes = field(default_factory=SqliteSandboxes)

    # ------------------------------------------------------------------------------ helpers
    async def _run(self, ctx: DbCallContext, argv: list[str], env: dict[str, str], *, classification: str) -> ProcessResult:
        return await self.auditor.run_command(
            ctx, argv, env=env, timeout=self.limits.command_timeout_seconds, classification=classification
        )

    def _ensure_ok(self, result: ProcessResult, what: str, code: str) -> None:
        if result.ok:
            return
        reason = "timed out" if result.timed_out else (result.error or f"exit {result.exit_code}")
        detail = redact_dsn(self.auditor.redactor.text(result.summary(800)))
        raise DbToolError(f"{what} failed ({reason}): {detail}", code=code)

    async def _connect(self, conn: ConnectionInfo, *, options: str = "", autocommit: bool = False) -> psycopg.AsyncConnection[Any]:
        try:
            return await psycopg.AsyncConnection.connect(
                autocommit=autocommit, **conn.psycopg_kwargs(options=options, connect_timeout=self.limits.connect_timeout_seconds)
            )
        except psycopg.Error as exc:
            raise DbToolError(f"cannot connect to {conn.display()}: {_msg(exc)}", code=DB_CONNECT_FAILED) from None

    # ------------------------------------------------------------------------------ backup
    async def backup(self, resolved: ResolvedTarget, ctx: DbCallContext, *, backup_dir: Path) -> dict[str, Any]:
        kind = resolved.target.kind
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        short = uuid.uuid4().hex[:8]
        directory = backup_dir / _safe_component(resolved.name)
        await asyncio.to_thread(_make_private_dir, directory)
        if kind == TargetKind.postgresql:
            final = directory / f"{stamp}-{short}.dump"
            partial = final.with_suffix(".dump.partial")
            try:
                snapshot, extensions, tool = await self._pg_dump(resolved.conn, partial, ctx)
            except BaseException:
                with contextlib.suppress(OSError):
                    partial.unlink()
                raise
            media, fmt = "application/octet-stream", FORMAT_PG
        elif kind == TargetKind.sqlite:
            assert resolved.conn.path is not None
            final = directory / f"{stamp}-{short}.sqlite3"
            partial = final.with_suffix(".sqlite3.partial")
            source = resolved.conn.path
            if not source.is_file():
                raise DbToolError(f"SQLite database {source.name} does not exist", code=DB_BACKUP_FAILED)
            try:
                snapshot = await asyncio.to_thread(_sqlite_backup, source, partial, self.limits.max_count_tables)
            except sqlite3.Error as exc:
                with contextlib.suppress(OSError):
                    partial.unlink()
                raise DbToolError(f"SQLite backup failed: {exc}", code=DB_BACKUP_FAILED) from None
            extensions, tool, media, fmt = [], "sqlite3 backup API", "application/vnd.sqlite3", FORMAT_SQLITE
        else:
            raise DbToolError(f"{kind.value} targets are not supported", code=DB_TARGET_UNSUPPORTED)
        await asyncio.to_thread(os.chmod, partial, 0o600)
        await asyncio.to_thread(os.replace, partial, final)
        digest, size = await asyncio.to_thread(sha256_file, final)
        metadata = {
            "dbtools": {
                "format": fmt,
                "target": resolved.name,
                "environment": resolved.target.environment.value,
                "server": resolved.conn.display(),
                "tool": tool,
                "extensions": extensions,
                "created_at": datetime.now(UTC).isoformat(),
                **snapshot.to_dict(),
            }
        }
        name = f"{_safe_component(resolved.name)}-{stamp}{final.suffix}"
        try:
            artifact_id = await self.store.add(
                name=name, path=final, media_type=media, sha256=digest, size=size, metadata=metadata, ctx=ctx
            )
        except BaseException:
            with contextlib.suppress(OSError):
                final.unlink()
            raise
        return {
            "artifact_id": str(artifact_id),
            "name": name,
            "format": fmt,
            "size_bytes": size,
            "sha256": digest,
            "tables": len(snapshot.tables),
            "rows": sum(v for v in snapshot.tables.values() if v is not None),
            "consistent": snapshot.consistent,
            "extensions": extensions,
        }

    async def _pg_dump(self, conn: ConnectionInfo, dest: Path, ctx: DbCallContext) -> tuple[TableSnapshot, list[str], str]:
        hold_ms = int((self.limits.command_timeout_seconds + 120) * 1000)
        count_ms = int(self.limits.count_timeout_seconds * 1000)
        aconn = await self._connect(conn, options=f"-c default_transaction_read_only=on -c idle_in_transaction_session_timeout={hold_ms}")
        try:
            await aconn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur = await aconn.execute("SELECT current_setting('server_version_num')::int, current_setting('server_version')")
            row = await cur.fetchone()
            version_num, version = (int(row[0]), str(row[1])) if row else (0, "")
            snapshot_id: str | None = None
            try:  # not in a savepoint: snapshots cannot be exported from a subtransaction
                cur = await aconn.execute("SELECT pg_export_snapshot()")
                snap_row = await cur.fetchone()
                snapshot_id = str(snap_row[0]) if snap_row else None
            except psycopg.Error as exc:  # e.g. not allowed on this server – counts are then only approximately in sync
                log.warning("pg_export_snapshot failed", extra={"error": _msg(exc)})
                await aconn.rollback()
                await aconn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            await aconn.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(count_ms)))
            tables = await pg_list_tables(aconn)
            counts, skipped = await pg_count_tables(aconn, tables, max_tables=self.limits.max_count_tables)
            tool = find_pg_tool("pg_dump", min_major=version_num // 10000 or None, bin_dir=self.pg_bin_dir)
            argv = [tool, "--format=custom", "--no-password", "--lock-wait-timeout=60000", f"--file={dest}"]
            if snapshot_id:
                argv.append(f"--snapshot={snapshot_id}")
            result = await self._run(ctx, argv, conn.libpq_env(), classification="db_backup")
            self._ensure_ok(result, "pg_dump", DB_BACKUP_FAILED)
        finally:
            with contextlib.suppress(Exception):
                await aconn.rollback()
            await aconn.close()
        extensions = await self._dump_extensions(dest, ctx)
        return TableSnapshot(counts, snapshot_id is not None, version, skipped), extensions, tool

    async def _dump_extensions(self, dump: Path, ctx: DbCallContext) -> list[str]:
        listing = await self._run(
            ctx, [find_pg_tool("pg_restore", bin_dir=self.pg_bin_dir), "--list", str(dump)], {}, classification="db_backup"
        )
        self._ensure_ok(listing, "pg_restore --list", DB_BACKUP_FAILED)
        return filter_toc(listing.stdout).extensions

    # ------------------------------------------------------------------------------ restore
    async def pg_restore_into(
        self, dump: Path, sandbox: SandboxDatabase, server: PgSandboxServer, ctx: DbCallContext, *, code: str = DB_RESTORE_FAILED
    ) -> list[str]:
        """Restore ``dump`` into the sandbox as its isolated role; returns warnings (filtered entries, extensions)."""
        tool = find_pg_tool("pg_restore", bin_dir=self.pg_bin_dir)
        listing = await self._run(ctx, [tool, "--list", str(dump)], {}, classification="db_restore")
        self._ensure_ok(listing, "pg_restore --list", code)
        toc = filter_toc(listing.stdout)
        warnings = [f"skipped (needs ownership/superuser): {d}" for d in toc.dropped]
        warnings += await server.ensure_extensions(sandbox, toc.extensions)
        workdir = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="hermclaw-restore-"))
        try:
            list_file = workdir / "restore.list"
            await asyncio.to_thread(_write_private, list_file, "\n".join(toc.lines) + "\n")
            argv = [
                tool,
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--no-tablespaces",
                "--no-subscriptions",
                "--no-security-labels",
                "--no-password",
                f"--use-list={list_file}",
                f"--dbname={sandbox.name}",
                str(dump),
            ]
            result = await self._run(ctx, argv, sandbox.conn.libpq_env(), classification="db_restore")
            self._ensure_ok(result, "pg_restore", code)
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)
        return warnings

    async def restore_test(
        self, artifact_id: str, ctx: DbCallContext, *, pg_server: PgSandboxServer | None, label: str = "restore-test"
    ) -> dict[str, Any]:
        row = await self.store.get_backup(artifact_id)
        meta: dict[str, Any] = dict(row.metadata_["dbtools"])
        path = self.store.checked_path(row)
        digest, size = await asyncio.to_thread(sha256_file, path)
        if digest != row.sha256 or size != row.size_bytes:
            raise DbToolError("backup artifact checksum mismatch – the file was changed or truncated", code=DB_ARTIFACT_INVALID)
        expected: dict[str, int | None] = {str(k): (None if v is None else int(v)) for k, v in (meta.get("tables") or {}).items()}
        base: dict[str, Any] = {"artifact_id": str(row.id), "target": meta.get("target"), "format": meta.get("format"), "checksum_ok": True}
        if meta.get("format") == FORMAT_PG:
            if pg_server is None:
                raise DbToolError("no sandbox server configured for restore tests (databases.yaml: sandbox)", code=DB_SANDBOX_UNAVAILABLE)
            sandbox = await pg_server.create(label=label)
            try:
                warnings = await self.pg_restore_into(path, sandbox, pg_server, ctx)
                actual = await self._pg_counts(sandbox.conn)
            finally:
                await asyncio.shield(pg_server.drop(sandbox))
            comparison = compare_snapshots(expected, actual)
            return {**base, **comparison, "sandbox": sandbox.name, "warnings": warnings, "consistent_source": bool(meta.get("consistent"))}
        if meta.get("format") == FORMAT_SQLITE:
            sbx = await asyncio.to_thread(self._sqlite_create, path)
            try:
                integrity, actual = await asyncio.to_thread(_sqlite_verify, sbx.conn, self.limits.max_count_tables)
            finally:
                await asyncio.to_thread(self.sqlite_sandboxes.drop, sbx)
            comparison = compare_snapshots(expected, actual)
            comparison["ok"] = comparison["ok"] and integrity == "ok"
            return {**base, **comparison, "integrity_check": integrity[:500], "sandbox": sbx.name, "warnings": []}
        raise DbToolError(f"unknown backup format {str(meta.get('format'))[:40]}", code=DB_ARTIFACT_INVALID)

    def _sqlite_create(self, source: Path) -> SandboxDatabase:
        return self.sqlite_sandboxes.create(source=source)

    async def _pg_counts(self, conn: ConnectionInfo) -> dict[str, int | None]:
        aconn = await self._connect(conn, options=f"-c statement_timeout={int(self.limits.count_timeout_seconds * 1000)}")
        try:
            await aconn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            tables = await pg_list_tables(aconn)
            counts, _ = await pg_count_tables(aconn, tables, max_tables=self.limits.max_count_tables)
        finally:
            with contextlib.suppress(Exception):
                await aconn.rollback()
            await aconn.close()
        return counts

    # ------------------------------------------------------------------------------ seeding
    async def seed_postgres_schema(
        self, source: ResolvedTarget, sandbox: SandboxDatabase, server: PgSandboxServer, ctx: DbCallContext
    ) -> dict[str, Any]:
        """Schema of ``source`` (``pg_dump --schema-only``) plus the data of migration bookkeeping tables → sandbox."""
        aconn = await self._connect(source.conn, options="-c default_transaction_read_only=on")
        try:
            cur = await aconn.execute("SELECT current_setting('server_version_num')::int")
            row = await cur.fetchone()
            major = int(row[0]) // 10000 if row else None
            cur = await aconn.execute(
                "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'r' AND c.relname = ANY(%s) AND n.nspname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2",
                (sorted(BOOKKEEPING_TABLES),),
            )
            bookkeeping = [(str(s), str(t)) for s, t in await cur.fetchall()]
        finally:
            with contextlib.suppress(Exception):
                await aconn.rollback()
            await aconn.close()
        tool = find_pg_tool("pg_dump", min_major=major, bin_dir=self.pg_bin_dir)
        workdir = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="hermclaw-dbseed-"))
        env = source.conn.libpq_env()
        try:
            schema_dump = workdir / "schema.dump"
            result = await self._run(
                ctx,
                [tool, "--format=custom", "--schema-only", "--no-password", "--lock-wait-timeout=60000", f"--file={schema_dump}"],
                env,
                classification="db_seed_schema",
            )
            self._ensure_ok(result, "pg_dump --schema-only", DB_SANDBOX_FAILED)
            warnings = await self.pg_restore_into(schema_dump, sandbox, server, ctx, code=DB_SANDBOX_FAILED)
            if bookkeeping:
                data_dump = workdir / "bookkeeping.dump"
                argv = [tool, "--format=custom", "--data-only", "--no-password", "--lock-wait-timeout=60000", f"--file={data_dump}"]
                argv += [f"--table={_pattern(s)}.{_pattern(t)}" for s, t in bookkeeping]
                result = await self._run(ctx, argv, env, classification="db_seed_schema")
                self._ensure_ok(result, "pg_dump --data-only (migration bookkeeping)", DB_SANDBOX_FAILED)
                warnings += await self.pg_restore_into(data_dump, sandbox, server, ctx, code=DB_SANDBOX_FAILED)
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)
        return {"seeded_from": source.name, "bookkeeping_tables": [f"{s}.{t}" for s, t in bookkeeping], "warnings": warnings}


def _pattern(identifier: str) -> str:
    """pg_dump ``--table`` pattern matching exactly ``identifier`` (double quotes make ``*``/``?``/``.`` literal)."""
    return '"' + identifier.replace('"', '""') + '"'


def _make_private_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _sqlite_backup(source: Path, dest: Path, max_tables: int) -> TableSnapshot:
    """Online backup inside one read transaction: the counts describe exactly the copied pages."""
    src = sqlite3.connect(f"file:{quote(str(source))}?mode=ro", uri=True, isolation_level=None, timeout=30)
    try:
        src.execute("BEGIN")
        counts, skipped = sqlite_counts(src, max_tables=max_tables)
        version = str(src.execute("SELECT sqlite_version()").fetchone()[0])
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # a WAL-mode copy could not be opened read-only later
        finally:
            dst.close()
        src.execute("COMMIT")
    finally:
        src.close()
    return TableSnapshot(counts, True, version, skipped)


def _sqlite_verify(conn: ConnectionInfo, max_tables: int) -> tuple[str, dict[str, int | None]]:
    assert conn.path is not None
    db = sqlite3.connect(f"file:{quote(str(conn.path))}?mode=ro", uri=True)
    try:
        rows = db.execute("PRAGMA integrity_check").fetchall()
        integrity = "; ".join(str(r[0]) for r in rows)
        counts, _ = sqlite_counts(db, max_tables=max_tables)
    finally:
        db.close()
    return integrity, counts


def _msg(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    lines = str(primary or exc).strip().splitlines()
    return redact_dsn(lines[0] if lines else type(exc).__name__)[:300]


__all__ = [
    "BACKUP_KIND",
    "FORMAT_PG",
    "FORMAT_SQLITE",
    "ArtifactStore",
    "BackupService",
    "RestoreList",
    "TableSnapshot",
    "compare_snapshots",
    "filter_toc",
    "find_pg_tool",
    "installed_pg_majors",
    "pg_count_tables",
    "pg_list_tables",
    "sha256_file",
    "sqlite_counts",
]
