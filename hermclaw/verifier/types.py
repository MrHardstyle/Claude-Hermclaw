"""Value types of the deterministic verifier (Bauplan §21, P21).

The verifier knows nothing about the task: it receives a :class:`VerificationStep` (key, kind, machine-checkable
acceptance evidence, the runtime scope contract and the network flag) and evaluates generic checks plus that
evidence against a workspace.
"""

from __future__ import annotations

import fnmatch
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.acceptance import AcceptanceCriterion
from hermclaw.contracts.common import StepKind
from hermclaw.contracts.scope import ScopeContract
from hermclaw.contracts.verification import VerificationReport
from hermclaw.persistence.models import Artifact, ScopeContractRow, Step

Operation = Literal["create", "modify", "delete"]

_ACCEPTANCE = TypeAdapter(list[AcceptanceCriterion])


def parse_acceptance(items: Sequence[AcceptanceCriterion | Mapping[str, Any]]) -> tuple[AcceptanceCriterion, ...]:
    """Validate raw acceptance items (e.g. ``steps.acceptance`` JSON) into typed evidence objects."""
    raw = [i.model_dump(mode="json") if isinstance(i, BaseModel) else dict(i) for i in items]
    return tuple(_ACCEPTANCE.validate_python(raw))


@dataclass(frozen=True)
class VerificationStep:
    """Everything the verifier needs to know about the step being verified."""

    key: str
    kind: str
    acceptance: tuple[AcceptanceCriterion, ...] = ()
    scope: ScopeContract | None = None
    network: bool = False
    title: str = ""

    @property
    def is_implement(self) -> bool:
        return self.kind == StepKind.implement.value

    @classmethod
    def build(
        cls,
        *,
        key: str,
        kind: str | StepKind,
        acceptance: Sequence[AcceptanceCriterion | Mapping[str, Any]] = (),
        scope: ScopeContract | Mapping[str, Any] | None = None,
        network: bool = False,
        title: str = "",
    ) -> VerificationStep:
        contract = scope if isinstance(scope, ScopeContract) or scope is None else ScopeContract.model_validate(dict(scope))
        return cls(
            key=key,
            kind=kind.value if isinstance(kind, StepKind) else str(kind),
            acceptance=parse_acceptance(acceptance),
            scope=contract,
            network=network,
            title=title,
        )

    @classmethod
    def from_row(cls, step: Step, scope: ScopeContract | Mapping[str, Any] | None) -> VerificationStep:
        return cls.build(
            key=step.step_key,
            kind=step.kind,
            acceptance=list(step.acceptance or []),
            scope=scope,
            network=bool(step.network),
            title=step.title,
        )


async def load_step(session: AsyncSession, step_id: uuid.UUID) -> VerificationStep:
    """Build a :class:`VerificationStep` from the ``steps`` row and its newest *active* scope contract."""
    step = await session.get(Step, step_id)
    if step is None:
        raise LookupError(f"step {step_id} does not exist")
    row = (
        await session.execute(
            select(ScopeContractRow)
            .where(ScopeContractRow.step_id == step_id, ScopeContractRow.status == "active")
            .order_by(ScopeContractRow.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return VerificationStep.from_row(step, row.contract if row is not None else None)


@dataclass(frozen=True)
class FileChange:
    """One changed path relative to the workspace base commit."""

    path: str
    operation: Operation
    status: str = ""  # porcelain XY code when the path appears in ``git status``
    orig_path: str | None = None


@dataclass
class ChangeSet:
    changes: list[FileChange] = field(default_factory=list)
    invalid_paths: list[str] = field(default_factory=list)  # paths git reported that are not repository-relative
    base_known: bool = False  # operations judged against the base tree (otherwise: status codes)

    @property
    def paths(self) -> list[str]:
        return sorted({c.path for c in self.changes})

    @property
    def existing(self) -> list[FileChange]:
        return [c for c in self.changes if c.operation != "delete"]

    @property
    def deleted(self) -> list[FileChange]:
        return [c for c in self.changes if c.operation == "delete"]

    def operations(self) -> list[tuple[str, Operation]]:
        return [(c.path, c.operation) for c in self.changes]


@dataclass(frozen=True)
class AddedLine:
    line: int
    text: str


@dataclass
class AddedContent:
    """Lines added by the change, per file (the input of the secret and conflict-marker scans)."""

    lines: dict[str, list[AddedLine]] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)  # path -> reason (binary, symlink, unreadable …)
    truncated: list[str] = field(default_factory=list)
    source: str = "local-git"  # local-git | git-reader


@dataclass(frozen=True)
class ArtifactRecord:
    name: str
    kind: str
    path: str = ""
    size_bytes: int = 0
    sha256: str = ""

    def matches(self, name_glob: str) -> bool:
        return fnmatch.fnmatchcase(self.name, name_glob or "*")


ArtifactLookup = Callable[[uuid.UUID, uuid.UUID, str], Awaitable[Sequence[ArtifactRecord]]]
"""``(job_id, step_id, kind) -> artifacts`` – how artifact evidence finds the step's artifacts."""


def db_artifact_lookup(sessionmaker: async_sessionmaker[AsyncSession]) -> ArtifactLookup:
    """Default lookup: ``artifacts`` rows of the job + step with the requested kind."""

    async def lookup(job_id: uuid.UUID, step_id: uuid.UUID, kind: str) -> Sequence[ArtifactRecord]:
        async with sessionmaker() as session:
            rows = (
                await session.execute(
                    select(Artifact)
                    .where(Artifact.job_id == job_id, Artifact.step_id == step_id, Artifact.kind == kind)
                    .order_by(Artifact.created_at, Artifact.id)
                )
            ).scalars()
            return [ArtifactRecord(name=r.name, kind=r.kind, path=r.path, size_bytes=int(r.size_bytes or 0), sha256=r.sha256) for r in rows]

    return lookup


@dataclass(frozen=True)
class VerificationOutcome:
    """Result of :meth:`hermclaw.verifier.Verifier.run`: the persisted run id (needed by ``commit_verified``) + report."""

    run_id: uuid.UUID
    report: VerificationReport
    status: str  # passed | failed | error
    duration_ms: int
