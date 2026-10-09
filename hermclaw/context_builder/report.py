"""Context telemetry (step 16.9): what went into a turn's context, what was cut and why.

The report contains sizes, flags and item labels (paths, line ranges, tool names) only – never file contents,
failure text or model output – so it can be stored as an event payload. ``to_event_payload()`` builds the
payload; the caller decides under which event type it is recorded (typically together with
``model.invocation.started`` of the coder turn). :func:`record_context_report` is a convenience for that.

Payload keys deliberately avoid the substring ``token``: the shared redactor (``hermclaw.core.redaction``) masks
every value whose key contains it, which would erase the token counters.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.models.protocols import ChatMessage

PAYLOAD_MAX_DROPPED = 40
PAYLOAD_MAX_WARNINGS = 20


@dataclass(frozen=True)
class SectionReport:
    name: str
    present: bool
    budget_tokens: int
    estimated_tokens: int
    chars: int
    truncated: bool
    items_included: int = 0
    items_dropped: int = 0
    omitted_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        # NOTE: payload keys avoid the substring "token" – the shared redactor masks such keys (see module doc)
        return {
            "name": self.name,
            "present": self.present,
            "budget": self.budget_tokens,
            "estimated": self.estimated_tokens,
            "chars": self.chars,
            "truncated": self.truncated,
            "items_included": self.items_included,
            "items_dropped": self.items_dropped,
            "omitted_reason": self.omitted_reason,
        }


@dataclass(frozen=True)
class DroppedItem:
    section: str
    item: str  # label only (path:range, tool name, file name) – never content
    reason: str  # budget | limit | duplicate | duplicate_content | contained | empty | excluded | invalid_path | read_failed


@dataclass
class ContextReport:
    turn: int
    max_turns: int
    context_tokens: int
    max_output_tokens: int
    safety_margin_tokens: int
    total_budget_tokens: int
    framing_tokens: int
    estimated_prompt_tokens: int
    redistributed_tokens: int
    sections: list[SectionReport]
    dropped: list[DroppedItem] = field(default_factory=list)
    merged_snippets: int = 0
    warnings: list[str] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def truncated(self) -> bool:
        return any(s.truncated for s in self.sections)

    def section(self, name: str) -> SectionReport:
        for s in self.sections:
            if s.name == name:
                return s
        raise KeyError(name)

    def to_event_payload(self) -> dict[str, Any]:
        """Compact, JSON-safe telemetry payload (bounded lists, free text redacted). All sizes are in tokens."""
        red = DEFAULT_REDACTOR.text
        dropped = [{"section": d.section, "item": red(d.item), "reason": d.reason} for d in self.dropped[:PAYLOAD_MAX_DROPPED]]
        return {
            "kind": "context_report",
            "unit": "tokens",
            "turn": self.turn,
            "max_turns": self.max_turns,
            "budget": {
                "context_window": self.context_tokens,
                "max_output": self.max_output_tokens,
                "safety_margin": self.safety_margin_tokens,
                "total": self.total_budget_tokens,
                "framing": self.framing_tokens,
                "redistributed": self.redistributed_tokens,
            },
            "estimated_prompt": self.estimated_prompt_tokens,
            "truncated": self.truncated,
            "sections": [s.as_dict() for s in self.sections],
            "dropped": dropped,
            "dropped_total": len(self.dropped),
            "merged_snippets": self.merged_snippets,
            "warnings": [red(w) for w in self.warnings[:PAYLOAD_MAX_WARNINGS]],
            "fingerprint": self.fingerprint,
        }


@dataclass
class BuiltContext:
    messages: list[ChatMessage]
    report: ContextReport


async def record_context_report(
    session: AsyncSession,
    report: ContextReport,
    *,
    event_type: str,
    job_id: uuid.UUID | None = None,
    step_id: uuid.UUID | None = None,
    attempt_id: uuid.UUID | None = None,
    source_id: str | None = None,
) -> None:
    """Append the report as an event payload in the caller's transaction (the caller picks the event type)."""
    await append_event(
        session,
        event_type,
        source_type="context_builder",
        source_id=source_id,
        job_id=job_id,
        step_id=step_id,
        attempt_id=attempt_id,
        payload={"context": report.to_event_payload()},
    )
