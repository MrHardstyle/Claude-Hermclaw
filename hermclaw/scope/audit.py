"""Scope audit of actual workspace changes (Bauplan §17 "Kein stiller Zugriff", P15 15.9).

After (or during) an attempt the runtime checks every real change against the step's *active* scope version with
:class:`~hermclaw.scope.guard.ScopeGuard` – the single path-permission authority. The result is persisted into
``scope_contracts.evidence["audits"]`` of the audited version and every audit with violations emits one
``scope.violation`` event (severity error). Steps without an active scope are audited against a deny-all
contract, so no change can pass silently.

:func:`derive_changes_from_status` maps ``git status --porcelain`` XY codes (``GitReader.status``) to
create / modify / delete operations.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import Operation, normalise_path
from hermclaw.core.config import HermclawConfig, ScopePolicy
from hermclaw.core.interfaces import GitStatusEntry
from hermclaw.core.logging import get_logger
from hermclaw.events.store import append_event
from hermclaw.scope.engine import (
    SOURCE_TYPE,
    contract_from_row,
    current_scope_row,
    deny_all_contract,
    load_step,
    merged_forbidden,
    now_iso,
)
from hermclaw.scope.guard import ScopeGuard

log = get_logger(__name__)

SOURCE_ID = "scope_audit"
_MAX_AUDIT_RECORDS = 20
_MAX_RECORDED_VIOLATIONS = 50


# ============================================================================================= git status mapping
def _unquote(path: str) -> str:
    p = path.strip()
    if len(p) >= 2 and p[0] == p[-1] == '"':
        inner = p[1:-1]
        try:
            return inner.encode("latin-1", errors="backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return inner
    return p


def derive_changes_from_status(entries: Iterable[GitStatusEntry]) -> list[tuple[str, Operation]]:
    """Map porcelain XY status entries to ``(path, operation)`` changes.

    ``??`` -> create; ``!!`` (ignored) -> skipped; ``R``/``C`` with ``old -> new`` -> delete old (rename only) +
    create new; any ``A`` -> create; any ``D`` -> delete; everything else (``M``, ``T``, unmerged ``U``…) -> modify.
    Paths are unquoted and de-duplicated; order is preserved.
    """
    out: list[tuple[str, Operation]] = []

    def add(path: str, op: Operation) -> None:
        item = (_unquote(path), op)
        if item[0] and item not in out:
            out.append(item)

    for entry in entries:
        code = (entry.status or "").ljust(2)[:2]
        x, y = code[0], code[1]
        path = entry.path
        if code == "!!":
            continue
        if code == "??":
            add(path, "create")
            continue
        if x in "RC" or y in "RC":
            if " -> " in path:
                old, new = path.split(" -> ", 1)
                if "R" in (x, y):
                    add(old, "delete")
                add(new, "create")
            else:
                add(path, "create")
            continue
        if "A" in (x, y):
            add(path, "create")
        elif "D" in (x, y):
            add(path, "delete")
        else:
            add(path, "modify")
    return out


# ============================================================================================= report
@dataclass(frozen=True)
class ScopeAuditReport:
    step_id: uuid.UUID
    scope_version: int | None
    scope_status: str | None  # active | unavailable | superseded | None (no scope)
    checked: int
    allowed: list[tuple[str, Operation]] = field(default_factory=list)
    violations: list[dict[str, str]] = field(default_factory=list)
    phase: str = "attempt"

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": str(self.step_id),
            "scope_version": self.scope_version,
            "scope_status": self.scope_status,
            "phase": self.phase,
            "checked": self.checked,
            "ok": self.ok,
            "allowed": [{"path": p, "operation": op} for p, op in self.allowed],
            "violations": self.violations,
        }


# ============================================================================================= auditor
class ScopeAuditor:
    """Audits real changes of a step against its active scope version and records the result."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], config: HermclawConfig) -> None:
        self.sessionmaker = sessionmaker
        self.config = config

    @property
    def policy(self) -> ScopePolicy:
        return self.config.policies.scope

    async def audit_changes(
        self,
        step_id: uuid.UUID,
        changes: Iterable[tuple[str, Operation]],
        *,
        attempt_id: uuid.UUID | None = None,
        phase: str = "attempt",
    ) -> ScopeAuditReport:
        """Check ``changes`` with ScopeGuard, persist the audit into the scope evidence, emit ``scope.violation``."""
        unique: list[tuple[str, Operation]] = []
        for path, op in changes:
            if (path, op) not in unique:
                unique.append((path, op))

        async with self.sessionmaker() as session, session.begin():
            # same lock order as generation/expansion (step, then scope row): never audit a version being superseded
            step = await load_step(session, step_id, for_update=True)
            row = await current_scope_row(session, step, for_update=True)
            if row is not None and row.status == "active":
                contract = contract_from_row(row)
            else:
                forbidden, _ = merged_forbidden(self.policy, [])
                status = row.status if row is not None else "none"
                contract = deny_all_contract(
                    version=row.version if row is not None else 1,
                    forbidden=forbidden,
                    reason=f"no active scope (status: {status})",
                )
            guard = ScopeGuard(contract, self.policy)
            allowed: list[tuple[str, Operation]] = []
            violations: list[dict[str, str]] = []
            for path, op in unique:
                ok, reason = guard.decide(path, op)
                if ok:
                    allowed.append((_display(path), op))
                else:
                    if row is None or row.status != "active":
                        reason = f"{reason} (step has no active scope)"
                    violations.append({"path": _display(path), "operation": op, "reason": reason})

            report = ScopeAuditReport(
                step_id=step_id,
                scope_version=row.version if row is not None else None,
                scope_status=row.status if row is not None else None,
                checked=len(unique),
                allowed=allowed,
                violations=violations,
                phase=phase,
            )
            if row is not None:
                evidence = dict(row.evidence or {})
                audits = list(evidence.get("audits") or [])
                audits.append(
                    {
                        "at": now_iso(),
                        "phase": phase[:64],
                        "attempt_id": str(attempt_id) if attempt_id else None,
                        "checked": report.checked,
                        "ok": report.ok,
                        "violation_count": len(violations),
                        "violations": violations[:_MAX_RECORDED_VIOLATIONS],
                    }
                )
                evidence["audits"] = audits[-_MAX_AUDIT_RECORDS:]
                evidence["last_audit_ok"] = report.ok
                row.evidence = evidence  # reassign: JSONB columns are not mutation-tracked
            if violations:
                await append_event(
                    session,
                    EventType.SCOPE_VIOLATION,
                    source_type=SOURCE_TYPE,
                    source_id=SOURCE_ID,
                    job_id=step.job_id,
                    step_id=step.id,
                    attempt_id=attempt_id,
                    severity=Severity.error,
                    payload={
                        "step_key": step.step_key,
                        "scope_version": report.scope_version,
                        "scope_status": report.scope_status,
                        "phase": phase[:64],
                        "checked": report.checked,
                        "violation_count": len(violations),
                        "violations": violations[:_MAX_RECORDED_VIOLATIONS],
                    },
                )
        if violations:
            log.warning(
                "scope violations detected",
                extra={"step_id": str(step_id), "scope_version": report.scope_version, "violation_count": len(violations)},
            )
        return report

    async def audit_status(
        self,
        step_id: uuid.UUID,
        entries: Iterable[GitStatusEntry],
        *,
        attempt_id: uuid.UUID | None = None,
        phase: str = "attempt",
    ) -> ScopeAuditReport:
        """Convenience: audit ``GitReader.status`` entries directly."""
        return await self.audit_changes(step_id, derive_changes_from_status(entries), attempt_id=attempt_id, phase=phase)


def _display(path: str) -> str:
    try:
        return normalise_path(path)
    except ValueError:
        return str(path)[:300]
