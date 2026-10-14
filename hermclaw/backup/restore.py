"""Restore test and disaster-recovery restore (P33 33.5 restore environment, 33.6 restore verification).

``restore_test`` proves that a backup set is usable – a catalog listing is not enough (research 20261008-022):

1. the set is materialised (local share in place; rsync targets are fetched into a private work dir) and verified:
   manifest (+ HMAC signature when configured), sha256/size of every file, full read of both tarballs (gzip CRC,
   relative ``..``-free members, file counts, ``records/*.jsonl`` line counts), ``pg_restore --list``;
2. the config tarball is extracted into a temporary directory (``data`` extraction filter – no absolute paths,
   links or devices) and its file count compared with the manifest;
3. **clean temporary environment**: a fresh database ``<prefix>_<8 hex>`` is created (``TEMPLATE template0`` with
   the source encoding, or a configured template that already has the extensions), the dump's extensions are
   created up front, ``pg_restore --no-owner --no-privileges --exit-on-error --single-transaction`` restores it;
4. verification: every table of the manifest exists, every row count equals the snapshot in the manifest, the
   alembic revision and the extensions are equal;
5. ``RESTORE_REPORT.json`` is written next to the backup (into the set directory on the target) and returned, a
   ``backup.restore_test.*`` event is emitted. The temporary database is **always** dropped (``WITH (FORCE)``),
   leftovers of crashed runs are dropped on the next run.

``restore_full`` restores a set into a target database for disaster recovery. It refuses a set that fails
verification and refuses a non-empty target database unless ``force=True`` (then ``--clean --if-exists``); files
are extracted into an empty ``files_dest`` directory for the operator to move into place
(``docs/operations/backup-restore.md``).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field

from hermclaw.backup import events, runner
from hermclaw.backup.archive import extract_tarball
from hermclaw.backup.backup import SessionMaker, build_store, latest_complete, signing_key, tool_version
from hermclaw.backup.config import BackupConfig, BackupTarget, LocalTarget
from hermclaw.backup.errors import (
    BACKUP_CONFIG_INVALID,
    BACKUP_LOCKED,
    BACKUP_VERIFY_FAILED,
    RESTORE_FAILED,
    RESTORE_TARGET_NOT_EMPTY,
    BackupError,
)
from hermclaw.backup.lock import backup_lock
from hermclaw.backup.manifest import ARTIFACTS_TAR, CONFIG_TAR, DB_DUMP, RESTORE_REPORT, Manifest, check_set_id, write_json
from hermclaw.backup.pg import (
    PgConn,
    alembic_revision,
    count_tables,
    database_is_empty,
    filter_toc,
    find_pg_tool,
    list_extensions,
    list_tables,
    pg_tool_env,
    resolve_conn,
    server_major,
    valid_dbname,
)
from hermclaw.backup.transfer import TargetStore
from hermclaw.backup.verify import VerifyReport, verify_set_dir
from hermclaw.contracts.common import Severity
from hermclaw.core.config import HostsConfig
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.core.settings import Settings, get_settings
from hermclaw.security.secrets import SecretStore

log = get_logger(__name__)
REPORT_FORMAT: Final = "hermclaw-restore-report/1"
LIST_TIMEOUT_SECONDS = 600
MAINTENANCE_DB = "postgres"


class RestoreCheck(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class TableCount(BaseModel):
    table: str
    expected: int | None
    actual: int | None
    ok: bool


class RestoreReport(BaseModel):
    format: Literal["hermclaw-restore-report/1"] = REPORT_FORMAT
    kind: Literal["restore_test", "restore_full"]
    set_id: str
    ok: bool = False
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int = 0
    location: str
    signature: str | None = None
    database: str | None = None  # temporary database (restore test) or credential-free target (full restore)
    database_dropped: bool | None = None
    server_version: str | None = None
    pg_restore_version: str | None = None
    alembic_expected: str | None = None
    alembic_actual: str | None = None
    tables_expected: int = 0
    tables_restored: int = 0
    rows_expected: int = 0
    rows_restored: int = 0
    checks: list[RestoreCheck] = Field(default_factory=list)
    tables: list[TableCount] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    report_path: str | None = None

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(RestoreCheck(name=name, ok=ok, detail=DEFAULT_REDACTOR.text(detail)[:1000]))

    def check(self, name: str) -> RestoreCheck | None:
        return next((c for c in self.checks if c.name == name), None)

    def failed_checks(self) -> list[str]:
        return [c.name for c in self.checks if not c.ok]

    def finish(self, started: float) -> RestoreReport:
        self.ok = bool(self.checks) and all(c.ok for c in self.checks) and self.error is None
        self.finished_at = datetime.now(UTC)
        self.duration_ms = int((time.monotonic() - started) * 1000)
        return self


# ================================================================================================ helpers
def _psql_error(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return DEFAULT_REDACTOR.text(lines[0][:300]) if lines else type(exc).__name__


def admin_conn(config: BackupConfig, settings: Settings) -> PgConn:
    """Connection that may CREATE/DROP the temporary database (maintenance DB ``postgres``)."""
    rt = config.restore_test
    store = SecretStore(env=settings.env)
    if rt.admin_url:
        return resolve_conn(rt.admin_url, rt.admin_password_ref, store=store)
    base = resolve_conn(config.effective_database_url(settings), rt.admin_password_ref or config.database_password_ref, store=store)
    return base.with_db(MAINTENANCE_DB)


def _temp_db_re(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}_[0-9a-f]{{8}}$")


async def _drop_database(conn: PgConn, name: str) -> bool:
    try:
        aconn = await conn.connect(autocommit=True)
    except BackupError as exc:
        log.error("cannot connect to drop the temporary database", extra={"database": name, "error": exc.message})
        return False
    try:
        await aconn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        cur = await aconn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        return await cur.fetchone() is None
    except psycopg.Error as exc:
        log.error("dropping the temporary database failed", extra={"database": name, "error": _psql_error(exc)})
        return False
    finally:
        await aconn.close()


async def drop_stale_temp_databases(conn: PgConn, prefix: str) -> list[str]:
    """Drop ``<prefix>_<8 hex>`` databases left behind by crashed restore tests (caller holds the backup lock)."""
    pattern = _temp_db_re(prefix)
    aconn = await conn.connect(autocommit=True)
    try:
        cur = await aconn.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (prefix.replace("_", r"\_") + r"\_%",))
        names = [str(r[0]) for r in await cur.fetchall() if pattern.match(str(r[0]))]
    finally:
        await aconn.close()
    dropped = [name for name in names if await _drop_database(conn, name)]
    if dropped:
        log.warning("dropped stale restore-test databases", extra={"databases": ",".join(dropped)})
    return dropped


@contextlib.contextmanager
def _private_tmp(base: Path | None, prefix: str) -> Any:
    if base is not None:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ================================================================================================ pg_restore
async def pg_restore_into(
    dump: Path,
    conn: PgConn,
    manifest: Manifest,
    report: RestoreReport,
    *,
    pg_bin_dir: Path | None,
    jobs: int,
    timeout_seconds: int,
    clean: bool,
    work_dir: Path | None,
) -> bool:
    """Restore ``dump`` into ``conn.dbname`` (extensions created first); records checks; True on success."""
    try:
        aconn = await conn.connect(autocommit=True)
    except BackupError as exc:
        report.add("database_connect", False, exc.message)
        return False
    try:
        major, version = await server_major(aconn)
        report.server_version = version
        pg_restore = find_pg_tool("pg_restore", bin_dir=pg_bin_dir, min_major=max(major, manifest.database.server_major))
        report.pg_restore_version = await tool_version(pg_restore)
        listing = await runner.run([pg_restore, "--list", "--", str(dump)], env=runner.base_env(), limit_seconds=LIST_TIMEOUT_SECONDS)
        if not listing.ok:
            report.add("pg_restore", False, f"pg_restore --list failed: {listing.error_text()}")
            return False
        toc = filter_toc(listing.stdout)
        extensions = sorted(set(toc.extensions) | set(manifest.database.extensions))
        for ext in extensions:
            try:
                await aconn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(ext)))
            except psycopg.Error as exc:
                report.add(
                    "extensions",
                    False,
                    f"CREATE EXTENSION {ext} failed: {_psql_error(exc)} – create it as superuser first "
                    "(or configure restore_test.template with the extension installed)",
                )
                return False
        report.add("extensions_created", True, ", ".join(extensions) or "none")
    except BackupError as exc:
        report.add("pg_restore", False, exc.message)
        return False
    finally:
        await aconn.close()
    if toc.skipped:
        report.warnings.append(f"TOC entries handled by the restore itself: {'; '.join(toc.skipped[:10])}")
    with _private_tmp(work_dir, "toc-") as tmp:
        list_file = tmp / "restore.list"
        await asyncio.to_thread(list_file.write_text, toc.listing, "utf-8")
        argv = [pg_restore, "--no-owner", "--no-privileges", "--exit-on-error", "--no-password", f"--use-list={list_file}"]
        argv.append(f"--jobs={jobs}" if jobs > 1 else "--single-transaction")
        if clean:
            argv += ["--clean", "--if-exists"]
        argv += [f"--dbname={valid_dbname(conn.dbname)}", "--", str(dump)]
        with pg_tool_env(conn) as pg_env:
            result = await runner.run(argv, env=runner.base_env(pg_env), limit_seconds=timeout_seconds)
    entries = sum(1 for ln in toc.listing.splitlines() if ln.strip() and not ln.lstrip().startswith(";"))
    report.add("pg_restore", result.ok, f"{entries} catalog entries restored" if result.ok else result.error_text())
    return result.ok


async def compare_with_manifest(conn: PgConn, manifest: Manifest, report: RestoreReport, *, extra_tables_fail: bool) -> None:
    """33.6: tables present, exact row counts, alembic revision, extensions."""
    aconn = await conn.connect()
    try:
        await aconn.set_read_only(True)
        tables = await list_tables(aconn)
        counts = await count_tables(aconn, tables)
        revision = await alembic_revision(aconn)
        extensions = await list_extensions(aconn)
    finally:
        await aconn.rollback()
        await aconn.close()
    expected = manifest.database.tables
    report.tables_expected = len(expected)
    report.tables_restored = len(counts)
    report.rows_expected = sum(expected.values())
    report.rows_restored = sum(counts.get(t, 0) for t in expected)
    report.alembic_expected = manifest.database.alembic_revision
    report.alembic_actual = revision
    report.tables = [TableCount(table=t, expected=n, actual=counts.get(t), ok=counts.get(t) == n) for t, n in sorted(expected.items())]
    missing = sorted(set(expected) - set(counts))
    extra = sorted(set(counts) - set(expected))
    report.add("tables_present", not missing, f"{len(expected)} tables" if not missing else f"missing: {', '.join(missing[:20])}")
    if extra:
        detail = f"tables not in the backup: {', '.join(extra[:20])}"
        if extra_tables_fail:
            report.add("no_extra_tables", False, detail)
        else:
            report.warnings.append(detail)
    mismatched = [t for t in report.tables if not t.ok and t.actual is not None]
    detail = (
        f"{report.rows_restored} rows in {len(expected)} tables equal the manifest snapshot"
        if not mismatched
        else "; ".join(f"{t.table}: {t.actual} != {t.expected}" for t in mismatched[:20])
    )
    if mismatched and not manifest.database.snapshot_consistent:
        report.warnings.append(f"row counts differ but the backup was not snapshot-consistent: {detail}")
        report.add("row_counts", True, "not comparable (backup without shared snapshot)")
    else:
        report.add("row_counts", not mismatched, detail)
    report.add(
        "alembic_revision",
        revision == manifest.database.alembic_revision,
        f"{revision} (manifest {manifest.database.alembic_revision})",
    )
    missing_ext = sorted(set(manifest.database.extensions) - set(extensions))
    report.add("extensions_present", not missing_ext, ", ".join(extensions) if not missing_ext else f"missing: {', '.join(missing_ext)}")
    record_problems = [
        f"{r.table}: {r.rows} exported, {expected.get(f'public.{r.table}')} in snapshot"
        for r in manifest.records
        if f"public.{r.table}" in expected and expected[f"public.{r.table}"] != r.rows
    ]
    if manifest.database.snapshot_consistent:
        report.add("records_export", not record_problems, "; ".join(record_problems) or f"{len(manifest.records)} record exports match")


# ================================================================================================ set handling
def _apply_verify(report: RestoreReport, verify: VerifyReport, manifest: Manifest | None) -> None:
    report.signature = verify.signature
    report.add("manifest", manifest is not None, "; ".join(verify.problems[:2]) if manifest is None else verify.signature)
    if manifest is None:
        return
    for f in verify.files:
        report.add(f"checksum:{f.name}", f.ok, f.problem or f"sha256 {f.actual_sha256} ({f.actual_size} bytes)")
    for archive in verify.archives:
        report.add(f"archive:{archive.name}", archive.ok, "; ".join(archive.problems[:5]) or f"{archive.files} files, {archive.dirs} dirs")
    if verify.dump is not None:
        report.add("dump_catalog", verify.dump.ok, verify.dump.problem or f"{verify.dump.toc_entries} entries")
    known = {f"{f.name}: {f.problem}" for f in verify.files if not f.ok}
    known |= {f"{a.name}: {p}" for a in verify.archives for p in a.problems}
    if verify.dump is not None and not verify.dump.ok:
        known.add(f"{DB_DUMP}: {verify.dump.problem or 'empty catalog'}")
    other = [p for p in verify.problems if p not in known]
    if other:
        report.add("set_structure", False, "; ".join(other[:5]))
    if verify.unexpected_files:
        report.warnings.append(f"unexpected files in the set: {', '.join(verify.unexpected_files[:20])}")


def _check_config_extract(directory: Path, manifest: Manifest, report: RestoreReport, work_dir: Path | None) -> None:
    checksum = report.check(f"checksum:{CONFIG_TAR}")
    if checksum is None or not checksum.ok:
        report.add("config_extract", False, "skipped: config tarball failed verification")
        return
    with _private_tmp(work_dir, "config-") as tmp:
        try:
            count = extract_tarball(directory / CONFIG_TAR, tmp / "extract")
        except BackupError as exc:
            report.add("config_extract", False, exc.message)
            return
    ok = count == manifest.config.files
    report.add("config_extract", ok, f"{count} files extracted (manifest {manifest.config.files})")


async def _restore_test_dir(directory: Path, cfg: BackupConfig, settings: Settings, *, location: str) -> RestoreReport:
    started = time.monotonic()
    report = RestoreReport(kind="restore_test", set_id=directory.name, started_at=datetime.now(UTC), location=location)
    work_dir = cfg.effective_restore_work_dir(settings)
    try:
        verify, manifest = await verify_set_dir(
            directory, signing_key=signing_key(cfg, settings), deep=True, location=location, pg_bin_dir=cfg.pg_bin_dir
        )
        _apply_verify(report, verify, manifest)
        if manifest is None:
            return report.finish(started)
        report.set_id = manifest.set_id
        report.alembic_expected = manifest.database.alembic_revision
        report.tables_expected = len(manifest.database.tables)
        report.rows_expected = sum(manifest.database.tables.values())
        await asyncio.to_thread(_check_config_extract, directory, manifest, report, work_dir)
        dump_ok = not verify.failed(DB_DUMP) and verify.dump is not None and verify.dump.ok
        if not dump_ok:
            report.add("database_restore", False, "skipped: the dump failed verification")
            return report.finish(started)
        await _restore_into_temp_db(directory / DB_DUMP, manifest, cfg, report, settings=settings, work_dir=work_dir)
    except BackupError as exc:
        report.error = exc.message
        report.add("restore_test", False, exc.message)
    except (psycopg.Error, OSError) as exc:  # unexpected database/file errors still produce a (failed) report
        report.error = f"{type(exc).__name__}: {_psql_error(exc)}"
        report.add("restore_test", False, report.error)
    return report.finish(started)


async def _restore_into_temp_db(
    dump: Path, manifest: Manifest, cfg: BackupConfig, report: RestoreReport, *, settings: Settings, work_dir: Path | None
) -> None:
    rt = cfg.restore_test
    admin = admin_conn(cfg, settings)
    name = f"{rt.db_prefix}_{secrets.token_hex(4)}"
    with contextlib.suppress(BackupError, psycopg.Error):
        await drop_stale_temp_databases(admin, rt.db_prefix)
    template = rt.template or "template0"
    statement = sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(name), sql.Identifier(template))
    if rt.template is None and manifest.database.encoding:
        statement = sql.SQL("{} ENCODING {}").format(statement, sql.Literal(manifest.database.encoding))
    aconn = await admin.connect(autocommit=True)
    try:
        await aconn.execute(statement)
    except psycopg.Error as exc:
        report.add("restore_environment", False, f"cannot create the temporary database: {_psql_error(exc)}")
        return
    finally:
        await aconn.close()
    try:
        report.database = name
        report.add("restore_environment", True, f"temporary database {name} (template {template})")
        temp = admin.with_db(name)
        restored = await pg_restore_into(
            dump,
            temp,
            manifest,
            report,
            pg_bin_dir=cfg.pg_bin_dir,
            jobs=rt.jobs,
            timeout_seconds=rt.timeout_seconds,
            clean=False,
            work_dir=work_dir,
        )
        if restored:
            await compare_with_manifest(temp, manifest, report, extra_tables_fail=rt.template is None)
    finally:  # always – also on errors and cancellation
        report.database_dropped = await asyncio.shield(_drop_database(admin, name))
        report.add("restore_environment_dropped", report.database_dropped, name)


@contextlib.asynccontextmanager
async def _materialized(
    backup_set: str | Path, cfg: BackupConfig, settings: Settings, target_store: TargetStore | None
) -> AsyncIterator[tuple[Path, str, str]]:
    """(directory, set id, location) – a local directory as is, else the set from the target store."""
    if isinstance(backup_set, Path):
        if not await asyncio.to_thread(backup_set.is_dir):
            raise BackupError(f"backup set directory {backup_set} does not exist", code=BACKUP_CONFIG_INVALID)
        yield backup_set, backup_set.name, str(backup_set)
        return
    assert target_store is not None
    set_id = await latest_complete(cfg, settings=settings, store=target_store) if backup_set in ("latest", "") else check_set_id(backup_set)
    async with target_store.materialize(set_id, cfg.effective_restore_work_dir(settings)) as directory:
        yield directory, set_id, f"{target_store.describe()}/{set_id}"


def _config_for(backup_set: str | Path, target: BackupTarget | None, config: BackupConfig | None) -> BackupConfig:
    if config is not None:
        return config if target is None else config.model_copy(update={"target": target})
    if target is not None:
        return BackupConfig(target=target)
    if isinstance(backup_set, Path):
        return BackupConfig(target=LocalTarget(path=backup_set.resolve().parent))
    raise BackupError("a backup set id needs a target or a BackupConfig", code=BACKUP_CONFIG_INVALID)


async def _write_report(report: RestoreReport, directory: Path, set_id: str, target_store: TargetStore | None, work_dir: Path) -> None:
    payload = report.model_dump(mode="json")
    try:
        if target_store is None:
            await asyncio.to_thread(write_json, directory / RESTORE_REPORT, payload)
            report.report_path = str(directory / RESTORE_REPORT)
            return
        with _private_tmp(work_dir, "report-") as tmp:
            await asyncio.to_thread(write_json, tmp / RESTORE_REPORT, payload)
            await target_store.put_file(set_id, tmp / RESTORE_REPORT, RESTORE_REPORT)
        report.report_path = f"{target_store.describe()}/{set_id}/{RESTORE_REPORT}"
    except (OSError, BackupError) as exc:
        report.warnings.append(f"restore report could not be written next to the backup: {exc}")
        log.error("restore report not written", extra={"set_id": set_id, "error": str(exc)[:300]})


# ================================================================================================ API
async def restore_test(
    backup_set: str | Path = "latest",
    *,
    config: BackupConfig | None = None,
    target: BackupTarget | None = None,
    settings: Settings | None = None,
    sessionmaker: SessionMaker | None = None,
    hosts: HostsConfig | None = None,
    store: TargetStore | None = None,
    write_report: bool = True,
) -> RestoreReport:
    """Restore ``backup_set`` (set id, ``latest`` or a set directory) into a clean temporary database and verify it."""
    cfg = _config_for(backup_set, target, config)
    settings = settings or get_settings()
    target_store = None if isinstance(backup_set, Path) else build_store(cfg, settings, hosts=hosts, store=store)
    try:
        async with (
            backup_lock(cfg.lock_path(settings), wait_seconds=cfg.lock_wait_seconds, what="restore-test"),
            _materialized(backup_set, cfg, settings, target_store) as (directory, set_id, location),
        ):
            report = await _restore_test_dir(directory, cfg, settings, location=location)
            if write_report:
                report.report_path = None
                await _write_report(report, directory, set_id, target_store, cfg.effective_restore_work_dir(settings))
    except BackupError as exc:  # no set to test (none on the target, fetch failed, …): monitoring must see it too
        if exc.code != BACKUP_LOCKED:
            await events.emit(
                sessionmaker,
                events.RESTORE_TEST_FAILED,
                set_id=backup_set if isinstance(backup_set, str) and backup_set != "latest" else None,
                payload={"ok": False, "code": exc.code, "error": exc.message[:500]},
                severity=Severity.error,
            )
        raise
    await events.emit(
        sessionmaker,
        events.RESTORE_TEST_PASSED if report.ok else events.RESTORE_TEST_FAILED,
        set_id=report.set_id,
        payload={
            "ok": report.ok,
            "location": report.location,
            "tables": report.tables_restored,
            "rows_expected": report.rows_expected,
            "rows_restored": report.rows_restored,
            "alembic_revision": report.alembic_actual,
            "failed_checks": report.failed_checks(),
            "temp_database_dropped": report.database_dropped,
            "duration_ms": report.duration_ms,
        },
        severity=Severity.info if report.ok else Severity.error,
    )
    return report


def _prepare_files_dest(dest: Path, force: bool) -> None:
    if dest.exists():
        if not dest.is_dir() or dest.is_symlink():
            raise BackupError(f"files destination {dest} is not a directory", code=BACKUP_CONFIG_INVALID)
        if any(dest.iterdir()) and not force:
            raise BackupError(f"files destination {dest} is not empty (use force)", code=RESTORE_TARGET_NOT_EMPTY)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)


async def restore_full(
    backup_set: str | Path,
    target_db_url: str,
    *,
    force: bool = False,
    password_ref: str | None = None,
    files_dest: Path | None = None,
    config: BackupConfig | None = None,
    target: BackupTarget | None = None,
    settings: Settings | None = None,
    sessionmaker: SessionMaker | None = None,
    hosts: HostsConfig | None = None,
    store: TargetStore | None = None,
) -> RestoreReport:
    """Disaster recovery: restore ``backup_set`` into ``target_db_url`` (+ files into ``files_dest``) and verify.

    Refuses a set that fails verification and a non-empty database unless ``force`` (``--clean --if-exists``:
    objects of the backup are replaced, objects not in the backup stay). Run with the services stopped.
    """
    cfg = _config_for(backup_set, target, config)
    settings = settings or get_settings()
    started = time.monotonic()
    conn = resolve_conn(target_db_url, password_ref, store=SecretStore(env=settings.env))
    valid_dbname(conn.dbname)
    aconn = await conn.connect()
    try:
        empty = await database_is_empty(aconn)
    finally:
        await aconn.close()
    if not empty and not force:
        raise BackupError(
            f"target database {conn.display()} is not empty; restore into a new database or pass force",
            code=RESTORE_TARGET_NOT_EMPTY,
        )
    if files_dest is not None:
        await asyncio.to_thread(_prepare_files_dest, files_dest, force)
    target_store = None if isinstance(backup_set, Path) else build_store(cfg, settings, hosts=hosts, store=store)
    work_dir = cfg.effective_restore_work_dir(settings)
    async with (
        backup_lock(cfg.lock_path(settings), wait_seconds=cfg.lock_wait_seconds, what="restore-full"),
        _materialized(backup_set, cfg, settings, target_store) as (directory, _set_id, location),
    ):
        report = RestoreReport(
            kind="restore_full", set_id=directory.name, started_at=datetime.now(UTC), location=location, database=conn.display()
        )
        verify, manifest = await verify_set_dir(
            directory, signing_key=signing_key(cfg, settings), deep=True, location=location, pg_bin_dir=cfg.pg_bin_dir
        )
        _apply_verify(report, verify, manifest)
        if manifest is None or not verify.ok:
            await _emit_full(sessionmaker, report.finish(started))
            raise BackupError(
                f"backup set failed verification, not restored: {'; '.join(verify.problems[:3])}",
                code=BACKUP_VERIFY_FAILED,
                details={"problems": verify.problems[:20]},
            )
        report.set_id = manifest.set_id
        if not empty:
            report.warnings.append("target database was not empty: restored with --clean --if-exists (force)")
        if files_dest is not None:
            for name in (CONFIG_TAR, ARTIFACTS_TAR):
                count = await asyncio.to_thread(extract_tarball, directory / name, files_dest)
                report.add(f"extract:{name}", True, f"{files_dest} ({count} files in total)")
        restored = await pg_restore_into(
            directory / DB_DUMP,
            conn,
            manifest,
            report,
            pg_bin_dir=cfg.pg_bin_dir,
            jobs=cfg.restore_test.jobs,
            timeout_seconds=cfg.restore_test.timeout_seconds,
            clean=not empty,
            work_dir=work_dir,
        )
        if restored:
            await compare_with_manifest(conn, manifest, report, extra_tables_fail=False)
        else:
            report.error = "pg_restore failed (see checks); with jobs > 1 the target may be partially restored"
    report.finish(started)
    if not report.ok and report.error is None:
        report.error = f"restore verification failed: {', '.join(report.failed_checks())}"
    await _emit_full(sessionmaker, report)
    if not report.ok:
        log.error("full restore failed", extra={"set_id": report.set_id, "failed": ",".join(report.failed_checks())})
        if not restored:
            raise BackupError(report.error or "restore failed", code=RESTORE_FAILED, details={"report": report.model_dump(mode="json")})
    return report


async def _emit_full(sessionmaker: SessionMaker | None, report: RestoreReport) -> None:
    await events.emit(
        sessionmaker,
        events.RESTORE_FULL_COMPLETED if report.ok else events.RESTORE_FULL_FAILED,
        set_id=report.set_id,
        payload={
            "ok": report.ok,
            "database": report.database,
            "rows_restored": report.rows_restored,
            "failed_checks": report.failed_checks(),
            "duration_ms": report.duration_ms,
        },
        severity=Severity.info if report.ok else Severity.error,
    )
