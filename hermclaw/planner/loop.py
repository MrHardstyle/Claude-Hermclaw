"""Bounded structured-output loop with validation-driven repair turns (P14 14.2-14.4, Bauplan §15).

One schema-constrained ``ChatModel.chat`` call, then at most ``max_repair_attempts`` repair turns in total. A
repair turn sends the exact validation error list (schema, semantic or enrichment errors) plus the previous JSON
answer back to the model. The model's reasoning is never part of the conversation: the gateway only returns the
final content, and from that content only the JSON object is echoed (surrounding prose is dropped).

After the budget is exhausted a ``PlannerError`` (``PLANNER_INVALID_OUTPUT``) is raised whose details carry the
complete validation error history. Technical model failures (timeouts, unreachable gateway) are not repaired here;
they propagate as the gateway's ``ModelError`` – the 12B technical fallback is the gateway's job and is surfaced
through ``ChatResult.fallback_used``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from hermclaw.models.protocols import CallContext, ChatMessage, ChatModel, ChatResult
from hermclaw.planner.errors import PlanInvalid, PlannerError
from hermclaw.planner.inputs import PlannerSettings
from hermclaw.planner.parsing import extract_json_object
from hermclaw.planner.prompt import json_candidate, repair_message


@dataclass
class AttemptRecord:
    """Validation outcome of one model answer (persisted as ``plan_versions.validation_errors`` history)."""

    attempt: int
    alias: str
    model: str
    fallback_used: bool
    phase: str | None = None
    errors: list[str] = field(default_factory=list)
    invocation_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "alias": self.alias,
            "model": self.model,
            "fallback_used": self.fallback_used,
            "phase": self.phase,
            "errors": list(self.errors),
            "invocation_id": self.invocation_id,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms,
        }


@dataclass
class LoopOutcome[T]:
    value: T
    result: ChatResult
    attempts: list[AttemptRecord]
    duration_ms: int

    @property
    def repair_attempts(self) -> int:
        return len(self.attempts) - 1

    @property
    def fallback_used(self) -> bool:
        return any(a.fallback_used for a in self.attempts)

    @property
    def validation_history(self) -> list[dict[str, Any]]:
        return [a.to_json() for a in self.attempts if a.errors]


RepairHook = Callable[[AttemptRecord, int], Awaitable[None]]


@dataclass(frozen=True)
class ModelCall:
    alias: str
    json_schema: dict[str, Any]
    max_tokens: int | None
    temperature: float | None
    timeout_seconds: float | None
    purpose: str


async def run_structured_loop[T](
    chat: ChatModel,
    call: ModelCall,
    messages: list[ChatMessage],
    validate: Callable[[dict[str, Any]], T],
    *,
    ctx: CallContext,
    settings: PlannerSettings,
    on_repair: RepairHook | None = None,
) -> LoopOutcome[T]:
    """Call the model, validate, and repair at most ``settings.max_repair_attempts`` times."""
    started = time.monotonic()
    attempts: list[AttemptRecord] = []
    conversation = list(messages)
    max_calls = 1 + max(settings.max_repair_attempts, 0)
    for attempt in range(1, max_calls + 1):
        result = await chat.chat(
            call.alias,
            conversation,
            ctx=ctx,
            max_tokens=call.max_tokens,
            temperature=call.temperature,
            json_schema=call.json_schema,
            timeout_seconds=call.timeout_seconds,
        )
        record = AttemptRecord(
            attempt=attempt,
            alias=result.alias,
            model=result.model,
            fallback_used=result.fallback_used,
            invocation_id=str(result.invocation_id) if result.invocation_id else None,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
        )
        attempts.append(record)
        try:
            data = extract_json_object(result.content, reasoning_chars=result.reasoning_chars)
            value = validate(data)
        except PlanInvalid as invalid:
            record.phase = invalid.phase
            record.errors = invalid.errors[: settings.max_errors_reported] or [f"{invalid.phase}: invalid plan"]
            remaining = max_calls - attempt
            if remaining <= 0:
                break
            if on_repair is not None:
                await on_repair(record, remaining - 1)
            conversation = _repair_conversation(messages, result.content, record.errors, remaining - 1, settings)
            continue
        return LoopOutcome(value=value, result=result, attempts=attempts, duration_ms=int((time.monotonic() - started) * 1000))
    last = attempts[-1]
    raise PlannerError(
        f"{call.purpose}: model output invalid after {len(attempts) - 1} repair attempt(s): {'; '.join(last.errors[:5])}",
        details={
            "purpose": call.purpose,
            "attempts": len(attempts),
            "repair_attempts": len(attempts) - 1,
            "last_errors": list(last.errors),
            "history": [a.to_json() for a in attempts],
            "fallback_used": any(a.fallback_used for a in attempts),
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )


def _repair_conversation(
    base: list[ChatMessage], content: str, errors: list[str], remaining_after: int, settings: PlannerSettings
) -> list[ChatMessage]:
    """Base prompt + the previous JSON answer (only the JSON part, never prose) + the exact error list."""
    conversation = list(base)
    candidate = json_candidate(content or "")
    if candidate is not None and len(candidate) <= settings.max_echo_chars:
        conversation.append(ChatMessage(role="assistant", content=candidate))
    conversation.append(ChatMessage(role="user", content=repair_message(errors, remaining_after)))
    return conversation
