"""Ports the runtime driver needs from components that are wired at service construction time.

Each port is a small protocol so the driver can be tested with real PostgreSQL/git and fake services, and so
concrete components (verifier, …) plug in through thin adapters. Repository intelligence uses the shared
``hermclaw.core.interfaces.RepoContextProvider`` directly.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

from hermclaw.core.interfaces import WorkspaceHandle


@runtime_checkable
class RegressionCheck(Protocol):
    """Re-verifies a workspace after the base branch moved (P23 23.5 regression rerun)."""

    async def rerun(self, job_id: uuid.UUID, workspace: WorkspaceHandle) -> tuple[bool, dict[str, Any]]: ...
