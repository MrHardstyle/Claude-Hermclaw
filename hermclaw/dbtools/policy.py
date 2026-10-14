"""Database tool policy and audit (P27 27.7).

Policy (Bauplan §30 "keine unkontrollierten Produktions-DB-Mutationen"):

- callers: an agent step may use the DB tools only with an allowed capability (default ``database``); the tool engine
  gates this already (``OPS_TOOLS_BY_CAPABILITY``), the check here is defence in depth;
- registered targets are only ever *read*: ``db_introspect``, ``db_query_readonly`` (enforced read-only), ``db_backup``
  and reading the schema to seed a sandbox – for every environment;
- writing to a registered target is refused for production and read-only targets and – because no DB tool writes to a
  target – is only reachable for ``environment: sandbox`` targets with ``read_only: false``;
- migrations are applied and backups restored **only** into throw-away sandbox databases created by the tools
  themselves (name ``<prefix><12 hex>``; only such names are ever dropped).

Audit: every call – executed, failed or refused – writes one ``db.tool`` event; every external command (pg_dump,
pg_restore, migration commands …) additionally a ``command_runs`` row (written *before* the command starts, completed
afterwards). DSNs appear only in their credential-free form; all payloads go through the redactor.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.dbtools.errors import (
    DB_CAPABILITY_DENIED,
    DB_POLICY_DENIED,
    DB_TARGET_UNSUPPORTED,
    DbPolicyError,
    DbToolError,
)
from hermclaw.dbtools.runner import ProcessResult
from hermclaw.dbtools.targets import DatabaseTarget, TargetEnvironment, TargetKind, redact_dsn
from hermclaw.events.store import append_event
from hermclaw.persistence.models import CommandRun

log = get_logger(__name__)
SOURCE_TYPE = "dbtools"
EXCERPT_CHARS = 8000


class DbOperation(StrEnum):
    introspect = "db_introspect"
    query = "db_query_readonly"
    migration_generate = "db_migration_generate"
    migration_apply = "db_migration_apply_sandbox"
    backup = "db_backup"
    restore_test = "db_restore_test"
    seed_schema = "db_seed_schema"  # reading a target's schema to seed a sandbox (part of migration_apply)
    sandbox_cleanup = "db_sandbox_cleanup"


#: operations that only read a registered target
TARGET_READ_OPERATIONS = frozenset({DbOperation.introspect, DbOperation.query, DbOperation.backup, DbOperation.seed_schema})


class DbOutcome(StrEnum):
    ok = "ok"
    failed = "failed"
    timed_out = "timed_out"
    denied = "denied"


SEVERITY: dict[DbOutcome, Severity] = {
    DbOutcome.ok: Severity.info,
    DbOutcome.failed: Severity.warning,
    DbOutcome.timed_out: Severity.warning,
    DbOutcome.denied: Severity.warning,
}


@dataclass(frozen=True)
class DbCallContext:
    """Who calls: job/step/attempt for the audit, the step capability for the gate."""

    job_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    tool_call_id: uuid.UUID | None = None
    capability: str | None = None
    actor: str = "agent"  # agent | runtime | operator


class DbPolicy:
    def __init__(self, *, allowed_capabilities: Iterable[str] = ("database",), sandbox_prefix: str = "hermclaw_sbx_") -> None:
        self.allowed_capabilities = frozenset(allowed_capabilities)
        self.sandbox_prefix = sandbox_prefix
        self._sandbox_re = re.compile(rf"^{re.escape(sandbox_prefix)}[0-9a-f]{{12}}$")

    def check_caller(self, op: DbOperation, ctx: DbCallContext) -> None:
        if ctx.actor in ("runtime", "operator"):
            return
        if ctx.capability not in self.allowed_capabilities:
            raise DbPolicyError(
                f"{op.value} is only available to steps with capability {sorted(self.allowed_capabilities)} "
                f"(got {ctx.capability or 'none'})",
                code=DB_CAPABILITY_DENIED,
            )

    def check_target(self, op: DbOperation, target: DatabaseTarget) -> None:
        """May ``op`` touch ``target``? Read operations: always; anything else: never for production/read-only."""
        if target.kind == TargetKind.mysql:
            raise DbToolError(f"target '{target.name}': MySQL targets are not supported by this runtime", code=DB_TARGET_UNSUPPORTED)
        if op in TARGET_READ_OPERATIONS:
            return
        self.check_target_write(op, target)

    def check_target_write(self, op: DbOperation, target: DatabaseTarget) -> None:
        if target.environment == TargetEnvironment.production or target.read_only:
            raise DbPolicyError(
                f"{op.value} would change target '{target.name}' ({target.environment.value}, "
                f"{'read-only' if target.read_only else 'writable'}): production and read-only targets are only read "
                "(introspect/query/backup); migrations run against throw-away sandbox databases only",
                code=DB_POLICY_DENIED,
            )
        if target.environment != TargetEnvironment.sandbox:
            raise DbPolicyError(
                f"{op.value}: writes are only allowed to sandbox targets (target '{target.name}' is {target.environment.value})",
                code=DB_POLICY_DENIED,
            )

    def is_sandbox_name(self, name: str) -> bool:
        return bool(self._sandbox_re.match(name))

    def check_sandbox_name(self, name: str) -> None:
        if not self.is_sandbox_name(name):
            raise DbPolicyError(f"'{name[:80]}' is not a sandbox database created by the DB tools", code=DB_POLICY_DENIED)


def clean_payload(redactor: Redactor, value: Any) -> Any:
    """Redactor + DSN password masking, recursively."""

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return redact_dsn(v)
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list | tuple):
            return [walk(x) for x in v]
        return v

    return walk(redactor.obj(value))


class DbAuditor:
    """Writes ``db.tool`` events and ``command_runs`` rows; audit problems never break a tool call."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None, redactor: Redactor = DEFAULT_REDACTOR) -> None:
        self.sessionmaker = sessionmaker
        self.redactor = redactor

    async def command_started(self, ctx: DbCallContext, display: str, *, classification: str, cwd: str | None, runner: str) -> uuid.UUID | None:
        if self.sessionmaker is None:
            return None
        run_id = uuid.uuid4()
        try:
            async with self.sessionmaker() as session, session.begin():
                session.add(
                    CommandRun(
                        id=run_id,
                        job_id=ctx.job_id,
                        step_id=ctx.step_id,
                        attempt_id=ctx.attempt_id,
                        tool_call_id=ctx.tool_call_id,
                        worker_id=None,
                        target="sandbox" if runner == "sandbox" else "local",
                        command=redact_dsn(self.redactor.text(display))[:4000],
                        classification=classification,
                        cwd=cwd,
                        exit_code=None,
                        timed_out=False,
                        network=True,
                    )
                )
        except Exception:
            log.exception("db tool command_runs row could not be written", extra={"command": display[:200]})
            return None
        return run_id

    async def command_finished(self, run_id: uuid.UUID | None, result: ProcessResult) -> None:
        if self.sessionmaker is None or run_id is None:
            return
        stdout = redact_dsn(self.redactor.text(result.stdout))
        stderr = redact_dsn(self.redactor.text(result.stderr or (result.error or "")))
        try:
            async with self.sessionmaker() as session, session.begin():
                await session.execute(
                    update(CommandRun)
                    .where(CommandRun.id == run_id)
                    .values(
                        exit_code=result.exit_code,
                        timed_out=result.timed_out,
                        stdout_excerpt=_excerpt(stdout),
                        stderr_excerpt=_excerpt(stderr),
                        duration_ms=result.duration_ms,
                    )
                )
        except Exception:
            log.exception("db tool command_runs row could not be completed", extra={"command_run_id": str(run_id)})

    async def event(
        self,
        tool: str,
        ctx: DbCallContext,
        outcome: DbOutcome,
        payload: dict[str, Any],
        *,
        duration_ms: int | None,
        source_id: str | None = None,
        severity: Severity | None = None,
    ) -> bool:
        body = clean_payload(self.redactor, {"tool": tool, "outcome": outcome.value, "actor": ctx.actor, "capability": ctx.capability, **payload})
        if self.sessionmaker is None:
            log.info("db tool call", extra={"tool": tool, "outcome": outcome.value})
            return False
        try:
            async with self.sessionmaker() as session, session.begin():
                await append_event(
                    session,
                    EventType.DB_TOOL,
                    source_type=SOURCE_TYPE,
                    source_id=(source_id or tool)[:100],
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    attempt_id=ctx.attempt_id,
                    severity=severity or SEVERITY[outcome],
                    payload=body,
                    duration_ms=duration_ms,
                )
        except Exception:
            log.exception("db tool event could not be written", extra={"tool": tool, "outcome": outcome.value})
            return False
        return True


def _excerpt(text: str) -> str | None:
    if not text:
        return None
    if len(text) <= EXCERPT_CHARS:
        return text
    half = EXCERPT_CHARS // 2
    return f"{text[:half]}\n…[{len(text) - EXCERPT_CHARS} chars omitted]…\n{text[-half:]}"


__all__ = [
    "SEVERITY",
    "TARGET_READ_OPERATIONS",
    "DbAuditor",
    "DbCallContext",
    "DbOperation",
    "DbOutcome",
    "DbPolicy",
    "clean_payload",
]
