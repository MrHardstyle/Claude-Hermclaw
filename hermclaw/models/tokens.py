"""Conservative token estimation and context-window validation (P08 8.9, DECISIONS D-008).

No tokenizer is downloaded on the orchestrator: a token is estimated as 3.2 characters (conservative for code and
German/English prose with the Gemma/Qwen tokenizers). Exact counts come back from Ollama (``prompt_eval_count`` →
``usage.prompt_tokens``) and are stored per invocation so the ratio can be monitored (see
``hermclaw.models.health.invocation_metrics``).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from hermclaw.core.config import ModelProfileConfig
from hermclaw.core.errors import ValidationFailed
from hermclaw.models.protocols import ChatMessage

CHARS_PER_TOKEN = 3.2
#: Chat-template overhead per message (role markers, separators) – conservative upper bound for Gemma/Qwen templates.
MESSAGE_OVERHEAD_TOKENS = 6
#: Tokens that prime the assistant answer.
REPLY_PRIMING_TOKENS = 4
CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for a text (``ceil(len / 3.2)``)."""
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_messages_tokens(messages: Iterable[ChatMessage], *, extra_texts: Sequence[str] = ()) -> int:
    """Estimate the prompt size of a chat request including per-message template overhead.

    ``extra_texts`` covers prompt material that is not part of ``messages`` (e.g. tool definitions)."""
    total = REPLY_PRIMING_TOKENS
    for message in messages:
        total += MESSAGE_OVERHEAD_TOKENS + estimate_tokens(message.role) + estimate_tokens(message.content)
    for extra in extra_texts:
        total += estimate_tokens(extra)
    return total


def estimate_json_tokens(value: Any) -> int:
    """Estimate for a JSON-serialisable value (compact serialisation)."""
    return estimate_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


@dataclass(frozen=True)
class ContextBudget:
    """Result of a context check. ``fits`` is ``prompt + max_output + reserve <= context``."""

    alias: str
    context_tokens: int
    prompt_tokens: int
    max_output_tokens: int
    reserve_tokens: int = 0

    @property
    def required_tokens(self) -> int:
        return self.prompt_tokens + self.max_output_tokens + self.reserve_tokens

    @property
    def remaining_tokens(self) -> int:
        return self.context_tokens - self.required_tokens

    @property
    def fits(self) -> bool:
        return self.required_tokens <= self.context_tokens

    def as_dict(self) -> dict[str, int | str | bool]:
        return {
            "alias": self.alias,
            "context_tokens": self.context_tokens,
            "estimated_prompt_tokens": self.prompt_tokens,
            "max_output_tokens": self.max_output_tokens,
            "reserve_tokens": self.reserve_tokens,
            "required_tokens": self.required_tokens,
            "remaining_tokens": self.remaining_tokens,
            "fits": self.fits,
        }


def context_budget(
    profile: ModelProfileConfig,
    messages: Iterable[ChatMessage],
    *,
    max_tokens: int | None = None,
    reserve_tokens: int = 0,
    extra_texts: Sequence[str] = (),
) -> ContextBudget:
    """Compute (without raising) how a request fits into ``profile.context_tokens``."""
    out = profile.max_output_tokens if max_tokens is None else max_tokens
    if out < 0 or reserve_tokens < 0:
        raise ValidationFailed("max_tokens and reserve_tokens must not be negative", code="INVALID_TOKEN_BUDGET")
    return ContextBudget(
        alias=profile.alias,
        context_tokens=profile.context_tokens,
        prompt_tokens=estimate_messages_tokens(messages, extra_texts=extra_texts),
        max_output_tokens=out,
        reserve_tokens=reserve_tokens,
    )


def validate_context(
    profile: ModelProfileConfig,
    messages: Iterable[ChatMessage],
    *,
    max_tokens: int | None = None,
    reserve_tokens: int = 0,
    extra_texts: Sequence[str] = (),
) -> ContextBudget:
    """Raise ``ValidationFailed(code=CONTEXT_OVERFLOW)`` if estimated prompt + max_tokens (+ reserve) exceeds the
    profile's context window; otherwise return the budget. Never truncates silently – shrinking the prompt is the
    context builder's job."""
    budget = context_budget(profile, messages, max_tokens=max_tokens, reserve_tokens=reserve_tokens, extra_texts=extra_texts)
    if not budget.fits:
        raise ValidationFailed(
            f"request for '{profile.alias}' needs ~{budget.required_tokens} tokens "
            f"(prompt ~{budget.prompt_tokens} + output {budget.max_output_tokens} + reserve {budget.reserve_tokens}) "
            f"but the context window is {budget.context_tokens}",
            code=CONTEXT_OVERFLOW,
            details=dict(budget.as_dict()),
        )
    return budget


def max_prompt_chars(profile: ModelProfileConfig, *, max_tokens: int | None = None, reserve_tokens: int = 0) -> int:
    """Upper bound of prompt characters that still fit (helper for context builders)."""
    out = profile.max_output_tokens if max_tokens is None else max_tokens
    free = profile.context_tokens - out - reserve_tokens - REPLY_PRIMING_TOKENS
    return max(0, math.floor(free * CHARS_PER_TOKEN))
