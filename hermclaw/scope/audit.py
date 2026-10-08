"""Scope audit of actual workspace changes (Bauplan §17 "Kein stiller Zugriff", P15 15.9).

After (or during) an attempt the runtime checks every real change against the step's *active* scope version with
:class:`~hermclaw.scope.guard.ScopeGuard` – the single path-permission authority. The result is persisted into
``scope_contracts.evidence["audits"]`` of the audited version and every audit with violations emits one
``scope.violation`` event (severity error). Steps without an active scope are audited against a deny-all
contract, so no change can pass silently.

:func:`derive_changes_from_status` maps ``git status --porcelain`` XY codes (``GitReader.status``) to
create / modify / delete operations. A staged rename whose origin the status entry does not carry is reported by
:func:`unresolved_renames`; :meth:`ScopeAuditor.audit_status` treats it as a violation (the deletion of the source
cannot be audited – fail closed).

A contract-level ``delete`` operation only covers the paths the scope version designates for deletion
(:func:`~hermclaw.scope.engine.designated_deletes`): deleting any other target is a violation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.contracts.scope import Operation, ScopeContract, normalise_path
from hermclaw.core.config import HermclawConfig, ScopePolicy
from hermclaw.core.interfaces import GitStatusEntry
from hermclaw.core.logging import get_logger
from hermclaw.events.store import append_event
from hermclaw.scope.engine import (
    SOURCE_TYPE,
    contract_from_row,
    current_scope_row,
    deny_all_contract,
    designated_deletes,
    glob_escape,
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

    ``??`` -> create; ``!!`` (ignored) -> skipped; ``R``/``C`` -> create new plus, for renames, delete of the origin
    (``entry.orig_path`` when the producer provides it, or the ``old -> new`` text form); any ``A`` -> create; any
    ``D`` -> delete; everything else (``M``, ``T``, unmerged ``U``…) -> modify. Paths are unquoted and
    de-duplicated; order is preserved. Renames without a known origin are listed by :func:`unresolved_renames`.
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
            old, new = _rename_parts(entry)
            if old is not None and "R" in (x, y):
                add(old, "delete")
            add(new, "create")
            continue
        if "A" in (x, y):
            add(path, "create")
        elif "D" in (x, y):
            add(path, "delete")
        else:
            add(path, "modify")
    return out


def _rename_parts(entry: GitStatusEntry) -> tuple[str | None, str]:
    orig = getattr(entry, "orig_path", None)  # forward compatible with producers that keep the origin
    if isinstance(orig, str) and orig:
        return orig, entry.path
    if " -> " in entry.path:
        old, new = entry.path.split(" -> ", 1)
        return old, new
    return None, entry.path


def unresolved_renames(entries: Iterable[GitStatusEntry]) -> list[str]:
    """New paths of staged renames whose origin is unknown – their source deletion cannot be audited."""
    out: list[str] = []
    for entry in entries:
        code = (entry.status or "").ljust(2)[:2]
        if "R" in code and _rename_parts(entry)[0] is None:
            path = _unquote(entry.path)
            if path and path not in out:
                out.append(path)
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
        unauditable: Iterable[tuple[str, str]] = (),
    ) -> ScopeAuditReport:
        """Check ``changes`` with ScopeGuard, persist the audit into the scope evidence, emit ``scope.violation``.

        ``unauditable`` lists ``(path, reason)`` changes the caller could not map to a checkable operation; each is
        a violation (fail closed).
        """
        unaudited = list(dict.fromkeys(unauditable))
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
            delete_guard = self._delete_guard(contract, row.evidence if row is not None and row.status == "active" else None)
            allowed: list[tuple[str, Operation]] = []
            violations: list[dict[str, str]] = []
            for path, op in unique:
                ok, reason = guard.decide(path, op)
                if ok and op == "delete" and delete_guard is not None and not delete_guard.allowed(path, "delete"):
                    ok = False
                    reason = (
                        f"deleting '{_display(path)}' is not designated by scope v{contract.version} "
                        "(only acceptance absence evidence or an explicit delete constraint designates deletions)"
                    )
                if ok:
                    allowed.append((_display(path), op))
                else:
                    if row is None or row.status != "active":
                        reason = f"{reason} (step has no active scope)"
                    violations.append({"path": _display(path), "operation": op, "reason": reason})
            for path, why in unaudited:
                violations.append({"path": _display(path), "operation": "delete", "reason": why[:500]})

            report = ScopeAuditReport(
                step_id=step_id,
                scope_version=row.version if row is not None else None,
                scope_status=row.status if row is not None else None,
                checked=len(unique) + len(unaudited),
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
        """Audit ``GitReader.status`` entries; renames with an unknown origin are violations (fail closed)."""
        listed = list(entries)
        unknown = [
            (p, "staged rename without a known origin: the deletion of its source path cannot be audited")
            for p in unresolved_renames(listed)
        ]
        return await self.audit_changes(
            step_id, derive_changes_from_status(listed), attempt_id=attempt_id, phase=phase, unauditable=unknown
        )

    def _delete_guard(self, contract: ScopeContract, evidence: dict[str, Any] | None) -> ScopeGuard | None:
        if "delete" not in contract.allowed_operations:
            return None
        deletes = designated_deletes(evidence)
        if deletes is None:
            return None  # versions without a designation (e.g. written by other components): ScopeGuard alone decides
        designated = ScopeContract(
            source=contract.source,
            version=contract.version,
            target_paths=[glob_escape(p) for p in deletes],
            forbidden_paths=contract.forbidden_paths,
            allowed_operations=["delete"],
        )
        return ScopeGuard(designated, self.policy)


def _display(path: str) -> str:
    try:
        return normalise_path(path)
    except ValueError:
        return str(path)[:300]
