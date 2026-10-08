"""Shared model-access interfaces (used by planner, coder, review, research, repo intelligence).

Implemented by ``hermclaw.models.gateway`` (LiteLLM). Tests use fakes that satisfy these protocols.
Reasoning/thinking text is NEVER returned to callers – only the final content.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class CallContext:
    """Traceability for model invocations (persisted in model_invocations + events)."""

    purpose: str  # planner|replan|coder_turn|review|research_queries|research_claims|research_synthesis|triage|embedding
    job_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None


@dataclass
class ChatMessage:
    role: str  # system|user|assistant
    content: str


@dataclass
class ChatResult:
    content: str
    alias: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    finish_reason: str | None = None
    reasoning_chars: int = 0  # length only, content discarded
    invocation_id: uuid.UUID | None = None
    fallback_used: bool = False
    raw_usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class StructuredResult[T: BaseModel]:
    value: T
    result: ChatResult
    repair_attempts: int = 0


@runtime_checkable
class ChatModel(Protocol):
    async def chat(
        self,
        alias: str,
        messages: list[ChatMessage],
        *,
        ctx: CallContext,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> ChatResult: ...

    async def structured(
        self,
        alias: str,
        messages: list[ChatMessage],
        schema: type[T],
        *,
        ctx: CallContext,
        max_repairs: int = 2,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
    ) -> StructuredResult[T]:
        """Validated structured output: JSON-schema constrained call + Pydantic validation + ≤max_repairs repairs.
        Raises hermclaw.core.errors.ModelOutputInvalid after the repair budget is exhausted."""
        ...


@runtime_checkable
class EmbeddingModel(Protocol):
    dimensions: int
    model_name: str

    async def embed(self, texts: list[str], *, ctx: CallContext) -> list[list[float]]: ...
