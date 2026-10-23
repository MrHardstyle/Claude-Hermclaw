"""Lessons from fixed mistakes (D-025): after a job, one model call per fix turns what went wrong and the change that
fixed it into a general lesson for later jobs.

A fix is either a correction attempt that passed the checks (its correction input is the failure) or, inside any
attempt, a red run of a test or command followed by edits and the same run green again. The model writes a lesson only
when the mistake can recur in other projects with the same language, library or tool – a requirement of this task, a
typo or a lint fix is no lesson. Why: every active lesson so far was written by the operator after reading a failed job
(an environment variable read at import time, an import inside a branch, Pester mocks of Windows cmdlets); the coder
models repeat such mistakes across jobs, and the knowledge only grew when someone wrote it down.

A lesson starts as a candidate. The same lesson from another job (a close embedding) counts that job; seen in
``promote_after`` jobs it becomes active and reaches the models. The operator reads, edits, activates or retires every
lesson on the "Wissen" page; a retired one is never written again.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.models.protocols import CallContext, ChatMessage, ChatModel
from hermclaw.tools import errors as E

LESSON_PURPOSE = "lesson"  # model_invocations.purpose: learning after the job, not the job's own work
EDIT_TOOLS = frozenset({"write_file", "replace_text", "apply_patch"})
RUN_TOOLS = frozenset({"run_test", "run_command"})
_MAX_GOAL = 800
_MAX_FAILURE = 2500
_MAX_EDIT = 900
_MAX_EDITS = 4500

SYSTEM_PROMPT = """You turn one fixed mistake of a coding agent into a lesson for its future tasks.
You get the step goal, the FAILURE (check results or the output of a red test or command) and the CHANGES that fixed it.
Set general true only when the mistake is one a coder can make again in another project with the same language,
library or tool: a wrong assumption about how it behaves, a testing pitfall, a design that breaks a check.
Set general false when the fix only adds a requirement of this task, corrects a typo, a wrong expected value or a name,
or only satisfies lint or formatting.
Write the lesson without the names of this project (its files, functions, data, task): title states the rule in one
line, mistake says what goes wrong and how it shows, fix says the right way. applies_to names the language and the
libraries or tools it is about. Answer with JSON only."""


class LessonDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, title="Lesson")

    general: bool
    title: str = Field(default="", max_length=160)
    mistake: str = Field(default="", max_length=700)
    fix: str = Field(default="", max_length=700)
    applies_to: list[str] = Field(default_factory=list, max_length=6)

    def content(self) -> str:
        return f"{self.mistake}\n\nFix: {self.fix}"

    def usable(self) -> bool:
        return self.general and len(self.title) >= 10 and len(self.mistake) >= 20 and len(self.fix) >= 10


@dataclass(frozen=True)
class FixEpisode:
    """One fix: what failed and the edits that made it pass."""

    attempt_id: uuid.UUID
    step_id: uuid.UUID | None
    source: str  # "correction" | "run"
    goal: str
    failure: str
    edits: tuple[str, ...] = field(default_factory=tuple)


def _clip(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def describe_edit(tool: str, args: Mapping[str, Any]) -> str:
    path = str(args.get("path") or "")
    if tool == "replace_text":
        old, new = _clip(str(args.get("old", "")), _MAX_EDIT // 2), _clip(str(args.get("new", "")), _MAX_EDIT // 2)
        return f"replace_text {path}\n--- old\n{old}\n+++ new\n{new}"
    if tool == "write_file":
        return f"write_file {path}\n{_clip(str(args.get('content', '')), _MAX_EDIT)}"
    return f"{tool}\n{_clip(str(args.get('patch', '')), _MAX_EDIT)}"


def _run_key(call: Any) -> tuple[str, str]:
    args = call.arguments or {}
    return call.tool, str(args.get("command") or args.get("test_command") or args.get("testpaths") or "")


def _correction_failure(correction_input: Mapping[str, Any] | None) -> str:
    items = (correction_input or {}).get("items") or []
    lines = [f"- {_clip(str(i.get('message', '')), 600)}" for i in items if isinstance(i, Mapping) and i.get("message")]
    return "\n".join(lines) or _clip(str((correction_input or {}).get("detail", "")), 600)


def fix_episodes(attempts: Sequence[Any], calls: Sequence[Any], goals: Mapping[uuid.UUID, str], *, limit: int) -> list[FixEpisode]:
    """The fixes of one job, corrections first: a completed correction attempt with its edits, then each red run that
    turned green after edits in the same attempt. ``attempts`` are StepAttempt rows, ``calls`` ToolCallRow rows."""
    by_attempt: dict[uuid.UUID, list[Any]] = {}
    for call in sorted(calls, key=lambda c: (c.started_at, c.turn)):
        if call.attempt_id is not None:
            by_attempt.setdefault(call.attempt_id, []).append(call)
    corrections: list[FixEpisode] = []
    runs: list[FixEpisode] = []
    for attempt in sorted(attempts, key=lambda a: (a.started_at is None, a.started_at, a.attempt_no)):
        own = by_attempt.get(attempt.id, [])
        goal = _clip(goals.get(attempt.step_id, ""), _MAX_GOAL)
        if attempt.kind == "correction" and attempt.status == "completed":
            edits = [describe_edit(c.tool, c.arguments or {}) for c in own if c.tool in EDIT_TOOLS and c.status == "succeeded"]
            failure = _correction_failure(attempt.correction_input)
            red = next((c for c in own if c.tool in RUN_TOOLS and c.error_code in E.RUN_FAILED), None)
            if red is not None:
                failure += f"\n\nA run in the attempt:\n{_clip(red.result_summary, 1500)}"
            if edits and failure.strip():
                corrections.append(FixEpisode(attempt.id, attempt.step_id, "correction", goal, _clip(failure, _MAX_FAILURE), tuple(edits)))
            continue
        red_call: Any = None
        pending: list[str] = []
        for call in own:
            if call.tool in RUN_TOOLS and call.error_code in E.RUN_FAILED:
                if red_call is None or _run_key(call) != _run_key(red_call):
                    red_call, pending = call, []
                continue
            if red_call is None:
                continue
            if call.tool in EDIT_TOOLS and call.status == "succeeded":
                pending.append(describe_edit(call.tool, call.arguments or {}))
            elif call.tool in RUN_TOOLS and call.status == "succeeded" and _run_key(call) == _run_key(red_call):
                if pending:
                    runs.append(
                        FixEpisode(attempt.id, attempt.step_id, "run", goal, _clip(red_call.result_summary, _MAX_FAILURE), tuple(pending))
                    )
                red_call, pending = None, []
    return [*corrections, *runs][: max(0, limit)]


def build_messages(episode: FixEpisode) -> list[ChatMessage]:
    edits: list[str] = []
    used = 0
    for edit in episode.edits:
        if used + len(edit) > _MAX_EDITS:
            edits.append(f"…[{len(episode.edits) - len(edits)} more change(s) left out]")
            break
        edits.append(edit)
        used += len(edit)
    user = "\n\n".join([f"STEP GOAL\n{episode.goal}", f"FAILURE\n{episode.failure}", "CHANGES\n" + "\n\n".join(edits)])
    return [ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", DEFAULT_REDACTOR.text(user))]


async def distill(
    chat: ChatModel, alias: str, episode: FixEpisode, *, job_id: uuid.UUID, max_tokens: int, temperature: float | None = None
) -> LessonDraft:
    result = await chat.structured(
        alias,
        build_messages(episode),
        LessonDraft,
        ctx=CallContext(purpose=LESSON_PURPOSE, job_id=job_id, step_id=episode.step_id, attempt_id=episode.attempt_id),
        max_repairs=1,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return result.value
