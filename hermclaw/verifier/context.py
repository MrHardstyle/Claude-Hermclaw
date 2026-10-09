"""Per-run state shared by the verifier checks."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermclaw.contracts.scope import ScopeContract
from hermclaw.contracts.verification import CheckStatus, VerificationCheck
from hermclaw.core.config import PoliciesConfig
from hermclaw.core.interfaces import GitReader, WorkspaceHandle
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.scope.guard import ScopeGuard
from hermclaw.tools.gitlocal import LocalGit
from hermclaw.verifier.changes import list_workspace_files
from hermclaw.verifier.commands import CommandRunner
from hermclaw.verifier.text import pg_safe
from hermclaw.verifier.types import ArtifactLookup, ChangeSet, VerificationStep

MAX_MESSAGE_CHARS = 4000
MAX_EVIDENCE_LIST = 50
MAX_EVIDENCE_STR = 4000


def _bound(value: Any, depth: int = 0) -> Any:
    """Keep evidence small: long strings clipped, long lists capped (with a count of what was left out)."""
    if isinstance(value, str):
        value = pg_safe(value)
        return value if len(value) <= MAX_EVIDENCE_STR else value[:MAX_EVIDENCE_STR] + f"…[{len(value) - MAX_EVIDENCE_STR} chars]"
    if depth > 6:
        return str(value)[:200]
    if isinstance(value, dict):
        return {pg_safe(str(k)): _bound(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        items = list(value)
        out = [_bound(v, depth + 1) for v in items[:MAX_EVIDENCE_LIST]]
        if len(items) > MAX_EVIDENCE_LIST:
            out.append(f"…[{len(items) - MAX_EVIDENCE_LIST} more]")
        return out
    return value


def make_check(
    check_type: str,
    name: str,
    status: CheckStatus,
    message: str = "",
    evidence: dict[str, Any] | None = None,
    *,
    blocking: bool = True,
    redactor: Redactor = DEFAULT_REDACTOR,
) -> VerificationCheck:
    msg = pg_safe(redactor.text(message))
    if len(msg) > MAX_MESSAGE_CHARS:
        msg = msg[:MAX_MESSAGE_CHARS] + "…"
    name = pg_safe(redactor.text(name))
    clipped_name = name if len(name) <= 300 else name[:299] + "…"
    return VerificationCheck(
        check_type=check_type[:32],
        name=clipped_name,
        status=status,
        message=msg,
        evidence=redactor.obj(_bound(evidence or {})),
        blocking=blocking,
    )


@dataclass
class VerifyContext:
    step: VerificationStep
    workspace: WorkspaceHandle
    policies: PoliciesConfig
    git: GitReader
    lgit: LocalGit
    runner: CommandRunner
    job_id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID | None
    artifacts: ArtifactLookup
    changes: ChangeSet = field(default_factory=ChangeSet)
    redactor: Redactor = DEFAULT_REDACTOR
    _files: list[str] | None = None

    @property
    def root(self) -> Path:
        return self.workspace.path

    @property
    def guard(self) -> ScopeGuard:
        return ScopeGuard(self.step.scope or ScopeContract(), self.policies.scope)

    async def files(self) -> list[str]:
        """Workspace inventory (tracked + untracked-not-ignored, existing), computed once per run."""
        if self._files is None:
            self._files = await list_workspace_files(self.workspace, self.lgit)
        return self._files

    def check(
        self,
        check_type: str,
        name: str,
        status: CheckStatus,
        message: str = "",
        evidence: dict[str, Any] | None = None,
        *,
        blocking: bool = True,
    ) -> VerificationCheck:
        return make_check(check_type, name, status, message, evidence, blocking=blocking, redactor=self.redactor)
