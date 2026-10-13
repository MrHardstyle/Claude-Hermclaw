"""Operations tools (Bauplan §29–31) behind the tool protocol: SSH, database and container/proxy tools.

The tool engine validates the arguments, checks the capability gate (``OPS_TOOLS_BY_CAPABILITY``), audits every
workspace side effect against the scope (exactly like ``run_command``) and records the call; the *provider* does the
work with its own policy (host allow-list, command classification, approval, read-only enforcement, sandbox DBs).
Providers come from the runtime (``hermclaw.runtime.services``): ``hermclaw.sshtools``, ``hermclaw.dbtools``,
``hermclaw.infratools`` adapters combined by :class:`CompositeOpsProvider`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from hermclaw.contracts.tools import ToolName
from hermclaw.core.interfaces import WorkspaceHandle


@dataclass(frozen=True)
class OpsCall:
    tool: ToolName
    args: BaseModel
    workspace: WorkspaceHandle
    capability: str | None
    job_id: uuid.UUID | None
    step_id: uuid.UUID | None
    attempt_id: uuid.UUID | None
    turn: int


@dataclass
class OpsResult:
    ok: bool
    output: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


class OpsToolProvider(Protocol):
    def provides(self, tool: ToolName) -> bool: ...

    async def call(self, call: OpsCall) -> OpsResult: ...


class CompositeOpsProvider:
    """Dispatches each operations tool to the first provider that offers it."""

    def __init__(self, providers: Iterable[OpsToolProvider]) -> None:
        self.providers = list(providers)

    def provides(self, tool: ToolName) -> bool:
        return any(p.provides(tool) for p in self.providers)

    async def call(self, call: OpsCall) -> OpsResult:
        for p in self.providers:
            if p.provides(call.tool):
                return await p.call(call)
        return OpsResult(False, f"no provider for {call.tool.value}", error_code="OPS_TOOL_UNAVAILABLE")


__all__ = ["CompositeOpsProvider", "OpsCall", "OpsResult", "OpsToolProvider"]
