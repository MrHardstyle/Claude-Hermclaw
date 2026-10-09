"""Per-attempt context of the tool engine: permissions, runtime callbacks and call identity (P17).

- :class:`ToolPermissions` – what the current step may do (tool allow-list, destructive commands, network, timeouts,
  turn budget). Derived from the step kind / capability with :meth:`ToolPermissions.for_step`.
- :class:`ToolCallbacks` – the runtime services behind the request tools (research, scope expansion, replan). The
  engine only validates and forwards; deciding is the runtime's job (Bauplan §17: the worker never widens its scope).
- :class:`NullCallbacks` – refuses every request (used for steps without those services).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from hermclaw.contracts.common import MUTATING_STEP_KINDS
from hermclaw.contracts.scope import ScopeContract, ScopeExpansionRequest
from hermclaw.contracts.tools import MUTATING_TOOLS, ToolName
from hermclaw.core.config import CapabilityConfig
from hermclaw.tools import errors as E
from hermclaw.tools.errors import ToolError

ALL_TOOLS: frozenset[ToolName] = frozenset(ToolName)
READ_ONLY_TOOLS: frozenset[ToolName] = ALL_TOOLS - MUTATING_TOOLS


@dataclass(frozen=True)
class ToolPermissions:
    """Execution permissions of one step attempt.

    ``allowed_tools=None`` allows every tool. ``max_turns=None`` / ``max_command_timeout_seconds=None`` fall back to
    ``policies.coder.max_turns`` / ``policies.sandbox.default_timeout_seconds``.
    """

    allowed_tools: frozenset[ToolName] | None = None
    allow_destructive_commands: bool = False
    network: bool = False
    image: str | None = None
    max_command_timeout_seconds: int | None = None
    max_turns: int | None = None
    max_research_requests: int = 3
    max_scope_requests: int = 3

    def allows(self, tool: ToolName) -> bool:
        return self.allowed_tools is None or tool in self.allowed_tools

    @classmethod
    def read_only(cls, **overrides: Any) -> ToolPermissions:
        return cls(allowed_tools=READ_ONLY_TOOLS, **overrides)

    @classmethod
    def for_step(
        cls,
        *,
        kind: str,
        network: bool = False,
        turn_budget: int | None = None,
        capability: CapabilityConfig | None = None,
        image: str | None = None,
        allow_destructive_commands: bool | None = None,
    ) -> ToolPermissions:
        """Permissions for a step: non-mutating step kinds never get write tools.

        Destructive commands are only allowed when the capability explicitly opts in
        (``allow_destructive_commands`` on the capability config) or the caller passes it explicitly.
        """
        mutating = kind in {k.value for k in MUTATING_STEP_KINDS}
        destructive = allow_destructive_commands
        if destructive is None:
            destructive = bool(getattr(capability, "allow_destructive_commands", False))
        return cls(
            allowed_tools=None if mutating else READ_ONLY_TOOLS,
            allow_destructive_commands=destructive,
            network=bool(network or (capability.network if capability is not None else False)),
            image=image,
            max_turns=turn_budget,
        )


@dataclass(frozen=True)
class ScopeExpansionOutcome:
    """Runtime answer to ``request_scope_expansion``. ``contract`` is the new active scope version (if any)."""

    granted: bool
    message: str
    contract: ScopeContract | None = None
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ToolCallbacks(Protocol):
    async def on_research(self, question: str) -> str:
        """Run (or enqueue) research; return a compact summary or a ticket reference."""
        ...

    async def on_scope_expansion(self, request: ScopeExpansionRequest) -> ScopeExpansionOutcome:
        """Validate the request (repository intelligence / replanner) and return the decision."""
        ...

    async def on_replan(self, reason: str) -> None:
        """Record that the step asks for a replan (the runtime stops the attempt afterwards)."""
        ...


class NullCallbacks:
    """Refuses research, scope expansion and replanning."""

    async def on_research(self, question: str) -> str:
        raise ToolError(E.RESEARCH_FAILED, "research is not available for this step")

    async def on_scope_expansion(self, request: ScopeExpansionRequest) -> ScopeExpansionOutcome:
        return ScopeExpansionOutcome(False, "scope expansion is not available for this step; block the step if the scope is insufficient")

    async def on_replan(self, reason: str) -> None:
        raise ToolError(E.REPLAN_FAILED, "replanning is not available for this step; use block_step")


@dataclass(frozen=True)
class CallContext:
    job_id: uuid.UUID | None
    step_id: uuid.UUID | None
    attempt_id: uuid.UUID | None
    turn: int
    tool_call_id: uuid.UUID
