"""Main coder tool loop (Bauplan §19, Phase 19): Qwen3-Coder 30B, exactly one structured action per turn.

Per turn: context builder (exact budget) → ``ChatModel.structured(CoderAction)`` → ``ToolEngine.execute`` →
turn record (digests only, never reasoning) → stagnation hook. The loop ends on a terminal tool
(``complete_step``, ``block_step``, ``request_replan``), turn-budget exhaustion, a stagnation stop, repeated invalid
model output, cancellation or a pause/checkpoint request. It never decides about verification, review or commits –
that is the step handler's job.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.coder.isolation import ISOLATION_PASSED, isolation_probe
from hermclaw.coder.salvage import salvage_write
from hermclaw.context_builder import (
    ContextBuilder,
    CorrectionItem,
    StepBrief,
    TurnContextInput,
    TurnRecord,
    record_context_report,
)
from hermclaw.contracts.events import EventType
from hermclaw.contracts.tools import SALVAGE_DECISION, CoderAction, ToolName, ToolResult
from hermclaw.core.errors import ModelError, ModelOutputInvalid, ModelOutputTruncated
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.models.protocols import CallContext, ChatModel
from hermclaw.persistence.models import StepAttempt
from hermclaw.scheduler.handlers import CancelToken

log = get_logger(__name__)

# tools that only look at the workspace: a successful call leaves the latest failure current
_READ_ONLY = frozenset(
    {
        ToolName.list_files,
        ToolName.read_file,
        ToolName.read_range,
        ToolName.find_text,
        ToolName.search_repo,
        ToolName.search_symbol,
        ToolName.recall,
        ToolName.git_status,
        ToolName.git_diff,
    }
)
# withheld after a refused repeat until something changes (BUG-181)
_RUNNERS = frozenset({ToolName.run_test.value, ToolName.run_command.value})
# withheld after a refused re-read until a file changes: the content is shown among the files read (BUG-271)
_READERS = frozenset({ToolName.read_file.value, ToolName.read_range.value})
# a cut-off answer is not a format problem: the model has to write less per turn (BUG-223)
TRUNCATED_NOTE = (
    "Your last answer hit the output limit of {max_tokens} tokens and was cut off, so nothing was executed. Write at most "
    "about 250 lines per answer: create a large file with its first part, then add each further part with write_file "
    'and "append": true (or split the code into several modules).'
)
# a cut-off write_file keeps its complete lines; the model continues after them (BUG-252)
SALVAGED_NOTE = (
    "Your last answer hit the output limit of {max_tokens} tokens. Its first {lines} complete lines of {path} were "
    "written; the file now ends with:\n{tail}\nContinue right after that line with write_file path={path!r} and "
    '"append": true, at most about 250 lines per answer.'
)
RUNNERS_WITHHELD = (
    "run_test and run_command are withheld until something changes: do not call them now – nothing changed since the "
    "refused command, so every check gives the same result. Edit the code or the tests now, or call complete_step if "
    "the step is done, or block_step."
)

READERS_WITHHELD = (
    "read_file and read_range are withheld until a file changes: do not call them now – what you read is shown above "
    "among the files you read. Make the change now with replace_text or write_file (or apply_patch), run a different "
    "check, or call block_step."
)

CoderOutcome = Literal["completed", "blocked", "replan", "budget_exhausted", "stagnated", "cancelled", "checkpointed", "model_failed"]

COMPLETION_CONTRACT = (
    "Finish with exactly one terminal tool. complete_step: only after the change is implemented, the targeted tests "
    "and the step's acceptance evidence pass, and you inspected git_diff; report changed_files and tests_run truthfully. "
    "block_step: the step cannot be done within scope/requirements (state reason_code and the concrete obstacle). "
    "request_replan: the plan itself is wrong. The runtime verifies everything deterministically after complete_step."
)


async def _unless_cancelled[T](call: Awaitable[T], token: CancelToken) -> T | None:
    """The call's result, or ``None`` once the token is cancelled first (the call is then cancelled)."""
    task = asyncio.ensure_future(call)
    stop = asyncio.ensure_future(token.wait_cancelled())
    try:
        await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.cancel()
    if task.done():
        return task.result()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return None


class ToolExecutor(Protocol):
    """The part of :class:`hermclaw.tools.engine.ToolEngine` the loop needs."""

    async def execute(
        self, action: CoderAction, *, job_id: uuid.UUID, step_id: uuid.UUID, attempt_id: uuid.UUID, turn: int
    ) -> ToolResult: ...

    def tool_catalog(self) -> list[dict[str, Any]]: ...

    @property
    def granted_turns(self) -> int: ...


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

    async def _runtime_completion(
        self, repeated: CoderAction, refusal: ToolResult, *, job_id: uuid.UUID, step_id: uuid.UUID, attempt_id: uuid.UUID, turn: int
    ) -> tuple[CoderAction, ToolResult]:
        """``complete_step`` on the coder's behalf after it re-proposed a check that passed with nothing changed since."""
        command = str(refusal.data.get("command") or repeated.args.get("command") or "")
        action = CoderAction(
            tool=ToolName.complete_step,
            args={
                "summary": (
                    f"Completed by the runtime: '{command[:200]}' passed and was proposed again without any change "
                    f"since turn {refusal.data.get('previous_turn')}."
                ),
                "tests_run": [command[:500]] if command else [],
                "changed_files": [],
            },
            status="Check passed and nothing changed since – the runtime reports the step as complete",
            decision="runtime-complete",
        )
        result = await self.engine.execute(action, job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn)
        await self._status(job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn, action=action, result=result)
        return action, result

    async def _isolation_check(
        self,
        action: CoderAction,
        result: ToolResult,
        probed: set[str],
        *,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID,
        turn: int,
    ) -> str | None:
        """Run the first failing test alone once; a note when it passes there (state shared between tests, BUG-291)."""
        probe = isolation_probe(str(action.args.get("command") or ""), result.output)
        if probe is None or probe[0] in probed:
            return None
        test_id, command = probe
        probed.add(test_id)
        check = CoderAction(
            tool=ToolName.run_test,
            args={"command": command, "cwd": str(action.args.get("cwd") or ".")},
            status=f"Laufzeit-Prüfung: {test_id[:200]} einzeln ausführen",
            decision="runtime-isolation-check",
        )
        outcome = await self.engine.execute(check, job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn)
        await self._status(job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn, action=check, result=outcome)
        if not outcome.ok or not outcome.data.get("passed"):
            return None
        return ISOLATION_PASSED.format(test=test_id, command=command)

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
        runners_withheld = False
        readers_withheld = False
        probed: set[str] = set()
        invalid_streak = 0
        turn = start_turn - 1
        # the engine may grant turns once when complete_step in the last turn finds a failing check (BUG-349)
        while turn < s.max_turns + self.engine.granted_turns:
            turn += 1
            if token.cancelled:
                return CoderResult("cancelled", turn - 1, records, last_failure=last_failure, detail=token.reason)
            if token.checkpoint_requested:
                return CoderResult("checkpointed", turn - 1, records, last_failure=last_failure, detail=token.reason)
            failure_text = "\n\n".join(x for x in (notice, last_failure) if x) or None
            # the catalogue stays the same in every turn: it is part of the system message, which llama.cpp reuses
            # from its cache only while it is unchanged; withheld checks and reads are named in the turn notice and a
            # repeat is refused by the engine (D-026, ladder A10 #4: three catalogue versions in 15 of 47 turns)
            tools = self.engine.tool_catalog()
            built = await self.builder.build(
                TurnContextInput(
                    step=step,
                    workspace=workspace,
                    turn=turn,
                    max_turns=s.max_turns + self.engine.granted_turns,
                    tools=tools,
                    completion_contract=completion_contract,
                    history=records,
                    latest_failure=failure_text,
                    correction=correction,
                )
            )
            await self._report(built, job_id, step_id, attempt_id)
            try:
                # a cancel interrupts the model call itself: a turn with repairs ran up to ~15 min before the cancel
                # of live job J6 on 0.1.0rc14 took effect (BUG-184)
                call = self.chat.structured(
                    s.alias,
                    built.messages,
                    CoderAction,
                    ctx=CallContext(purpose="coder_turn", job_id=job_id, step_id=step_id, attempt_id=attempt_id),
                    max_repairs=s.max_repairs,
                    truncation_repairs=0,  # the turn note below asks for writing in parts instead (BUG-223)
                    max_tokens=s.max_output_tokens,
                    temperature=s.temperature,
                    timeout_seconds=s.turn_timeout_seconds,
                )
                res = await _unless_cancelled(call, token)
                if res is None:
                    return CoderResult("cancelled", turn - 1, records, last_failure=last_failure, detail=token.reason)
            except ModelOutputInvalid as exc:
                salvaged = salvage_write(exc.partial) if isinstance(exc, ModelOutputTruncated) else None
                if salvaged is not None:
                    action = CoderAction(
                        tool=ToolName.write_file,
                        args={"path": salvaged.path, "content": salvaged.content, "append": salvaged.append},
                        status=f"{salvaged.path}: {salvaged.lines} Zeilen geschrieben (Ausgabelimit erreicht)",
                        decision=SALVAGE_DECISION,
                    )
                    result = await self.engine.execute(action, job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn)
                    if result.ok:
                        tail = "\n".join(salvaged.content.rstrip("\n").splitlines()[-3:])
                        note = SALVAGED_NOTE.format(max_tokens=s.max_output_tokens, lines=salvaged.lines, path=salvaged.path, tail=tail)
                        records.append(self._record(turn, action, result, note=note))
                        await self._persist(attempt_id, turn, records)
                        await self._status(job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn, action=action, result=result)
                        invalid_streak = 0
                        notice = note
                        continue
                invalid_streak += 1
                if isinstance(exc, ModelOutputTruncated):
                    note = TRUNCATED_NOTE.format(max_tokens=s.max_output_tokens)
                else:
                    note = "Your last answer was not one valid action object; answer with exactly one tool call as JSON."
                records.append(self._record(turn, None, None, error_code=exc.code, note=note))
                await self._persist(attempt_id, turn, records)
                # one extra turn after a cut-off answer: the note tells the model how to write in parts
                if invalid_streak >= s.max_invalid_turns + (1 if isinstance(exc, ModelOutputTruncated) else 0):
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
            # a guard refusing a no-op repeat (same command or install, nothing changed) is not a new failure, and reading
            # or searching changes nothing either: the real latest failure stays authoritative (the refusal is shown above
            # it), so the model never re-runs a command only to see an error output it lost (BUG-175)
            repeated = not result.ok and bool(result.data.get("repeated"))
            if not repeated and not (result.ok and action.tool in _READ_ONLY):
                last_failure = None if result.ok else result.output
            notice = result.output if repeated else None
            # after a refused repeat the coder re-proposed the same check up to four times (live battery J1 on
            # 0.1.0rc14): the checks leave the catalogue until a file or the environment changes (BUG-181)
            if repeated and action.tool.value in _RUNNERS and runners_withheld and result.data.get("passed"):
                # BUG-207: the check passed, nothing changed since, and the coder re-proposes it after the refusal
                # told it to finish (rc21 battery J5: three more turns each time). The runtime reports the completion
                # for it; the verifier and the review still judge the step as after any complete_step.
                done = await self._runtime_completion(action, result, job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn)
                records.append(self._record(turn, done[0], done[1]))
                await self._persist(attempt_id, turn, records)
                if done[1].terminal and done[1].ok:
                    return CoderResult("completed", turn, records, completion=dict(done[1].data), last_failure=None)
            # live G1 S008/S014: after a refused re-read the coder asked for the same lines again and again across 50 turns
            # and three attempts without a single edit, until stagnation blocked the step and the job failed (BUG-271)
            changed = (
                result.ok
                and action.tool not in _READ_ONLY
                and action.tool.value not in _RUNNERS
                and result.data.get("changed") is not False  # a write of identical content changes nothing (J3, BUG-182)
            )
            if repeated and action.tool.value in _READERS:
                readers_withheld = True
            elif changed or (result.ok and result.mutated_paths):
                readers_withheld = False
            if repeated and action.tool.value in _RUNNERS:
                runners_withheld = True
            elif changed:
                runners_withheld = False
            if runners_withheld:
                notice = "\n".join(x for x in (notice, RUNNERS_WITHHELD) if x)
            if readers_withheld:
                notice = "\n".join(x for x in (notice, READERS_WITHHELD) if x)
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
                    notice = "\n\n".join(x for x in (directive.message, notice) if x)
                    if action.tool.value in _RUNNERS and not result.ok:
                        isolated = await self._isolation_check(
                            action, result, probed, job_id=job_id, step_id=step_id, attempt_id=attempt_id, turn=turn
                        )
                        if isolated:
                            notice = "\n\n".join((notice, isolated))
        return CoderResult(
            "budget_exhausted",
            turn,
            records,
            last_failure=last_failure,
            error_code="TURN_BUDGET_EXHAUSTED",
            detail=f"no terminal tool within {turn} turns",
        )
