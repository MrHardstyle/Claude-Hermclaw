"""Ports the runtime driver needs from components that are wired at service construction time.

Each port is a small protocol so the driver can be tested with real PostgreSQL/git and fake model/repo services,
and so concrete components (repository intelligence, verifier, …) plug in through thin adapters
(:mod:`hermclaw.runtime.adapters`).
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

from hermclaw.core.interfaces import RepoHit, WorkspaceHandle


@runtime_checkable
class RepoIntel(Protocol):
    """Repository intelligence as seen by the job driver (Bauplan §14)."""

    async def inventory(self, workspace: WorkspaceHandle, *, job_id: uuid.UUID | None = None) -> dict[str, Any]:
        """Deterministic inventory (files, languages, build systems, tests, routes, …) as plain JSON data."""
        ...

    async def context_for(
        self, workspace: WorkspaceHandle, goal: str, *, budget_chars: int, job_id: uuid.UUID | None = None
    ) -> list[RepoHit]:
        """Most relevant snippets for ``goal`` within ``budget_chars`` (fusion ranking)."""
        ...


@runtime_checkable
class RegressionCheck(Protocol):
    """Re-verifies a workspace after the base branch moved (P23 23.5 regression rerun)."""

    async def rerun(self, job_id: uuid.UUID, workspace: WorkspaceHandle) -> tuple[bool, dict[str, Any]]: ...
