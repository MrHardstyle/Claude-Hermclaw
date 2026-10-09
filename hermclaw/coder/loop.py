"""Main coder tool loop (Bauplan §19, Phase 19): Qwen3-Coder 30B, exactly one structured action per turn.

Per turn: context builder (exact budget) → ``ChatModel.structured(CoderAction)`` → ``ToolEngine.execute`` →
turn record (digests only, never reasoning) → stagnation hook. The loop ends on a terminal tool
(``complete_step``, ``block_step``, ``request_replan``), turn-budget exhaustion, a stagnation stop, repeated invalid
model output, cancellation or a pause/checkpoint request. It never decides about verification, review or commits –
that is the step handler's job.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.context_builder import (
    ContextBuilder,
    CorrectionItem,
    StepBrief,
    TurnContextInput,
    TurnRecord,
    record_context_report,
)
from hermclaw.contracts.events import EventType
from hermclaw.contracts.tools import CoderAction, ToolName, ToolResult
from hermclaw.core.errors import ModelError, ModelOutputInvalid
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.models.protocols import CallContext, ChatModel
from hermclaw.persistence.models import StepAttempt
from hermclaw.scheduler.handlers import CancelToken

log = get_logger(__name__)

CoderOutcome = Literal["completed", "blocked", "replan", "budget_exhausted", "stagnated", "cancelled", "checkpointed", "model_failed"]

COMPLETION_CONTRACT = (
    "Finish with exactly one terminal tool. complete_step: only after the change is implemented, the targeted tests "
    "and the step's acceptance evidence pass, and you inspected git_diff; report changed_files and tests_run truthfully. "
    "block_step: the step cannot be done within scope/requirements (state reason_code and the concrete obstacle). "
    "request_replan: the plan itself is wrong. The runtime verifies everything deterministically after complete_step."
)


class ToolExecutor(Protocol):
    """The part of :class:`hermclaw.tools.engine.ToolEngine` the loop needs."""

    async def execute(
        self, action: CoderAction, *, job_id: uuid.UUID, step_id: uuid.UUID, attempt_id: uuid.UUID, turn: int
    ) -> ToolResult: ...

    def tool_catalog(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class TurnObservation:
    turn: int
    action: CoderAction
    result: ToolResult


@dataclass(frozen=True)
class StagnationDirective:
    level: Literal["none", "warning", "diagnose", "stop"] = "none"
    message: str = ""  # injected into the next turn's context (warning/diagnose)
    recommendation: str | None = None  # research | heavy_review | replan | block (stop)
    reasons: tuple[str, ...] = ()


class StagnationHook(Protocol):
    async def observe(self, obs: TurnObservation) -> StagnationDirective: ...


@dataclass
class CoderSettings:
    alias: str = "coder-main"
    max_turns: int = 20
    max_output_tokens: int = 6144
    temperature: float = 0.2
    turn_timeout_seconds: float = 900.0
    max_repairs: int = 2
    max_invalid_turns: int = 2  # consecutive turns without a valid action -> model_failed
    args_digest_chars: int = 240
    result_digest_chars: int = 400


@dataclass
class CoderResult:
    outcome: CoderOutcome
    turns: int
    history: list[TurnRecord] = field(default_factory=list)
    completion: dict[str, Any] | None = None
    block: dict[str, Any] | None = None
    replan_reason: str | None = None
    recommendation: str | None = None
    last_failure: str | None = None
    error_code: str | None = None
    detail: str = ""

    def checkpoint(self) -> dict[str, Any]:
        """JSON state to resume the loop in a later attempt (no model text besides digests)."""
        return {"turn": self.turns, "history": [_record_dict(r) for r in self.history], "last_failure": self.last_failure}


def _record_dict(r: TurnRecord) -> dict[str, Any]:
    return {
        "turn": r.turn,
        "tool": r.tool,
        "args_digest": r.args_digest,
        "ok": r.ok,
        "result_digest": r.result_digest,
        "error_code": r.error_code,
        "mutated_paths": list(r.mutated_paths),
    }


def history_from_checkpoint(checkpoint: dict[str, Any]) -> tuple[int, list[TurnRecord], str | None]:
    rows = checkpoint.get("history") or []
    return (
        int(checkpoint.get("turn") or 0),
        [TurnRecord.from_mapping(r) for r in rows if isinstance(r, dict)],
        checkpoint.get("last_failure"),
    )


def _clip(text: str, n: int) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


class CoderLoop:
    def __init__(
        self,
        chat: ChatModel,
        builder: ContextBuilder,
        engine: ToolExecutor,
        *,
        settings: CoderSettings | None = None,
        stagnation: StagnationHook | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.chat = chat
        self.builder = builder
        self.engine = engine
        self.settings = settings or CoderSettings()
        self.stagnation = stagnation
        self.sm = sessionmaker

    def _record(
        self, turn: int, action: CoderAction | None, result: ToolResult | None, *, error_code: str | None = None, note: str = ""
    ) -> TurnRecord:
        if action is None or result is None:
            return TurnRecord(
                turn=turn,
                tool="invalid_action",
                ok=False,
                result_digest=_clip(note, self.settings.result_digest_chars),
                error_code=error_code,
            )
        args = DEFAULT_REDACTOR.obj(action.args)
        return TurnRecord(
            turn=turn,
            tool=action.tool.value,
            args_digest=_clip(json.dumps(args, sort_keys=True, default=str), self.settings.args_digest_chars),
            ok=result.ok,
            result_digest=_clip(DEFAULT_REDACTOR.text(result.output), self.settings.result_digest_chars),
            error_code=result.error_code,
            mutated_paths=tuple(result.mutated_paths),
        )

    async def _persist(self, attempt_id: uuid.UUID, turn: int, history: Sequence[TurnRecord]) -> None:
        if self.sm is None:
            return
        try:
            async with self.sm() as s:
                await s.execute(
                    update(StepAttempt)
                    .where(StepAttempt.id == attempt_id)
                    .values(turns_used=turn, history=[_record_dict(r) for r in history])
                )
                await s.commit()
        except Exception:  # telemetry only – never break the loop
            log.warning("attempt history not persisted", extra={"attempt_id": str(attempt_id)})

    async def _report(self, built: Any, job_id: uuid.UUID, step_id: uuid.UUID, attempt_id: uuid.UUID) -> None:
        if self.sm is None:
            return
        try:
            async with self.sm() as s:
                await record_context_report(
                    s,
                    built.report,
                    event_type=EventType.CONTEXT_BUILT,
                    job_id=job_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    source_id="coder",
                )
                await s.commit()
        except Exception:
            log.warning("context report not recorded", extra={"attempt_id": str(attempt_id)})

    async def _status(
        self, *, job_id: uuid.UUID, step_id: uuid.UUID, attempt_id: uuid.UUID, turn: int, action: CoderAction, result: ToolResult
    ) -> None:
        """User-visible one-line progress (the model's short status note, never reasoning)."""
        if self.sm is None or not action.status:
            return
        try:
            async with self.sm() as s:
                await append_event(
                    s,
                    EventType.STATUS,
                    source_type="coder",
                    job_id=job_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    payload={
                        "text": f"Turn {turn}: {DEFAULT_REDACTOR.text(action.status)[:200]}",
                        "tool": action.tool.value,
                        "ok": result.ok,
                    },
                )
                await s.commit()
        except Exception:
            log.warning("status event not recorded", extra={"attempt_id": str(attempt_id)})

    async def run(
        self,
        *,
        step: StepBrief,
        workspace: WorkspaceHandle,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID,
        token: CancelToken,
        history: Sequence[TurnRecord] = (),
        start_turn: int = 1,
        last_failure: str | None = None,
        correction: Sequence[CorrectionItem] = (),
        completion_contract: str = COMPLETION_CONTRACT,
    ) -> CoderResult:
        s = self.settings
        records = list(history)
        notice: str | None = None
        invalid_streak = 0
        turn = start_turn - 1
        for turn in range(start_turn, s.max_turns + 1):
            if token.cancelled:
                return CoderResult("cancelled", turn - 1, records, last_failure=last_failure, detail=token.reason)
            if token.checkpoint_requested:
                return CoderResult("checkpointed", turn - 1, records, last_failure=last_failure, detail=token.reason)
            failure_text = "\n\n".join(x for x in (notice, last_failure) if x) or None
            built = await self.builder.build(
                TurnContextInput(
                    step=step,
                    workspace=workspace,
                    turn=turn,
                    max_turns=s.max_turns,
                    tools=self.engine.tool_catalog(),
                    completion_contract=completion_contract,
                    history=records,
                    latest_failure=failure_text,
                    correction=correction,
                )
            )
            await self._report(built, job_id, step_id, attempt_id)
            try:
                res = await self.chat.structured(
                    s.alias,
                    built.messages,
                    CoderAction,
                    ctx=CallContext(purpose="coder_turn", job_id=job_id, step_id=step_id, attempt_id=attempt_id),
                    max_repairs=s.max_repairs,
                    max_tokens=s.max_output_tokens,
                    temperature=s.temperature,
                    timeout_seconds=s.turn_timeout_seconds,
                )
            except ModelOutputInvalid as exc:
                invalid_streak += 1
                note = "Your last answer was not one valid action object; answer with exactly one tool call as JSON."
                records.append(self._record(turn, None, None, error_code=exc.code, note=note))
                await self._persist(attempt_id, turn, records)
                if invalid_streak >= s.max_invalid_turns:
                    return CoderResult("model_failed", turn, records, last_failure=last_failure, error_code=exc.code, detail=exc.message)
                notice = note
                continue
            except ModelError as exc:
                return CoderResult("model_failed", turn - 1, records, last_failure=last_failure, error_code=exc.code, detail=exc.message)
            invalid_streak = 0
            action = res.value
            result = await self.engine.execute(action, job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn)
            records.append(self._record(turn, action, result))
            await self._persist(attempt_id, turn, records)
            await self._status(job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn, action=action, result=result)
            last_failure = None if result.ok else result.output
            notice = None
            if result.terminal and result.ok:
                if action.tool == ToolName.complete_step:
                    return CoderResult("completed", turn, records, completion=dict(result.data), last_failure=None)
                if action.tool == ToolName.block_step:
                    return CoderResult("blocked", turn, records, block=dict(result.data.get("report") or {}), last_failure=last_failure)
                if action.tool == ToolName.request_replan:
                    reason = str(result.data.get("reason") or action.args.get("reason") or "coder requested replan")
                    return CoderResult("replan", turn, records, replan_reason=reason)
            if self.stagnation is not None:
                directive = await self.stagnation.observe(TurnObservation(turn, action, result))
                if directive.level == "stop":
                    return CoderResult(
                        "stagnated",
                        turn,
                        records,
                        recommendation=directive.recommendation or "replan",
                        last_failure=last_failure,
                        error_code="STAGNATION",
                        detail="; ".join(directive.reasons) or directive.message,
                    )
                if directive.level in ("warning", "diagnose") and directive.message:
                    notice = directive.message
        return CoderResult(
            "budget_exhausted",
            turn,
            records,
            last_failure=last_failure,
            error_code="TURN_BUDGET_EXHAUSTED",
            detail=f"no terminal tool within {s.max_turns} turns",
        )
