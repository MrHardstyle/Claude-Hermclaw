"""Adapter: the database tools behind the tool engine's operations-tool protocol (``hermclaw.tools.ops``).

The tool engine has validated the arguments (``DbQueryArgs`` …) and the capability gate (``OPS_TOOLS_BY_CAPABILITY``:
only ``database`` steps); :class:`~hermclaw.dbtools.tools.DbTools` checks the capability again (defence in depth),
applies the target policy and audits. Project migration commands (alembic) run through the sandbox
``CommandExecutor`` of the job workspace when the runtime passes one (``build_provider(..., executor=...)``);
without an executor they run as a local subprocess, which :class:`~hermclaw.dbtools.runner.LocalMigrationRunner`
refuses in production (raw SQL migrations never need it).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.tools import DB_TOOLS, ToolName
from hermclaw.core.config import HermclawConfig
from hermclaw.core.interfaces import CommandExecutor
from hermclaw.core.settings import Settings
from hermclaw.dbtools.policy import DbCallContext
from hermclaw.dbtools.runner import ExecutorMigrationRunner, MigrationCommandRunner
from hermclaw.dbtools.tools import DbToolResult, DbTools
from hermclaw.security.secrets import SecretStore
from hermclaw.tools.ops import OpsCall, OpsResult
from hermclaw.tools.registry import DbMigrationApplyArgs, DbMigrationGenerateArgs, DbQueryArgs, DbRestoreTestArgs, DbTargetArgs

DEFAULT_OUTPUT_CHARS = 12000


class DbOpsProvider:
    """``OpsToolProvider`` for ``db_introspect`` … ``db_restore_test``."""

    def __init__(
        self,
        tools: DbTools,
        *,
        executor: CommandExecutor | None = None,
        image: str | None = None,
        output_chars: int = DEFAULT_OUTPUT_CHARS,
    ) -> None:
        self.tools = tools
        self.executor = executor
        self.image = image
        self.output_chars = output_chars

    def provides(self, tool: ToolName) -> bool:
        return tool in DB_TOOLS

    def _runner(self, call: OpsCall) -> MigrationCommandRunner | None:
        if self.executor is None:
            return None
        return ExecutorMigrationRunner(self.executor, call.workspace, step_id=call.step_id, attempt_id=call.attempt_id, image=self.image)

    async def call(self, call: OpsCall) -> OpsResult:
        ctx = DbCallContext(job_id=call.job_id, step_id=call.step_id, attempt_id=call.attempt_id, capability=call.capability, actor="agent")
        args = call.args
        result: DbToolResult
        if call.tool == ToolName.db_introspect and isinstance(args, DbTargetArgs):
            result = await self.tools.db_introspect(args.target, ctx=ctx)
        elif call.tool == ToolName.db_query_readonly and isinstance(args, DbQueryArgs):
            result = await self.tools.db_query_readonly(
                args.target, args.sql, args.params, row_limit=args.row_limit, timeout=args.timeout_seconds, ctx=ctx
            )
        elif call.tool == ToolName.db_migration_generate and isinstance(args, DbMigrationGenerateArgs):
            result = await self.tools.db_migration_generate(call.workspace.path, args.description, path=args.path, ctx=ctx)
        elif call.tool == ToolName.db_migration_apply_sandbox and isinstance(args, DbMigrationApplyArgs):
            result = await self.tools.db_migration_apply_sandbox(
                call.workspace.path,
                path=args.path,
                target_kind=args.target_kind,
                seed_schema_from=args.seed_schema_from,
                runner=self._runner(call),
                ctx=ctx,
            )
        elif call.tool == ToolName.db_backup and isinstance(args, DbTargetArgs):
            result = await self.tools.db_backup(args.target, ctx=ctx)
        elif call.tool == ToolName.db_restore_test and isinstance(args, DbRestoreTestArgs):
            result = await self.tools.db_restore_test(args.artifact_id, ctx=ctx)
        else:
            return OpsResult(
                False, f"{call.tool.value} is not a database tool or got unexpected arguments", error_code="OPS_TOOL_UNAVAILABLE"
            )
        return OpsResult(result.ok, result.render(self.output_chars), result.to_dict(), error_code=None if result.ok else result.code)


def build_provider(
    config: HermclawConfig,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    executor: CommandExecutor | None = None,
) -> DbOpsProvider:
    """Factory used by ``hermclaw.runtime.ops``: targets from ``<config_dir>/databases.yaml`` (absent = no targets)."""
    tools = DbTools.from_settings(settings, sessionmaker, secrets=SecretStore(env=settings.env))
    return DbOpsProvider(tools, executor=executor, output_chars=config.policies.coder.tool_output_chars)


__all__ = ["DbOpsProvider", "build_provider"]
