"""The six database tools of Bauplan §30 behind one policy + audit facade (P27 27.1–27.7).

=============================  =====================================================================================
``db_introspect``              schema description of a registered target (read-only session)
``db_query_readonly``          one statically checked read statement, DB-enforced read-only, row cap + timeout
``db_migration_generate``      next migration skeleton of the project's framework in the workspace (no database)
``db_migration_apply_sandbox`` upgrade → downgrade → upgrade on a throw-away sandbox database (optionally seeded
                               with the schema of a target), fingerprints prove downgrade/re-upgrade
``db_backup``                  dump of a target into the artifact store with row-count snapshot and sha256
``db_restore_test``            restore of a backup artifact into a sandbox + table/row-count comparison
=============================  =====================================================================================

Every call – successful, failed or refused – writes exactly one ``db.tool`` event (credential-free target description,
redacted payload); external commands additionally write ``command_runs`` rows. Registered targets are only read; writes
happen exclusively in sandbox databases the tools create and always drop again (:mod:`hermclaw.dbtools.policy`).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.errors import HermclawError
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.core.settings import Settings
from hermclaw.dbtools.backup import ArtifactStore, BackupService
from hermclaw.dbtools.errors import (
    DB_COMMAND_TIMEOUT,
    DB_MIGRATION_FAILED,
    DB_MIGRATION_INVALID,
    DB_MIGRATION_UNSUPPORTED,
    DB_QUERY_TIMEOUT,
    DB_RESTORE_MISMATCH,
    DB_SANDBOX_UNAVAILABLE,
    DB_TARGET_UNSUPPORTED,
    DB_TOOL_ERROR,
    DbPolicyError,
    DbToolError,
)
from hermclaw.dbtools.introspect import describe, fingerprint, render
from hermclaw.dbtools.migrations import (
    AlembicDriver,
    CommandDriver,
    Framework,
    MigrationDriver,
    MigrationProject,
    RawSqlDriver,
    VerifyScope,
    applied_raw_position,
    detect_framework,
    generate_migration,
    parse_sql_migrations,
    resolve_project_dir,
    schema_snapshot,
    verify_migrations,
)
from hermclaw.dbtools.policy import DbAuditor, DbCallContext, DbOperation, DbOutcome, DbPolicy
from hermclaw.dbtools.query import Params, query_readonly
from hermclaw.dbtools.runner import LocalMigrationRunner, MigrationCommandRunner, ProcessResult
from hermclaw.dbtools.sandbox import PgSandboxServer, SandboxDatabase, SqliteSandboxes
from hermclaw.dbtools.targets import (
    ConnectionInfo,
    DbToolsConfig,
    ResolvedTarget,
    TargetKind,
    TargetRegistry,
    load_dbtools_config,
    redact_dsn,
    resolve_connection,
)
from hermclaw.security.secrets import SecretStore

log = get_logger(__name__)
CONFIG_FILE = "databases.yaml"
TIMEOUT_CODES = frozenset({DB_QUERY_TIMEOUT, DB_COMMAND_TIMEOUT})


@dataclass
class DbToolResult:
    tool: str
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    code: str | None = None
    text: str = ""  # body for the model (schema rendering, result table, step report)
    audit: dict[str, Any] = field(default_factory=dict)  # extra event payload (never rows of user data)

    def render(self, limit: int = 12000) -> str:
        head = f"{self.tool}: {'ok' if self.ok else 'FAILED'}" + (f" [{self.code}]" if self.code else "") + f" – {self.message}"
        text = head + (f"\n{self.text}" if self.text else "")
        if len(text) > limit:
            text = text[: max(0, limit - 60)] + f"\n…[{len(text) - limit + 60} chars truncated]"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {**self.data, "tool": self.tool, "ok": self.ok, "code": self.code, "message": self.message}


def _clamp(value: float | None, default: float, maximum: float, minimum: float = 1) -> float:
    chosen = default if value is None else value
    return max(minimum, min(float(chosen), maximum))


class DbTools:
    def __init__(
        self,
        config: DbToolsConfig,
        *,
        registry: TargetRegistry,
        artifacts_dir: Path,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        redactor: Redactor = DEFAULT_REDACTOR,
        sandbox_server: PgSandboxServer | None = None,
        migration_runner: MigrationCommandRunner | None = None,
    ) -> None:
        self.config = config
        self.limits = config.limits
        self.registry = registry
        self.redactor = redactor
        prefix = config.sandbox.name_prefix if config.sandbox else "hermclaw_sbx_"
        self.policy = DbPolicy(allowed_capabilities=config.allowed_capabilities, sandbox_prefix=prefix)
        self.auditor = DbAuditor(sessionmaker, redactor)
        self.backup_dir = config.backup_dir or artifacts_dir / "db-backups"
        self.store = ArtifactStore(sessionmaker, self.backup_dir)
        self.sqlite_sandboxes = SqliteSandboxes(prefix=prefix, base_dir=config.sqlite_sandbox_dir)
        self.backups = BackupService(self.auditor, self.store, self.limits, config.pg_bin_dir, self.sqlite_sandboxes)
        self._sandbox_server = sandbox_server
        self.migration_runner = migration_runner

    @classmethod
    def from_config(
        cls,
        config: DbToolsConfig,
        *,
        artifacts_dir: Path,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        secrets: SecretStore | None = None,
        redactor: Redactor = DEFAULT_REDACTOR,
        migration_runner: MigrationCommandRunner | None = None,
    ) -> DbTools:
        registry = TargetRegistry.from_targets(config.targets, secrets=secrets, redactor=redactor)
        return cls(
            config,
            registry=registry,
            artifacts_dir=artifacts_dir,
            sessionmaker=sessionmaker,
            redactor=redactor,
            migration_runner=migration_runner,
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession] | None, *, secrets: SecretStore | None = None
    ) -> DbTools:
        config = load_dbtools_config(settings.config_dir / CONFIG_FILE)
        return cls.from_config(
            config, artifacts_dir=settings.artifacts_dir, sessionmaker=sessionmaker, secrets=secrets or SecretStore(env=settings.env)
        )

    # ------------------------------------------------------------------------------ infrastructure
    def sandbox_server(self) -> PgSandboxServer:
        """The PostgreSQL sandbox server (admin secret resolved on first use)."""
        if self._sandbox_server is not None:
            return self._sandbox_server
        sbx = self.config.sandbox
        if sbx is None:
            raise DbToolError(
                "no PostgreSQL sandbox server configured (databases.yaml: sandbox.admin_dsn_ref)", code=DB_SANDBOX_UNAVAILABLE
            )
        admin = resolve_connection(
            sbx.admin_dsn_ref, TargetKind.postgresql, password_ref=sbx.password_ref, secrets=self.registry.secrets, redactor=self.redactor
        )
        self._sandbox_server = PgSandboxServer(
            admin,
            policy=self.policy,
            client_host=sbx.client_host,
            client_port=sbx.client_port,
            connect_timeout=self.limits.connect_timeout_seconds,
            redactor=self.redactor,
        )
        return self._sandbox_server

    async def _call(
        self,
        op: DbOperation,
        ctx: DbCallContext | None,
        payload: dict[str, Any],
        work: Callable[[dict[str, Any]], Awaitable[DbToolResult]],
    ) -> DbToolResult:
        context = ctx or DbCallContext(actor="runtime")
        started = time.monotonic()
        try:
            self.policy.check_caller(op, context)
            result = await work(payload)
            outcome = DbOutcome.ok if result.ok else DbOutcome.failed
        except asyncio.CancelledError:
            body = {**payload, "ok": False, "code": "CANCELLED", "message": "the tool call was cancelled"}
            await asyncio.shield(
                self.auditor.event(op.value, context, DbOutcome.failed, body, duration_ms=int((time.monotonic() - started) * 1000))
            )
            raise
        except DbPolicyError as exc:
            result, outcome = DbToolResult(op.value, False, exc.message, code=exc.code), DbOutcome.denied
        except HermclawError as exc:
            outcome = DbOutcome.timed_out if exc.code in TIMEOUT_CODES else DbOutcome.failed
            result = DbToolResult(op.value, False, self.redactor.text(exc.message), code=exc.code)
        except Exception as exc:
            log.exception("database tool crashed", extra={"tool": op.value})
            result, outcome = DbToolResult(op.value, False, f"internal error ({type(exc).__name__})", code=DB_TOOL_ERROR), DbOutcome.failed
        # nothing registered as a secret (DSNs, passwords, sandbox credentials) reaches the model or the tool_calls row
        result.message = redact_dsn(self.redactor.text(result.message))
        result.text = redact_dsn(self.redactor.text(result.text))
        result.data = self.redactor.obj(result.data)
        body = {**payload, "ok": result.ok, "code": result.code, "message": result.message[:1000], **result.audit}
        await self.auditor.event(op.value, context, outcome, body, duration_ms=int((time.monotonic() - started) * 1000))
        return result

    def _resolve(self, op: DbOperation, name: str, payload: dict[str, Any]) -> ResolvedTarget:
        target = self.registry.get(name)
        payload.update(
            {"target": target.name, "kind": target.kind.value, "environment": target.environment.value, "read_only": target.read_only}
        )
        self.policy.check_target(op, target)
        resolved = self.registry.resolve(target)
        payload["server"] = resolved.conn.display()
        return resolved

    # ------------------------------------------------------------------------------ 27.1
    async def db_introspect(self, target: str, *, ctx: DbCallContext | None = None, schemas: list[str] | None = None) -> DbToolResult:
        async def work(payload: dict[str, Any]) -> DbToolResult:
            resolved = self._resolve(DbOperation.introspect, target, payload)
            desc = await describe(
                resolved.conn,
                schemas=schemas,
                timeout_seconds=self.limits.introspect_timeout_seconds,
                connect_timeout=self.limits.connect_timeout_seconds,
                max_tables=self.limits.introspect_max_tables,
                read_only=True,
            )
            fp = fingerprint(desc)
            message = f"{desc['table_count']} tables, {desc['view_count']} views" + (" (truncated)" if desc["truncated"] else "")
            return DbToolResult(
                DbOperation.introspect.value,
                True,
                message,
                data={"target": resolved.describe(), "schema": desc, "fingerprint": fp},
                text=render(desc, title=resolved.name, limit=self.limits.max_output_chars),
                audit={"table_count": desc["table_count"], "view_count": desc["view_count"], "fingerprint": fp},
            )

        return await self._call(DbOperation.introspect, ctx, {"target": target[:100]}, work)

    # ------------------------------------------------------------------------------ 27.2
    async def db_query_readonly(
        self,
        target: str,
        sql: str,
        params: Params = None,
        *,
        row_limit: int | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 – statement_timeout of the query
        ctx: DbCallContext | None = None,
    ) -> DbToolResult:
        limit = int(_clamp(row_limit, self.limits.default_row_limit, self.limits.max_row_limit))
        seconds = _clamp(timeout, self.limits.default_timeout_seconds, self.limits.max_timeout_seconds, minimum=0.05)

        async def work(payload: dict[str, Any]) -> DbToolResult:
            resolved = self._resolve(DbOperation.query, target, payload)
            res = await query_readonly(
                resolved.conn,
                sql,
                params,
                row_limit=limit,
                timeout_seconds=seconds,
                connect_timeout=self.limits.connect_timeout_seconds,
                max_cell_chars=self.limits.max_cell_chars,
            )
            message = f"{res.row_count} row{'s' if res.row_count != 1 else ''}" + (" (truncated at the row limit)" if res.truncated else "")
            return DbToolResult(
                DbOperation.query.value,
                True,
                message,
                data={"target": resolved.describe(), **res.to_dict()},
                text=res.render(self.limits.max_output_chars),
                audit={"statement": res.statement, "row_count": res.row_count, "truncated": res.truncated, "query_ms": res.duration_ms},
            )

        payload = {"target": target[:100], "sql": sql[:2000], "row_limit": limit, "timeout_seconds": seconds}
        return await self._call(DbOperation.query, ctx, payload, work)

    # ------------------------------------------------------------------------------ 27.3
    async def db_migration_generate(
        self, workspace_root: Path, description: str, *, path: str = ".", ctx: DbCallContext | None = None, now: datetime | None = None
    ) -> DbToolResult:
        async def work(payload: dict[str, Any]) -> DbToolResult:
            project_dir = resolve_project_dir(workspace_root, path)
            project = await asyncio.to_thread(detect_framework, project_dir, workspace_root=workspace_root)
            payload["framework"] = project.framework.value
            generated = await asyncio.to_thread(generate_migration, project, description, now=now)
            info = generated.describe()
            payload["files"] = info["files"]
            lines = [f"created {f}" for f in info["files"]]
            if generated.revision:
                lines.append(f"revision {generated.revision} (revises {generated.down_revision or 'base'})")
            lines.append("Fill in the upgrade/up and downgrade/down sections; db_migration_apply_sandbox verifies both directions.")
            lines += [f"note: {n}" for n in generated.notes]
            return DbToolResult(
                DbOperation.migration_generate.value,
                True,
                f"{project.framework.value} migration {info['path']}",
                data={"project": project.describe(), **info},
                text="\n".join(lines),
            )

        payload = {"path": path[:500], "description": description[:200]}
        return await self._call(DbOperation.migration_generate, ctx, payload, work)

    # ------------------------------------------------------------------------------ 27.4
    async def db_migration_apply_sandbox(
        self,
        workspace_root: Path,
        *,
        path: str = ".",
        target_kind: str = "postgresql",
        seed_schema_from: str | None = None,
        command: Sequence[str] | None = None,
        downgrade_command: Sequence[str] | None = None,
        scope: VerifyScope = "latest",
        runner: MigrationCommandRunner | None = None,
        ctx: DbCallContext | None = None,
    ) -> DbToolResult:
        context = ctx or DbCallContext(actor="runtime")

        async def work(payload: dict[str, Any]) -> DbToolResult:
            try:
                kind = TargetKind(target_kind)
            except ValueError:
                raise DbToolError(f"unknown sandbox kind '{target_kind[:40]}'", code=DB_TARGET_UNSUPPORTED) from None
            if kind == TargetKind.mysql:
                raise DbToolError("MySQL sandboxes are not supported by this runtime", code=DB_TARGET_UNSUPPORTED)
            if scope not in ("latest", "all", "none"):
                raise DbToolError(f"unknown verification scope '{scope}'", code=DB_MIGRATION_INVALID)
            project_dir = resolve_project_dir(workspace_root, path)
            project: MigrationProject | None
            try:
                project = await asyncio.to_thread(detect_framework, project_dir, workspace_root=workspace_root)
            except DbToolError:
                if command is None:
                    raise
                project = None
            payload["framework"] = project.framework.value if project else "command"
            if command is None and project is not None and not project.can_apply:
                hint = f": {project.hint}" if project.hint else " (pass the project's migration command)"
                raise DbToolError(
                    f"{project.framework.value} migrations are not applied by the DB tools without an explicit command{hint}",
                    code=DB_MIGRATION_UNSUPPORTED,
                )
            seed: ResolvedTarget | None = None
            if seed_schema_from:
                seed = self._resolve(DbOperation.seed_schema, seed_schema_from, payload)
                if seed.target.kind != kind:
                    raise DbToolError(
                        f"seed target '{seed.name}' is {seed.target.kind.value}, the sandbox is {kind.value}", code=DB_TARGET_UNSUPPORTED
                    )
            chosen_runner = runner or self.migration_runner or LocalMigrationRunner()
            payload["runner"] = chosen_runner.name
            server = self.sandbox_server() if kind == TargetKind.postgresql else None
            sandbox = await self._create_sandbox(kind, server, seed, chosen_runner, project_dir)
            payload["sandbox"] = sandbox.name
            try:
                seed_info: dict[str, Any] = {}
                if seed is not None and server is not None:
                    seed_info = await self.backups.seed_postgres_schema(seed, sandbox, server, context)
                elif seed is not None:
                    seed_info = {"seeded_from": seed.name, "copy": True, "warnings": []}
                driver, warnings = await self._driver(
                    project,
                    sandbox,
                    runner=chosen_runner,
                    project_dir=project_dir,
                    ctx=context,
                    command=command,
                    downgrade_command=downgrade_command,
                    seeded=seed is not None,
                )

                async def snapshot() -> tuple[str, dict[str, Any]]:
                    return await schema_snapshot(
                        sandbox.conn,
                        timeout_seconds=self.limits.introspect_timeout_seconds,
                        connect_timeout=self.limits.connect_timeout_seconds,
                    )

                report = await verify_migrations(driver, snapshot, scope=scope)
            finally:
                await asyncio.shield(self._drop_sandbox(sandbox, server))
            report.warnings[:0] = warnings + list(seed_info.get("warnings") or [])
            data = {
                "project": project.describe() if project else {"framework": "command"},
                "sandbox": sandbox.describe(),
                "seed": {k: v for k, v in seed_info.items() if k != "warnings"},
                **report.to_dict(),
            }
            failed = report.failed_step()
            bad = [c["check"] for c in report.checks if not c["ok"]]
            parts = [f"schema check failed: {', '.join(bad)}"] if bad else []
            if failed is not None:
                parts.append(f"step '{failed['step']}' failed: {str(failed['detail']).splitlines()[0][:400]}")
            message = "; ".join(parts) if parts else f"{len(report.chain)} migration(s) verified in sandbox {sandbox.name} ({scope})"
            return DbToolResult(
                DbOperation.migration_apply.value,
                report.ok,
                message,
                data=data,
                code=None if report.ok else DB_MIGRATION_FAILED,
                text=_render_report(data),
                audit={
                    "steps": [{"step": s["step"], "to": s["to"], "ok": s["ok"]} for s in report.steps],
                    "checks": [{"check": c["check"], "ok": c["ok"]} for c in report.checks],
                    "scope": scope,
                },
            )

        payload = {"path": path[:500], "target_kind": target_kind[:20], "seed_schema_from": seed_schema_from, "scope": scope}
        if command is not None:
            payload["command"] = " ".join(command)[:500]
        return await self._call(DbOperation.migration_apply, context, payload, work)

    async def _create_sandbox(
        self,
        kind: TargetKind,
        server: PgSandboxServer | None,
        seed: ResolvedTarget | None,
        runner: MigrationCommandRunner,
        project_dir: Path,
    ) -> SandboxDatabase:
        if kind == TargetKind.postgresql:
            assert server is not None
            return await server.create(label=f"migrate-{project_dir.name}")
        source = seed.conn.path if seed is not None else None
        if source is not None and not source.is_file():
            raise DbToolError(f"SQLite seed database {source.name} does not exist", code=DB_SANDBOX_UNAVAILABLE)
        base_dir = runner.sqlite_dir(project_dir) or self.config.sqlite_sandbox_dir
        return await asyncio.to_thread(self.sqlite_sandboxes.create, source=source, base_dir=base_dir)

    async def _drop_sandbox(self, sandbox: SandboxDatabase, server: PgSandboxServer | None) -> None:
        try:
            if sandbox.kind == TargetKind.postgresql and server is not None:
                await server.drop(sandbox)
            else:
                await asyncio.to_thread(self.sqlite_sandboxes.drop, sandbox)
        except Exception:
            log.exception("sandbox database could not be dropped", extra={"sandbox": sandbox.name})

    async def _driver(
        self,
        project: MigrationProject | None,
        sandbox: SandboxDatabase,
        *,
        runner: MigrationCommandRunner,
        project_dir: Path,
        ctx: DbCallContext,
        command: Sequence[str] | None,
        downgrade_command: Sequence[str] | None,
        seeded: bool,
    ) -> tuple[MigrationDriver, list[str]]:
        warnings: list[str] = []

        async def run(argv: Sequence[str], label: str, env: Mapping[str, str], cwd: Path, timeout: float) -> ProcessResult:  # noqa: ASYNC109
            return await self.auditor.run_migration_command(ctx, runner, argv, display=label, env=env, cwd=cwd, timeout=timeout)

        timeout = self.limits.migration_timeout_seconds
        if command is not None:
            env = sandbox_env(sandbox, "", cwd=project_dir, runner=runner)
            return CommandDriver(
                command, downgrade_command=downgrade_command, run=run, env=env, cwd=project_dir, timeout_seconds=timeout
            ), warnings
        assert project is not None
        if project.framework == Framework.raw_sql:
            assert project.migrations_dir is not None
            migrations = await asyncio.to_thread(parse_sql_migrations, project.migrations_dir)
            applied = -1
            if seeded:
                applied, table = await applied_raw_position(sandbox.conn, migrations, connect_timeout=self.limits.connect_timeout_seconds)
                if table is None:
                    warnings.append("no migration bookkeeping table in the seed – assuming no migration has been applied")
            driver = RawSqlDriver(
                migrations, sandbox.conn, timeout_seconds=timeout, connect_timeout=self.limits.connect_timeout_seconds, applied=applied
            )
            return driver, warnings
        if project.framework == Framework.alembic:
            scheme = str(project.settings.get("url_scheme") or "")
            env = sandbox_env(sandbox, scheme, cwd=project.root, runner=runner)
            alembic = AlembicDriver(
                project,
                run=run,
                python=self.config.migration_python,
                env=env,
                state_conn=sandbox.conn,
                timeout_seconds=timeout,
                connect_timeout=self.limits.connect_timeout_seconds,
            )
            return alembic, warnings + alembic.warnings
        raise DbToolError(f"{project.framework.value} migrations are not applied by the DB tools", code=DB_MIGRATION_UNSUPPORTED)

    # ------------------------------------------------------------------------------ 27.5
    async def db_backup(self, target: str, *, ctx: DbCallContext | None = None) -> DbToolResult:
        context = ctx or DbCallContext(actor="runtime")

        async def work(payload: dict[str, Any]) -> DbToolResult:
            resolved = self._resolve(DbOperation.backup, target, payload)
            info = await self.backups.backup(resolved, context, backup_dir=self.backup_dir)
            text = (
                f"artifact {info['artifact_id']} ({info['format']}, {info['size_bytes']} bytes, sha256 {info['sha256'][:16]}…)\n"
                f"{info['tables']} tables, {info['rows']} rows recorded"
                + ("" if info["consistent"] else " (counts not taken on the dump snapshot)")
            )
            return DbToolResult(
                DbOperation.backup.value,
                True,
                f"backup of {resolved.name} stored as artifact {info['artifact_id']}",
                data={"target": resolved.describe(), **info},
                text=text,
                audit={k: info[k] for k in ("artifact_id", "format", "size_bytes", "sha256", "tables", "rows", "consistent")},
            )

        return await self._call(DbOperation.backup, context, {"target": target[:100]}, work)

    # ------------------------------------------------------------------------------ 27.6
    async def db_restore_test(self, artifact_id: str, *, ctx: DbCallContext | None = None) -> DbToolResult:
        context = ctx or DbCallContext(actor="runtime")

        async def work(payload: dict[str, Any]) -> DbToolResult:
            server: PgSandboxServer | None
            try:
                server = self.sandbox_server()
            except DbToolError:
                server = None  # only needed for PostgreSQL dumps; the service reports DB_SANDBOX_UNAVAILABLE then
            info = await self.backups.restore_test(artifact_id, context, pg_server=server)
            payload.update({"target": info.get("target"), "sandbox": info.get("sandbox")})
            ok = bool(info["ok"])
            problems = len(info["missing_tables"]) + len(info["extra_tables"]) + len(info["count_mismatches"])
            message = (
                f"restore of {info['tables']} tables verified (row counts match)"
                if ok
                else f"restore differs from the backup snapshot ({problems} difference(s))"
            )
            lines = [message]
            lines += [f"missing table {t}" for t in info["missing_tables"][:20]]
            lines += [f"unexpected table {t}" for t in info["extra_tables"][:20]]
            lines += [f"{m['table']}: expected {m['expected']} rows, got {m['actual']}" for m in info["count_mismatches"][:20]]
            lines += [f"warning: {w}" for w in info.get("warnings", [])[:20]]
            if info.get("integrity_check") not in (None, "ok"):
                lines.append(f"integrity_check: {info['integrity_check']}")
            return DbToolResult(
                DbOperation.restore_test.value,
                ok,
                message,
                data=info,
                code=None if ok else DB_RESTORE_MISMATCH,
                text="\n".join(lines),
                audit={"artifact_id": info["artifact_id"], "tables": info["tables"], "differences": problems},
            )

        return await self._call(DbOperation.restore_test, context, {"artifact_id": str(artifact_id)[:64]}, work)

    # ------------------------------------------------------------------------------ recovery
    async def cleanup_orphan_sandboxes(self, *, max_age: timedelta | None = None, now: datetime | None = None) -> DbToolResult:
        """Drop sandbox databases left behind by crashed runs (runtime/operator only)."""
        ctx = DbCallContext(actor="runtime")

        async def work(payload: dict[str, Any]) -> DbToolResult:
            age = max_age or timedelta(hours=self.config.sandbox.orphan_max_age_hours if self.config.sandbox else 6.0)
            has_server = self.config.sandbox is not None or self._sandbox_server is not None
            dropped = await self.sandbox_server().cleanup_orphans(max_age=age, now=now) if has_server else []
            removed = await asyncio.to_thread(self.sqlite_sandboxes.cleanup_orphans, max_age=age, now=now)
            payload.update({"dropped": dropped, "sqlite_removed": removed})
            return DbToolResult(
                DbOperation.sandbox_cleanup.value,
                True,
                f"{len(dropped)} orphaned sandbox database(s) dropped, {len(removed)} SQLite sandbox(es) removed",
                data={"dropped": dropped, "sqlite_removed": removed},
            )

        return await self._call(DbOperation.sandbox_cleanup, ctx, {}, work)


def sandbox_env(sandbox: SandboxDatabase, scheme: str, *, cwd: Path, runner: MigrationCommandRunner) -> dict[str, str]:
    """Environment of a migration command: ``DATABASE_URL`` & co. point at the sandbox (credentials only here)."""
    client: ConnectionInfo = sandbox.client_conn
    if sandbox.kind == TargetKind.sqlite:
        assert client.path is not None
        sqlite_scheme = scheme if scheme.startswith("sqlite") else "sqlite"
        if runner.sqlite_dir(cwd) is not None:  # executed elsewhere: path relative to the command's cwd
            url = f"{sqlite_scheme}:///{Path(os.path.relpath(client.path, cwd)).as_posix()}"
        else:
            url = f"{sqlite_scheme}:///{client.path}"
        return {"DATABASE_URL": url, "HERMCLAW_SANDBOX_URL": url, "SQLALCHEMY_DATABASE_URI": url}
    pg_scheme = scheme if scheme.startswith("postgres") else "postgresql"
    if pg_scheme.startswith("postgres+") or pg_scheme == "postgres":
        pg_scheme = "postgresql" + pg_scheme[len("postgres") :]
    url = client.url(pg_scheme)
    plain = client.url("postgresql")
    return {**client.libpq_env(), "DATABASE_URL": plain, "HERMCLAW_SANDBOX_URL": url, "SQLALCHEMY_DATABASE_URI": url}


def _render_report(data: dict[str, Any]) -> str:
    lines = [f"framework: {data['project'].get('framework')}  sandbox: {data['sandbox'].get('sandbox')}  scope: {data.get('scope')}"]
    if data.get("seed"):
        lines.append(f"seeded from: {data['seed'].get('seeded_from')}")
    for s in data.get("steps", []):
        lines.append(f"[{'ok' if s['ok'] else 'FAILED'}] {s['step']} -> {s['to']} ({s['duration_ms']} ms): {str(s['detail'])[:600]}")
    for c in data.get("checks", []):
        lines.append(f"[{'ok' if c['ok'] else 'FAILED'}] check: {c['check']}")
        lines += [f"    {d}" for d in c.get("diff", [])]
    lines += [f"warning: {w}" for w in data.get("warnings", [])]
    return "\n".join(lines)


__all__ = ["CONFIG_FILE", "DbToolResult", "DbTools", "sandbox_env"]
