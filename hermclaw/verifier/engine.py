"""The deterministic verifier (Bauplan §21, Phase 21).

``Verifier.run`` evaluates generic checks plus the step's acceptance evidence against a workspace, persists a
``verification_runs`` row with one ``verification_checks`` row per check and emits VERIFIER_STARTED /
VERIFIER_CHECK_FAILED / VERIFIER_FINISHED. It contains no task-, project- or benchmark-specific rule.

Order of evaluation:

1. changed files + operations (GitReader + base tree) – without them nothing can be judged (run status ``error``);
2. local checks: scope, forbidden paths, generated files, changed-file count, deletion policy, secrets, conflict
   markers, in-process syntax, presence/absence/diff/schema/artifact evidence;
3. sandbox commands (serialised per workspace): sandbox syntax batches, compile, lint, command and test evidence;
   the workspace is snapshotted before the first command and every side effect of verifier commands is reverted
   afterwards, so the verified content is exactly what gitops may commit;
4. the implement-step test requirement (21.12) and the mirrored scope/security criteria; report + persistence.

A crash of one check group becomes an ``error`` check of that group – the run still finishes and is persisted.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.acceptance import (
    AbsenceEvidence,
    ArtifactEvidence,
    CommandEvidence,
    DiffEvidence,
    PresenceEvidence,
    SchemaEvidence,
    ScopeEvidence,
    SecurityEvidence,
    TestEvidence,
)
from hermclaw.contracts.scope import Operation
from hermclaw.contracts.verification import VerificationCheck, VerificationReport
from hermclaw.core.config import HermclawConfig, PoliciesConfig
from hermclaw.core.interfaces import CommandExecutor, GitReader, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.tools.gitlocal import LocalGit
from hermclaw.tools.snapshot import DEFAULT_GENERATED_GLOBS, WorkspaceSnapshot, WorkspaceTracker
from hermclaw.tools.workspace import WorkspaceFS
from hermclaw.verifier import checks as generic
from hermclaw.verifier import evidence as acceptance
from hermclaw.verifier.changes import added_content, collect_changes
from hermclaw.verifier.commands import CommandRunner
from hermclaw.verifier.context import VerifyContext, make_check
from hermclaw.verifier.report import build_report, run_status
from hermclaw.verifier.secrets import SecretScanner
from hermclaw.verifier.store import abort_run, finish_run, start_run
from hermclaw.verifier.types import ArtifactLookup, VerificationOutcome, VerificationStep, db_artifact_lookup

log = get_logger(__name__)

SIDE_EFFECT_REASON = "verifier commands must not change the workspace"


class _KeyedLocks:
    """One asyncio lock per workspace; entries are dropped when nobody holds or waits for them."""

    def __init__(self) -> None:
        self._locks: dict[uuid.UUID, tuple[asyncio.Lock, int]] = {}

    @contextlib.asynccontextmanager
    async def hold(self, key: uuid.UUID) -> AsyncIterator[None]:
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)

    def __len__(self) -> int:
        return len(self._locks)


def _deny_all(_path: str, _op: Operation) -> tuple[bool, str]:
    return False, SIDE_EFFECT_REASON


class Verifier:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: HermclawConfig | PoliciesConfig,
        executor: CommandExecutor,
        git: GitReader,
        *,
        artifacts: ArtifactLookup | None = None,
        redactor: Redactor = DEFAULT_REDACTOR,
        scanner: SecretScanner | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.policies = config.policies if isinstance(config, HermclawConfig) else config
        self.executor = executor
        self.git = git
        self.artifacts = artifacts or db_artifact_lookup(sessionmaker)
        self.redactor = redactor
        self.scanner = scanner or SecretScanner()
        self._locks = _KeyedLocks()

    async def verify(
        self,
        step: VerificationStep,
        workspace: WorkspaceHandle,
        *,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID | None = None,
        artifacts: ArtifactLookup | None = None,
    ) -> VerificationReport:
        outcome = await self.run(step, workspace, job_id=job_id, step_id=step_id, attempt_id=attempt_id, artifacts=artifacts)
        return outcome.report

    async def run(
        self,
        step: VerificationStep,
        workspace: WorkspaceHandle,
        *,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID | None = None,
        artifacts: ArtifactLookup | None = None,
    ) -> VerificationOutcome:
        started = time.monotonic()
        run_id = await start_run(self.sessionmaker, job_id=job_id, step_id=step_id, attempt_id=attempt_id, step=step)
        runner = CommandRunner(self.executor, workspace, job_id, step_id, attempt_id, redactor=self.redactor)
        try:
            async with self._locks.hold(workspace.id):
                report = await self._evaluate(
                    step, workspace, runner, job_id=job_id, step_id=step_id, attempt_id=attempt_id, artifacts=artifacts or self.artifacts
                )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(abort_run(self.sessionmaker, run_id=run_id, reason="verification was cancelled"))
            raise
        except Exception as exc:
            log.exception("verifier crashed", extra={"step_key": step.key, "verification_run_id": str(run_id)})
            crash = make_check("verifier", "verifier", "error", f"verifier crashed: {type(exc).__name__}: {exc}", redactor=self.redactor)
            report = build_report([crash], [])
        status = run_status(report)
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            await finish_run(
                self.sessionmaker,
                run_id=run_id,
                job_id=job_id,
                step_id=step_id,
                attempt_id=attempt_id,
                report=report,
                status=status,
                commands=runner.records,
                tests=runner.tests,
                duration_ms=duration_ms,
            )
        except Exception:
            log.exception("could not persist the verification result", extra={"verification_run_id": str(run_id)})
            with contextlib.suppress(Exception):
                await abort_run(self.sessionmaker, run_id=run_id, reason="verification result could not be persisted")
            raise
        return VerificationOutcome(run_id=run_id, report=report, status=status, duration_ms=duration_ms)

    # ------------------------------------------------------------------------------------------- evaluation
    async def _evaluate(
        self,
        step: VerificationStep,
        workspace: WorkspaceHandle,
        runner: CommandRunner,
        *,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID | None,
        artifacts: ArtifactLookup,
    ) -> VerificationReport:
        if not workspace.path.is_dir():
            missing = make_check(
                "workspace", "workspace", "error", f"workspace directory does not exist: {workspace.path}", redactor=self.redactor
            )
            return build_report([missing], [])
        lgit = LocalGit(workspace.path)
        ctx = VerifyContext(
            step=step,
            workspace=workspace,
            policies=self.policies,
            git=self.git,
            lgit=lgit,
            runner=runner,
            job_id=job_id,
            step_id=step_id,
            attempt_id=attempt_id,
            artifacts=artifacts,
            redactor=self.redactor,
        )
        side_effects = _SideEffectGuard(workspace, lgit, self.policies)
        runner.before_first = side_effects.snapshot
        checks: list[VerificationCheck] = []
        try:
            ctx.changes = await collect_changes(workspace, self.git, lgit)
        except Exception as exc:
            checks.append(
                ctx.check("changes", "changed_files", "error", f"cannot determine the changed files: {type(exc).__name__}: {exc}")
            )
            return build_report(checks, [])

        # ---- local checks
        scope_checks = await self._guarded(ctx, "scope", "scope", lambda: _sync(generic.scope_checks(ctx)))
        checks.extend(scope_checks)
        checks.extend(await self._guarded(ctx, "generated", "generated_files", lambda: _sync([generic.generated_check(ctx)])))
        checks.extend(await self._guarded(ctx, "changed_files", "changed_file_count", lambda: _sync([generic.changed_count_check(ctx)])))
        checks.extend(await self._guarded(ctx, "deletions", "deleted_files", lambda: _sync([generic.deletions_check(ctx)])))
        secret_checks: list[VerificationCheck]
        conflict_checks: list[VerificationCheck]
        try:
            added = await added_content(workspace, ctx.changes, self.git, lgit)
        except Exception as exc:
            msg = f"cannot read the added lines of the change: {type(exc).__name__}: {exc}"
            secret_checks = [ctx.check("secrets", "secrets", "error", msg)]
            conflict_checks = [ctx.check("conflicts", "conflict_markers", "error", msg)]
        else:
            secret_checks = await self._guarded(ctx, "secrets", "secrets", lambda: _sync(generic.secret_checks(ctx, added, self.scanner)))
            conflict_checks = await self._guarded(ctx, "conflicts", "conflict_markers", lambda: _sync(generic.conflict_checks(ctx, added)))
        checks.extend(secret_checks)
        checks.extend(conflict_checks)

        criteria: dict[int, VerificationCheck] = {}
        test_outcomes: list[tuple[VerificationCheck, bool | None, bool]] = []
        for index, item in enumerate(step.acceptance):
            if isinstance(item, PresenceEvidence | AbsenceEvidence | DiffEvidence | SchemaEvidence | ArtifactEvidence):
                criteria[index] = await self._criterion(ctx, index, item)

        # ---- checks that may run sandbox commands (serialised; side effects reverted afterwards, also on errors)
        integrity: VerificationCheck | None = None
        try:
            checks.extend(await self._command_phase(ctx, criteria, test_outcomes))
        finally:
            if runner.used:
                integrity = await asyncio.shield(side_effects.restore(ctx))
        if integrity is not None:
            checks.append(integrity)

        # ---- derived checks
        for index, item in enumerate(step.acceptance):
            if isinstance(item, ScopeEvidence):
                criteria[index] = acceptance.mirrored(ctx, index, item, scope_checks)
            elif isinstance(item, SecurityEvidence):
                sources = [*(secret_checks if item.secret_scan else []), *(conflict_checks if item.conflict_markers else [])]
                criteria[index] = acceptance.mirrored(ctx, index, item, sources)
        checks.extend(criteria[i] for i in sorted(criteria))
        checks.extend(
            await self._guarded(ctx, "test_evidence", "test_evidence", lambda: _one(generic.require_tests_check(ctx, test_outcomes)))
        )
        return build_report(checks, ctx.changes.paths)

    async def _command_phase(
        self,
        ctx: VerifyContext,
        criteria: dict[int, VerificationCheck],
        test_outcomes: list[tuple[VerificationCheck, bool | None, bool]],
    ) -> list[VerificationCheck]:
        out: list[VerificationCheck] = []
        out.extend(await self._guarded(ctx, "syntax", "syntax", lambda: generic.syntax_checks(ctx)))
        out.extend(await self._guarded(ctx, "compile", "compile", lambda: generic.compile_checks(ctx)))
        out.extend(await self._guarded(ctx, "lint", "lint", lambda: generic.lint_checks(ctx)))
        for index, item in enumerate(ctx.step.acceptance):
            if isinstance(item, CommandEvidence):
                criteria[index] = await self._criterion(ctx, index, item)
            elif isinstance(item, TestEvidence):
                try:
                    result = await acceptance.run_test_evidence(ctx, index, item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("test evidence crashed", extra={"step_key": ctx.step.key})
                    criteria[index] = ctx.check(
                        acceptance.categorise_test(item), acceptance.label(index, item), "error", f"check crashed: {exc}"
                    )
                    test_outcomes.append((criteria[index], None, False))
                else:
                    criteria[index] = result.check
                    test_outcomes.append((result.check, result.executed, result.ok))

        return out

    async def _criterion(self, ctx: VerifyContext, index: int, item: Any) -> VerificationCheck:
        handlers: dict[type, Callable[[], Awaitable[VerificationCheck]]] = {
            PresenceEvidence: lambda: acceptance.presence(ctx, index, item),
            AbsenceEvidence: lambda: acceptance.absence(ctx, index, item),
            DiffEvidence: lambda: _sync_one(acceptance.diff_evidence(ctx, index, item)),
            SchemaEvidence: lambda: acceptance.schema_evidence(ctx, index, item),
            ArtifactEvidence: lambda: acceptance.artifact_evidence(ctx, index, item),
            CommandEvidence: lambda: acceptance.command_evidence(ctx, index, item),
        }
        try:
            return await handlers[type(item)]()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("acceptance check crashed", extra={"step_key": ctx.step.key, "criterion": item.type})
            return ctx.check(item.type, acceptance.label(index, item), "error", f"check crashed: {type(exc).__name__}: {exc}")

    @staticmethod
    async def _guarded(
        ctx: VerifyContext, check_type: str, name: str, fn: Callable[[], Awaitable[list[VerificationCheck]]]
    ) -> list[VerificationCheck]:
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("verifier check crashed", extra={"check_type": check_type, "step_key": ctx.step.key})
            return [ctx.check(check_type, name, "error", f"{check_type} check crashed: {type(exc).__name__}: {exc}")]


async def _sync(value: list[VerificationCheck]) -> list[VerificationCheck]:
    return value


async def _sync_one(value: VerificationCheck) -> VerificationCheck:
    return value


async def _one(coro: Awaitable[VerificationCheck]) -> list[VerificationCheck]:
    return [await coro]


class _SideEffectGuard:
    """Snapshot before the first verifier command; revert every change verifier commands made afterwards."""

    def __init__(self, workspace: WorkspaceHandle, lgit: LocalGit, policies: PoliciesConfig) -> None:
        self.workspace = workspace
        self.lgit = lgit
        self.policies = policies
        self.tracker: WorkspaceTracker | None = None
        self.before: WorkspaceSnapshot | None = None
        self.error: str | None = None

    async def snapshot(self) -> None:
        try:
            fs = WorkspaceFS(self.workspace.path)
            globs = (*DEFAULT_GENERATED_GLOBS, *self.policies.verifier.generated_file_globs)
            self.tracker = WorkspaceTracker(fs, self.lgit, generated_globs=globs)
            self.before = await self.tracker.snapshot()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("verifier could not snapshot the workspace", extra={"error": self.error})

    async def restore(self, ctx: VerifyContext) -> VerificationCheck:
        if self.tracker is None or self.before is None:
            return ctx.check(
                "side_effects",
                "workspace_integrity",
                "error",
                f"the workspace could not be snapshotted before verifier commands ran ({self.error}); "
                "side effects of the commands cannot be ruled out",
            )
        try:
            outcome = await self.tracker.audit(self.before, _deny_all)
        except Exception as exc:
            return ctx.check(
                "side_effects", "workspace_integrity", "error", f"could not restore the workspace: {type(exc).__name__}: {exc}"
            )
        reverted = [v.to_dict() for v in outcome.violations if v.reverted]
        stuck = [v.to_dict() for v in outcome.violations if not v.reverted]
        evidence: dict[str, Any] = {"reverted": reverted, "cleaned": outcome.cleaned}
        if stuck:
            evidence["not_reverted"] = stuck
            evidence["path"] = str(stuck[0]["path"])
            return ctx.check(
                "side_effects",
                "workspace_integrity",
                "error",
                f"verifier commands changed {len(stuck)} path(s) that could not be restored: {[s['path'] for s in stuck[:5]]}",
                evidence,
            )
        if reverted or outcome.cleaned:
            return ctx.check(
                "side_effects",
                "workspace_integrity",
                "pass",
                f"reverted {len(reverted)} change(s) and removed {len(outcome.cleaned)} generated file(s) left by verifier commands",
                evidence,
                blocking=False,
            )
        return ctx.check(
            "side_effects", "workspace_integrity", "pass", "verifier commands left the workspace unchanged", evidence, blocking=False
        )
