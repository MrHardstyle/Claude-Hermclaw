"""Hermclaw's own backup (Bauplan §36, P33 33.1-33.4; research 20261008-022). Target: Terra/Unraid ``.60``.

``create_backup`` builds one timestamped backup set in a private staging directory and transfers it to the target:

1. **PostgreSQL** (33.1): a ``REPEATABLE READ READ ONLY`` transaction exports its snapshot (``pg_export_snapshot``);
   in that snapshot the exact row count of every table, the alembic revision and the record exports are taken, and
   ``pg_dump -Fc --snapshot=…`` dumps exactly the same state. ``pg_dumpall --globals-only`` (roles, tablespaces;
   ``--no-role-passwords`` by default) is skipped gracefully when not permitted. Credentials reach the client tools
   only via ``PGPASSFILE`` (never argv).
2. **Config** (33.2): tarball of the config dir without plaintext secrets – ``secrets/``, private keys, ``*.key``/
   ``*.pem`` …, files referenced as ``file:`` secrets and text files containing a secret are excluded and listed in
   the manifest; encrypted credential stores (``credstore_dirs``) are included.
3. **Artifacts** (33.3): tarball of the artifact store plus ``records/<table>.jsonl`` (research runs/sources/claims,
   deployment manifests) exported from the dump snapshot.
4. **Manifest** + transfer (33.4): ``MANIFEST.json`` (sha256 + size per file, row-count snapshot, alembic revision,
   versions; optional HMAC signature) is written last; the set is copied to the target and verified there
   (local: read-back sha256; rsync: ``--checksum`` dry-run must show no difference), then retention is applied.

Events ``backup.*`` (best effort) and JSON log lines record every run. The staging copy is always removed.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import socket
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import hermclaw
from hermclaw.backup import events, runner
from hermclaw.backup.archive import ArchiveSource, ArchiveStats, build_tarball
from hermclaw.backup.config import BackupConfig, BackupTarget, LocalTarget, RsyncTarget, load_hosts
from hermclaw.backup.errors import (
    BACKUP_CONFIG_INVALID,
    BACKUP_DUMP_FAILED,
    BACKUP_FAILED,
    BACKUP_NOT_FOUND,
    BACKUP_VERIFY_FAILED,
    BackupError,
)
from hermclaw.backup.lock import backup_lock
from hermclaw.backup.manifest import (
    ARTIFACTS_TAR,
    CONFIG_TAR,
    DB_DUMP,
    GLOBALS,
    ArchiveInfo,
    DatabaseInfo,
    FileEntry,
    FileKind,
    GlobalsInfo,
    Manifest,
    RecordExport,
    is_set_id,
    new_set_id,
    sha256_file,
    write_manifest,
)
from hermclaw.backup.pg import (
    PgConn,
    alembic_revision,
    count_tables,
    filter_toc,
    find_pg_tool,
    list_extensions,
    list_tables,
    pg_tool_env,
    resolve_conn,
    server_major,
)
from hermclaw.backup.retention import RetentionPlan, plan_retention
from hermclaw.backup.transfer import SetListing, TargetStore, TransferResult, open_store
from hermclaw.backup.verify import verify_set_dir
from hermclaw.contracts.common import Severity
from hermclaw.core.config import HostsConfig
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.core.settings import Settings, get_settings
from hermclaw.security.secrets import SecretStore

log = get_logger(__name__)
RECORDS_DIR = "records"
EXPORT_BATCH = 500
VERSION_TIMEOUT_SECONDS = 30
_FILE_REF_RE = re.compile(r"""\bfile:(/[^\s"'#,\]}]+)""")
_REF_SCAN_SUFFIXES = (".yaml", ".yml", ".json", ".toml", ".env", ".conf", ".ini")
_REF_SCAN_MAX_BYTES = 2 * 1024 * 1024
SessionMaker = async_sessionmaker[AsyncSession]


class TransferInfo(BaseModel):
    method: str
    location: str
    files: int
    bytes: int
    verified: bool
    duration_ms: int


class BackupResult(BaseModel):
    set_id: str
    target: str
    location: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    total_bytes: int
    files: list[FileEntry]
    tables: int
    rows: int
    alembic_revision: str | None
    snapshot_consistent: bool
    globals_included: bool
    config_excluded: list[str] = Field(default_factory=list)
    signed: bool = False
    transfer: TransferInfo
    pruned: list[str] = Field(default_factory=list)
    prune_error: str | None = None


class PruneResult(BaseModel):
    target: str
    dry_run: bool
    keep: list[str]
    delete: list[str]
    deleted: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    reasons: dict[str, list[str]] = Field(default_factory=dict)


@dataclass
class _DbDump:
    info: DatabaseInfo
    records: list[RecordExport]


# ================================================================================================ helpers
def signing_key(config: BackupConfig, settings: Settings) -> bytes | None:
    if not config.manifest_signing_key_ref:
        return None
    value = SecretStore(env=settings.env).resolve(config.manifest_signing_key_ref)
    return value.encode("utf-8") if value else None


def build_store(
    config: BackupConfig, settings: Settings, *, hosts: HostsConfig | None = None, store: TargetStore | None = None
) -> TargetStore:
    if store is not None:
        return store
    if hosts is None and isinstance(config.target, RsyncTarget) and config.target.host_id:
        hosts = load_hosts(settings)
    return open_store(config.target, hosts=hosts, env=settings.env)


async def tool_version(binary: str) -> str:
    try:
        result = await runner.run([binary, "--version"], env=runner.base_env(), limit_seconds=VERSION_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return "missing"
    lines = (result.stdout or result.stderr).strip().splitlines()
    return lines[0].strip()[:200] if lines else "unknown"


def referenced_secret_files(config_dir: Path) -> set[Path]:
    """Files referenced as ``file:/…`` secrets by the configuration (never copied into the config tarball)."""
    found: set[Path] = set()
    if not config_dir.is_dir():
        return found
    for dirpath, _dirs, filenames in os.walk(config_dir, followlinks=False):
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() not in _REF_SCAN_SUFFIXES or path.is_symlink() or not path.is_file():
                continue
            try:
                if path.stat().st_size > _REF_SCAN_MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            found.update(Path(match.group(1)) for match in _FILE_REF_RE.finditer(text))
    return found


def _archive_info(sources: Iterable[ArchiveSource], stats: ArchiveStats) -> ArchiveInfo:
    return ArchiveInfo(
        sources=[f"{s.arc_root}={s.path}" for s in sources],
        files=stats.files,
        dirs=stats.dirs,
        bytes=stats.bytes,
        root_files=dict(stats.per_root_files),
        excluded=list(stats.excluded),
        skipped=list(stats.skipped),
        missing=list(stats.missing),
    )


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _clean_stale_staging(staging_root: Path, keep: str | None = None) -> list[str]:
    """Leftovers of interrupted runs (only called while holding the lock)."""
    removed: list[str] = []
    for entry in staging_root.iterdir():
        if entry.is_dir() and not entry.is_symlink() and is_set_id(entry.name) and entry.name != keep:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


# ================================================================================================ database
async def _export_records(
    aconn: psycopg.AsyncConnection[Any], tables: list[tuple[str, str]], wanted: list[str], dest: Path
) -> list[RecordExport]:
    """``records/<table>.jsonl`` (``row_to_json`` per row) in the caller's snapshot; missing tables are skipped."""
    exports: list[RecordExport] = []
    by_name: dict[str, tuple[str, str]] = {}
    for schema, table in tables:
        if schema == "public" or table not in by_name:
            by_name[table] = (schema, table)
    for name in wanted:
        if name not in by_name:
            log.info("record export skipped: table missing", extra={"table": name})
            continue
        schema, table = by_name[name]
        target = dest / f"{name}.jsonl"
        fd = await asyncio.to_thread(os.open, target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        handle = os.fdopen(fd, "wb")
        rows = 0
        try:
            query = sql.SQL("SELECT row_to_json(t)::text FROM {}.{} AS t").format(sql.Identifier(schema), sql.Identifier(table))
            async with aconn.cursor(name=f"hermclaw_backup_{name}"[:63]) as cur:
                await cur.execute(query)
                while batch := await cur.fetchmany(EXPORT_BATCH):
                    data = b"".join(str(row[0]).encode("utf-8") + b"\n" for row in batch)
                    await asyncio.to_thread(handle.write, data)
                    rows += len(batch)
            await asyncio.to_thread(handle.flush)
        finally:
            await asyncio.to_thread(handle.close)
        exports.append(RecordExport(table=name, rows=rows, member=f"{RECORDS_DIR}/{name}.jsonl"))
    return exports


async def _begin_snapshot(conn: PgConn) -> tuple[psycopg.AsyncConnection[Any], str | None]:
    """Connection in a ``REPEATABLE READ READ ONLY`` transaction and its exported snapshot (None if not exportable)."""
    aconn = await conn.connect()
    try:
        await aconn.set_isolation_level(psycopg.IsolationLevel.REPEATABLE_READ)
        await aconn.set_read_only(True)
        try:
            cur = await aconn.execute("SELECT pg_export_snapshot()")
            row = await cur.fetchone()
            return aconn, str(row[0]) if row else None
        except psycopg.Error as exc:  # e.g. a hot standby without snapshot export rights: no shared snapshot
            log.warning("pg_export_snapshot failed; row counts are not snapshot-consistent", extra={"error": type(exc).__name__})
            await aconn.rollback()
            return aconn, None
    except BaseException:
        await aconn.close()
        raise


async def dump_database(conn: PgConn, work: Path, config: BackupConfig) -> _DbDump:
    """``db.dump`` + ``records/`` + row-count snapshot (33.1, 33.3 record exports)."""
    records_dir = _private_dir(work / RECORDS_DIR)
    aconn, snapshot = await _begin_snapshot(conn)
    try:
        major, version = await server_major(aconn)
        tables = await list_tables(aconn)
        counts = await count_tables(aconn, tables)
        revision = await alembic_revision(aconn)
        extensions = await list_extensions(aconn)
        cur = await aconn.execute("SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()")
        enc_row = await cur.fetchone()
        records = await _export_records(aconn, tables, config.record_exports, records_dir)
        pg_dump = find_pg_tool("pg_dump", bin_dir=config.pg_bin_dir, min_major=major)
        dump_version = await tool_version(pg_dump)
        argv = [
            pg_dump,
            "--format=custom",
            f"--compress={config.dump_compress_level}",
            f"--lock-wait-timeout={config.lock_timeout_seconds * 1000}",
            "--no-password",
            f"--file={work / DB_DUMP}",
        ]
        if snapshot:
            argv.append(f"--snapshot={snapshot}")
        with pg_tool_env(conn) as pg_env:
            result = await runner.run(argv, env=runner.base_env(pg_env), limit_seconds=config.dump_timeout_seconds)
        if not result.ok:
            raise BackupError(f"pg_dump failed (exit {result.returncode}): {result.error_text()}", code=BACKUP_DUMP_FAILED)
    finally:
        await aconn.rollback()
        await aconn.close()
    pg_restore = find_pg_tool("pg_restore", bin_dir=config.pg_bin_dir, min_major=major)
    listing = await runner.run([pg_restore, "--list", "--", str(work / DB_DUMP)], env=runner.base_env(), limit_seconds=600)
    if not listing.ok:
        raise BackupError(f"the new dump is not readable by pg_restore: {listing.error_text()}", code=BACKUP_DUMP_FAILED)
    toc_entries = sum(1 for ln in listing.stdout.splitlines() if ln.strip() and not ln.lstrip().startswith(";"))
    info = DatabaseInfo(
        dbname=conn.dbname,
        connection=conn.display(),
        server_version=version,
        server_major=major,
        pg_dump_version=dump_version,
        alembic_revision=revision,
        encoding=str(enc_row[0]) if enc_row else None,
        extensions=sorted(set(extensions) | set(filter_toc(listing.stdout).extensions)),
        tables=counts,
        snapshot_consistent=snapshot is not None,
        toc_entries=toc_entries,
    )
    return _DbDump(info=info, records=records)


async def dump_globals(conn: PgConn, work: Path, config: BackupConfig, server_major_version: int) -> GlobalsInfo:
    """``pg_dumpall --globals-only`` – skipped (not failed) when the role may not read the globals."""
    if not config.include_globals:
        return GlobalsInfo(included=False, reason="disabled (include_globals: false)")
    try:
        pg_dumpall = find_pg_tool("pg_dumpall", bin_dir=config.pg_bin_dir, min_major=server_major_version)
    except BackupError as exc:
        return GlobalsInfo(included=False, reason=exc.message)
    dest = work / GLOBALS
    argv = [pg_dumpall, "--globals-only", "--no-password", f"--database={conn.dbname}", f"--file={dest}"]
    if not config.globals_role_passwords:
        argv.append("--no-role-passwords")
    with pg_tool_env(conn) as pg_env:
        result = await runner.run(argv, env=runner.base_env(pg_env), limit_seconds=config.dump_timeout_seconds)
    if not result.ok or not dest.is_file():
        dest.unlink(missing_ok=True)
        reason = f"pg_dumpall --globals-only not permitted/failed: {result.error_text(300)}"
        log.warning("globals dump skipped", extra={"reason": reason})
        return GlobalsInfo(included=False, reason=reason)
    dest.chmod(0o600)
    return GlobalsInfo(included=True, role_passwords=config.globals_role_passwords)


# ================================================================================================ tarballs
def _credstore_sources(config: BackupConfig) -> list[ArchiveSource]:
    sources: list[ArchiveSource] = []
    used: set[str] = set()
    for index, path in enumerate(config.credstore_dirs):
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", path.name) or f"store{index}"
        if name in used:
            name = f"{name}-{index}"
        used.add(name)
        # encrypted blobs only: plaintext keys/secrets that ended up there are still refused
        sources.append(ArchiveSource(f"credstore/{name}", path, sniff_private_keys=True, scan_secrets=True, required=True))
    return sources


def build_config_tarball(work: Path, config: BackupConfig, settings: Settings, skip: list[Path]) -> ArchiveInfo:
    config_dir = config.effective_config_dir(settings)
    sources = [
        ArchiveSource("config", config_dir, excludes=tuple(config.config_excludes()), scan_secrets=True, required=True),
        *_credstore_sources(config),
    ]
    stats = build_tarball(
        work / CONFIG_TAR,
        sources,
        compresslevel=config.tar_compress_level,
        skip_paths=skip,
        exclude_paths=referenced_secret_files(config_dir),
    )
    return _archive_info(sources, stats)


def build_artifacts_tarball(work: Path, config: BackupConfig, settings: Settings, skip: list[Path]) -> ArchiveInfo:
    sources = [
        ArchiveSource(
            "artifacts", config.effective_artifacts_dir(settings), excludes=tuple(config.artifacts_exclude), sniff_private_keys=False
        ),
        ArchiveSource(RECORDS_DIR, work / RECORDS_DIR, sniff_private_keys=False, required=True),
    ]
    stats = build_tarball(work / ARTIFACTS_TAR, sources, compresslevel=config.tar_compress_level, skip_paths=skip)
    shutil.rmtree(work / RECORDS_DIR, ignore_errors=True)
    return _archive_info(sources, stats)


def _file_entries(work: Path, names: list[tuple[str, FileKind]]) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for name, kind in names:
        if (work / name).is_file():
            digest, size = sha256_file(work / name)
            entries.append(FileEntry(name=name, kind=kind, sha256=digest, size=size))
    return entries


# ================================================================================================ API
def check_staging_separate(config: BackupConfig, staging_root: Path) -> None:
    """The staging dir is cleaned of set-like directories – it must never be (inside) a local target or vice versa."""
    local: Path | None = None
    if isinstance(config.target, LocalTarget):
        local = config.target.path
    elif isinstance(config.target, RsyncTarget) and not (config.target.host or config.target.host_id):
        local = Path(config.target.path)
    if local is None:
        return
    staging, target = staging_root.resolve(), local.resolve()
    if staging == target or staging.is_relative_to(target) or target.is_relative_to(staging):
        raise BackupError(
            f"staging_dir {staging_root} and the backup target {local} must be separate directories", code=BACKUP_CONFIG_INVALID
        )


def _target_skip_paths(config: BackupConfig, staging_root: Path) -> list[Path]:
    skip = [staging_root]
    if isinstance(config.target, LocalTarget):
        skip.append(config.target.path)
    elif isinstance(config.target, RsyncTarget) and not (config.target.host or config.target.host_id):
        skip.append(Path(config.target.path))
    return skip


def effective_config(target: BackupTarget | None, config: BackupConfig | None) -> BackupConfig:
    if config is None:
        if target is None:
            raise BackupError("create_backup needs a target or a BackupConfig", code=BACKUP_FAILED)
        return BackupConfig(target=target)
    return config if target is None else config.model_copy(update={"target": target})


async def create_backup(
    target: BackupTarget | None = None,
    *,
    config: BackupConfig | None = None,
    settings: Settings | None = None,
    sessionmaker: SessionMaker | None = None,
    hosts: HostsConfig | None = None,
    store: TargetStore | None = None,
    prune: bool | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Create a backup set, transfer it to ``target`` (or ``config.target``) and apply retention."""
    cfg = effective_config(target, config)
    settings = settings or get_settings()
    check_staging_separate(cfg, cfg.effective_staging_dir(settings))
    staging_root = await asyncio.to_thread(_private_dir, cfg.effective_staging_dir(settings))
    async with backup_lock(cfg.lock_path(settings), wait_seconds=cfg.lock_wait_seconds, what="create"):
        target_store = build_store(cfg, settings, hosts=hosts, store=store)
        return await _create_locked(
            cfg, settings, staging_root=staging_root, sessionmaker=sessionmaker, target_store=target_store, prune=prune, now=now
        )


async def _create_locked(
    cfg: BackupConfig,
    settings: Settings,
    *,
    staging_root: Path,
    sessionmaker: SessionMaker | None,
    target_store: TargetStore,
    prune: bool | None,
    now: datetime | None,
) -> BackupResult:
    started_at = datetime.now(UTC)
    started = time.monotonic()
    set_id = new_set_id(now)
    stale = await asyncio.to_thread(_clean_stale_staging, staging_root)
    if stale:
        log.warning("removed stale backup staging directories", extra={"sets": ",".join(stale)})
    work = await asyncio.to_thread(_private_dir, staging_root / set_id)
    target_desc = target_store.describe()
    await events.emit(
        sessionmaker, events.BACKUP_STARTED, set_id=set_id, payload={"target": target_desc, "instance_id": settings.instance_id}
    )
    try:
        conn = resolve_conn(cfg.effective_database_url(settings), cfg.database_password_ref, store=SecretStore(env=settings.env))
        dumped = await dump_database(conn, work, cfg)
        globals_info = await dump_globals(conn, work, cfg, dumped.info.server_major)
        skip = _target_skip_paths(cfg, staging_root)
        config_info = await asyncio.to_thread(build_config_tarball, work, cfg, settings, skip)
        artifacts_info = await asyncio.to_thread(build_artifacts_tarball, work, cfg, settings, skip)
        names: list[tuple[str, FileKind]] = [
            (DB_DUMP, "db_dump"),
            (GLOBALS, "globals"),
            (CONFIG_TAR, "config"),
            (ARTIFACTS_TAR, "artifacts"),
        ]
        files = await asyncio.to_thread(_file_entries, work, names)
        versions = {
            "pg_dump": dumped.info.pg_dump_version,
            "python": platform.python_version(),
            "hermclaw": hermclaw.__version__,
        }
        if isinstance(cfg.target, RsyncTarget):
            versions["rsync"] = await tool_version(cfg.target.rsync_binary)
        manifest = Manifest(
            set_id=set_id,
            created_at=started_at,
            finished_at=datetime.now(UTC),
            hermclaw_version=hermclaw.__version__,
            instance_id=settings.instance_id,
            hostname=socket.gethostname(),
            python_version=platform.python_version(),
            target=target_desc,
            database=dumped.info,
            globals=globals_info,
            config=config_info,
            artifacts=artifacts_info,
            records=dumped.records,
            files=files,
            tool_versions=versions,
        )
        key = signing_key(cfg, settings)
        await asyncio.to_thread(write_manifest, work, manifest, signing_key=key)
        local_check, _ = await verify_set_dir(work, signing_key=key, deep=False)
        if not local_check.ok:
            raise BackupError(f"staged backup set failed verification: {'; '.join(local_check.problems[:3])}", code=BACKUP_VERIFY_FAILED)
        transfer: TransferResult = await target_store.upload(work, manifest)
    except BaseException as exc:
        if isinstance(exc, Exception):
            code = exc.code if isinstance(exc, BackupError) else BACKUP_FAILED
            message = exc.message if isinstance(exc, BackupError) else DEFAULT_REDACTOR.text(f"{type(exc).__name__}: {exc}")
            await events.emit(
                sessionmaker,
                events.BACKUP_FAILED,
                set_id=set_id,
                payload={"target": target_desc, "code": code, "error": message[:500]},
                severity=Severity.error,
            )
        raise
    finally:
        await asyncio.to_thread(shutil.rmtree, work, True)
    rows = sum(dumped.info.tables.values())
    duration = int((time.monotonic() - started) * 1000)
    await events.emit(
        sessionmaker,
        events.BACKUP_COMPLETED,
        set_id=set_id,
        payload={
            "target": target_desc,
            "location": transfer.location,
            "bytes": manifest.total_bytes(),
            "files": len(files),
            "tables": len(dumped.info.tables),
            "rows": rows,
            "alembic_revision": dumped.info.alembic_revision,
            "snapshot_consistent": dumped.info.snapshot_consistent,
            "globals_included": globals_info.included,
            "config_excluded": len(config_info.excluded),
            "skipped": len(config_info.skipped) + len(artifacts_info.skipped),
            "verified": transfer.verified,
            "duration_ms": duration,
        },
    )
    result = BackupResult(
        set_id=set_id,
        target=target_desc,
        location=transfer.location,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        duration_ms=duration,
        total_bytes=manifest.total_bytes(),
        files=files,
        tables=len(dumped.info.tables),
        rows=rows,
        alembic_revision=dumped.info.alembic_revision,
        snapshot_consistent=dumped.info.snapshot_consistent,
        globals_included=globals_info.included,
        config_excluded=config_info.excluded,
        signed=key is not None,
        transfer=TransferInfo(**asdict(transfer)),
    )
    if cfg.retention.prune_after_backup if prune is None else prune:
        try:
            pruned = await _prune_locked(cfg, target_store, sessionmaker, dry_run=False, protect=[set_id])
            result.pruned = pruned.deleted
            if pruned.errors:
                result.prune_error = "; ".join(pruned.errors[:3])
        except BackupError as exc:  # the new set is safe on the target – a failed prune does not fail the backup
            result.prune_error = exc.message
            log.warning("retention after backup failed", extra={"error": exc.message})
    return result


async def _prune_locked(
    cfg: BackupConfig,
    target_store: TargetStore,
    sessionmaker: SessionMaker | None,
    *,
    dry_run: bool,
    protect: Iterable[str] = (),
    now: datetime | None = None,
) -> PruneResult:
    listings = await target_store.list_sets()
    plan: RetentionPlan = plan_retention(listings, cfg.retention, now=now, protect=protect)
    result = PruneResult(
        target=target_store.describe(),
        dry_run=dry_run,
        keep=[s.set_id for s in plan.keep],
        delete=[_listing_key(s) for s in plan.delete],
        reasons=plan.reasons,
    )
    if dry_run:
        return result
    for listing in plan.delete:
        try:
            await target_store.delete(listing)
            result.deleted.append(_listing_key(listing))
        except (BackupError, OSError) as exc:
            result.errors.append(f"{_listing_key(listing)}: {exc}")
    if result.deleted or result.errors:
        await events.emit(
            sessionmaker,
            events.BACKUP_PRUNED,
            set_id=None,
            payload={"target": result.target, "deleted": result.deleted, "kept": result.keep, "errors": result.errors[:10]},
            severity=Severity.warning if result.errors else Severity.info,
        )
    return result


def _listing_key(listing: SetListing) -> str:
    return listing.set_id + (".partial" if listing.partial_dir else "")


async def prune_backups(
    config: BackupConfig,
    *,
    settings: Settings | None = None,
    sessionmaker: SessionMaker | None = None,
    hosts: HostsConfig | None = None,
    store: TargetStore | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> PruneResult:
    """Apply the retention policy to the target (keep N daily / weekly / monthly)."""
    settings = settings or get_settings()
    target_store = build_store(config, settings, hosts=hosts, store=store)
    async with backup_lock(config.lock_path(settings), wait_seconds=config.lock_wait_seconds, what="prune"):
        return await _prune_locked(config, target_store, sessionmaker, dry_run=dry_run, now=now)


async def list_backups(
    config: BackupConfig, *, settings: Settings | None = None, hosts: HostsConfig | None = None, store: TargetStore | None = None
) -> list[SetListing]:
    """Sets on the target, newest first."""
    settings = settings or get_settings()
    listings = await build_store(config, settings, hosts=hosts, store=store).list_sets()
    return sorted(listings, key=lambda s: (s.created_at, s.set_id), reverse=True)


async def latest_complete(
    config: BackupConfig, *, settings: Settings | None = None, hosts: HostsConfig | None = None, store: TargetStore | None = None
) -> str:
    for listing in await list_backups(config, settings=settings, hosts=hosts, store=store):
        if listing.complete and not listing.partial_dir:
            return listing.set_id
    raise BackupError("no complete backup set on the target", code=BACKUP_NOT_FOUND)
