"""HeavyReviewer – Qwen3.8 27B review of one completed step (Bauplan §22, Phase 22.2–22.5).

Flow of ``review(ReviewInput)``:

1. ``review_runs`` row (status ``running``) + ``review.started`` event, committed.
2. Deterministic pre-check: a mutating step without any change evidence (empty diff, no command log) fails closed
   (``REVIEW_EMPTY_DIFF``) – the model is not called.
3. Prompt (22.1) → ``ChatModel.structured(<heavy alias>, ReviewDraft)`` (22.2/22.3) bounded by
   ``policies.review.timeout_seconds``.
4. Severity normalisation (22.4) → invariants (22.5): major/blocker or a failed verifier never PASS.
5. One transaction: run finished (raw_verdict, verdict, invariant_override, summary), ``review_findings`` rows,
   one ``review.finding.created`` event per finding and ``review.finished``.

Every failure (invalid output after the gateway's repairs, timeout, model/infra error, missing heavy profile,
internal error) yields ``status='error'`` with the fail-closed verdict ``fix_required`` and a clear reason. A
cancellation marks the run as error and re-raises. Model reasoning is never requested, stored or logged.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import FindingSeverity, Severity
from hermclaw.contracts.events import EventType
from hermclaw.contracts.review import ReviewContract
from hermclaw.core.config import HermclawConfig, ModelProfileConfig, ReviewPolicy, get_config
from hermclaw.core.errors import ConfigError, HermclawError, ModelOutputInvalid, ModelTimeout
from hermclaw.core.logging import get_logger
from hermclaw.events.store import append_event
from hermclaw.models.protocols import CallContext, ChatModel, StructuredResult
from hermclaw.persistence.models import ReviewFindingRow, ReviewRun
from hermclaw.review.diff import split_diff
from hermclaw.review.invariant import apply_review_invariants
from hermclaw.review.policy import requires_change_evidence, should_review
from hermclaw.review.prompt import build_review_prompt
from hermclaw.review.severity import ReviewDraft, normalise_findings, redact_review
from hermclaw.review.text import clip, one_line, redact
from hermclaw.review.types import (
    EMPTY_DIFF,
    REVIEW_CANCELLED,
    REVIEW_INTERNAL_ERROR,
    REVIEW_PROFILE_MISSING,
    REVIEW_TIMEOUT,
    ReviewInput,
    ReviewOutcome,
    ReviewSettings,
)

log = get_logger(__name__)

HEAVY_ROLE = "heavy"
SOURCE_TYPE = "review"
ACTOR = "heavy-reviewer"
_REASON_CHARS = 2_000
_NOTES_IN_EVENT = 20


@dataclass(frozen=True)
class _Target:
    job_id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID | None
    step_kind: str


def _fail_closed_review(reason: str) -> ReviewContract:
    return ReviewContract(verdict="fix_required", findings=[], summary=clip(reason, 4000))


def changed_files_of(inp: ReviewInput) -> list[str]:
    """Explicit list → verifier's changed files → paths of the diff."""
    if inp.changed_files is not None:
        return list(dict.fromkeys(inp.changed_files))
    if inp.verification.changed_files:
        return list(dict.fromkeys(inp.verification.changed_files))
    _, files = split_diff(inp.diff)
    return list(dict.fromkeys(f.path for f in files))


class HeavyReviewer:
    """Heavy review stage. ``chat`` is the model gateway (``ChatModel`` protocol); persistence via ``sessionmaker``."""

    def __init__(
        self,
        chat: ChatModel,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: HermclawConfig | None = None,
        *,
        settings: ReviewSettings | None = None,
    ) -> None:
        self.chat = chat
        self.sessionmaker = sessionmaker
        self.config = config or get_config()
        self.settings = settings or ReviewSettings()

    # ------------------------------------------------------------------------------------------------ policy
    @property
    def policy(self) -> ReviewPolicy:
        return self.config.policies.review

    def should_review(self, step_kind: str, verifier_passed: bool) -> bool:
        return should_review(self.policy, step_kind, verifier_passed, review_failed_verification=self.settings.review_failed_verification)

    def profile(self) -> ModelProfileConfig:
        """The enabled profile of role ``heavy`` (Qwen3.8 27B, alias e.g. ``heavy-review``)."""
        return self.config.models.by_role(HEAVY_ROLE)

    # ------------------------------------------------------------------------------------------------ review
    async def review(self, inp: ReviewInput) -> ReviewOutcome:
        started = time.monotonic()
        target = _Target(inp.job_id, inp.step_id, inp.attempt_id, inp.step.kind.value)
        changed = changed_files_of(inp)
        try:
            profile: ModelProfileConfig | None = self.profile()
        except ConfigError:
            profile = None
        alias = profile.alias if profile is not None else None
        run_id = await self._start(
            target,
            alias,
            {
                "verifier_passed": inp.verification.passed,
                "changed_files": len(changed),
                "diff_chars": len(inp.diff),
                "snippets": len(inp.snippets),
            },
        )
        try:
            if requires_change_evidence(target.step_kind) and not inp.diff.strip() and not any(c.strip() for c in inp.command_log):
                reason = (
                    f"fail-closed: the mutating step kind '{target.step_kind}' produced no changes "
                    "(empty diff against the workspace base and no executed-command evidence); review was not run"
                )
                outcome = self._error_outcome(run_id, EMPTY_DIFF, reason, alias=None, started=started)
            elif profile is None:
                outcome = self._error_outcome(
                    run_id,
                    REVIEW_PROFILE_MISSING,
                    f"fail-closed: no enabled model profile for role '{HEAVY_ROLE}' is configured; review was not run",
                    alias=None,
                    started=started,
                )
            else:
                outcome = await self._run_model(run_id, inp, profile, changed, started)
        except asyncio.CancelledError:
            cancelled = self._error_outcome(run_id, REVIEW_CANCELLED, "fail-closed: the review was cancelled", alias=alias, started=started)
            await asyncio.shield(self._finish_quietly(target, cancelled))
            raise
        return await self._finish(target, outcome)

    async def fail_closed(
        self,
        *,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID | None,
        step_kind: str,
        error_code: str,
        reason: str,
    ) -> ReviewOutcome:
        """Persist a review run that could not even be prepared (e.g. the diff was unreadable) as a fail-closed error."""
        started = time.monotonic()
        target = _Target(job_id, step_id, attempt_id, str(step_kind))
        run_id = await self._start(target, None, {"prepared": False})
        outcome = self._error_outcome(run_id, error_code, reason, alias=None, started=started)
        return await self._finish(target, outcome)

    # ------------------------------------------------------------------------------------------------ model call
    async def _run_model(
        self, run_id: uuid.UUID, inp: ReviewInput, profile: ModelProfileConfig, changed: Sequence[str], started: float
    ) -> ReviewOutcome:
        alias = profile.alias
        stats: dict[str, int | str | bool] = {}
        overall = float(self.policy.timeout_seconds)
        per_call = float(min(profile.timeout_seconds, self.policy.timeout_seconds))
        try:
            prompt = build_review_prompt(
                inp,
                profile,
                self.settings,
                changed_files=changed,
                generated_globs=self.config.policies.verifier.generated_file_globs,
                withheld_globs=self.config.policies.scope.always_forbidden,
            )
            stats = prompt.stats
            async with asyncio.timeout(overall):
                result = await self.chat.structured(
                    alias,
                    prompt.messages,
                    ReviewDraft,
                    ctx=CallContext(purpose="review", job_id=inp.job_id, step_id=inp.step_id, attempt_id=inp.attempt_id),
                    max_repairs=self.settings.max_repairs,
                    max_tokens=profile.max_output_tokens,
                    temperature=profile.temperature,
                    timeout_seconds=per_call,
                )
        except TimeoutError:
            return self._error_outcome(
                run_id,
                REVIEW_TIMEOUT,
                f"fail-closed: heavy review '{alias}' did not finish within {int(overall)}s",
                alias=alias,
                started=started,
                stats=stats,
            )
        except ModelTimeout as exc:
            return self._error_outcome(
                run_id,
                REVIEW_TIMEOUT,
                f"fail-closed: heavy review '{alias}' timed out ({exc.code}): {clip(one_line(redact(exc.message)), 500)}",
                alias=alias,
                started=started,
                stats=stats,
            )
        except ModelOutputInvalid as exc:
            attempts = exc.details.get("attempts", self.settings.max_repairs + 1)
            errors = exc.details.get("errors") or []
            last_error = clip(one_line(redact(str(errors[-1]))), 400) if isinstance(errors, list) and errors else ""
            return self._error_outcome(
                run_id,
                exc.code,
                f"fail-closed: heavy review '{alias}' returned no valid ReviewContract after {attempts} attempt(s)"
                + (f"; last validation error: {last_error}" if last_error else ""),
                alias=alias,
                started=started,
                stats=stats,
                repair_attempts=self.settings.max_repairs,
            )
        except HermclawError as exc:
            return self._error_outcome(
                run_id,
                exc.code,
                f"fail-closed: heavy review '{alias}' failed ({exc.code}): {clip(one_line(redact(exc.message)), 500)}",
                alias=alias,
                started=started,
                stats=stats,
            )
        except Exception as exc:  # defensive: an unexpected error must never turn into a pass
            log.error("heavy review internal error (%s): %s", type(exc).__name__, clip(redact(str(exc)), 500))
            return self._error_outcome(
                run_id,
                REVIEW_INTERNAL_ERROR,
                f"fail-closed: heavy review failed with an internal error ({type(exc).__name__})",
                alias=alias,
                started=started,
                stats=stats,
            )
        try:
            return self._evaluate(run_id, inp, result, changed=changed, started=started, alias=alias, stats=stats)
        except Exception as exc:  # defensive: post-processing must never turn into a pass either
            log.error("heavy review post-processing error (%s): %s", type(exc).__name__, clip(redact(str(exc)), 500))
            return self._error_outcome(
                run_id,
                REVIEW_INTERNAL_ERROR,
                f"fail-closed: heavy review output could not be evaluated ({type(exc).__name__})",
                alias=alias,
                started=started,
                stats=stats,
            )

    def _evaluate(
        self,
        run_id: uuid.UUID,
        inp: ReviewInput,
        result: StructuredResult[ReviewDraft],
        *,
        changed: Sequence[str],
        started: float,
        alias: str,
        stats: dict[str, int | str | bool],
    ) -> ReviewOutcome:
        """Normalisation (22.4) and deterministic invariants (22.5) of a validated model answer."""
        draft = result.value
        notes = draft.normalisation_notes if isinstance(draft, ReviewDraft) else []
        contract = draft.to_contract() if isinstance(draft, ReviewDraft) else ReviewContract.model_validate(draft.model_dump())
        normalised, more_notes = normalise_findings(redact_review(contract), changed)
        inv = apply_review_invariants(normalised, verifier_passed=inp.verification.passed)
        reason = inv.review.summary or (
            "review passed" if inv.review.verdict == "pass" else f"review requires fixes ({len(inv.review.findings)} finding(s))"
        )
        if inv.overridden:
            reason = f"{reason} [invariant override: {', '.join(inv.reasons)}]"
        return ReviewOutcome(
            review_run_id=run_id,
            status="completed",
            review=inv.review,
            raw_verdict=inv.raw_verdict,
            invariant_override=inv.overridden,
            override_reasons=inv.reasons,
            reason=clip(redact(reason), _REASON_CHARS),
            model_alias=result.result.alias or alias,
            repair_attempts=result.repair_attempts,
            duration_ms=int((time.monotonic() - started) * 1000),
            normalisation_notes=tuple([*notes, *more_notes]),
            prompt_stats=stats,
        )

    @staticmethod
    def _error_outcome(
        run_id: uuid.UUID,
        code: str,
        reason: str,
        *,
        alias: str | None,
        started: float,
        stats: dict[str, int | str | bool] | None = None,
        repair_attempts: int = 0,
    ) -> ReviewOutcome:
        text = clip(redact(reason), _REASON_CHARS)
        return ReviewOutcome(
            review_run_id=run_id,
            status="error",
            review=_fail_closed_review(text),
            fail_closed=True,
            error_code=code,
            reason=text,
            model_alias=alias,
            repair_attempts=repair_attempts,
            duration_ms=int((time.monotonic() - started) * 1000),
            prompt_stats=dict(stats or {}),
        )

    # ------------------------------------------------------------------------------------------------ persistence
    async def _start(self, target: _Target, alias: str | None, payload: dict[str, Any]) -> uuid.UUID:
        run_id = uuid.uuid4()
        async with self.sessionmaker() as session:
            session.add(
                ReviewRun(
                    id=run_id,
                    job_id=target.job_id,
                    step_id=target.step_id,
                    attempt_id=target.attempt_id,
                    model_alias=alias,
                    status="running",
                )
            )
            await session.flush()
            await append_event(
                session,
                EventType.REVIEW_STARTED,
                source_type=SOURCE_TYPE,
                source_id=ACTOR,
                job_id=target.job_id,
                step_id=target.step_id,
                attempt_id=target.attempt_id,
                payload={"review_run_id": str(run_id), "alias": alias, "step_kind": target.step_kind} | payload,
            )
            await session.commit()
        return run_id

    async def _finish(self, target: _Target, outcome: ReviewOutcome) -> ReviewOutcome:
        finding_ids: list[uuid.UUID] = []
        async with self.sessionmaker() as session:
            run = await session.get(ReviewRun, outcome.review_run_id, with_for_update=True)
            if run is None:  # pragma: no cover - the row was created by _start in this call
                raise HermclawError(f"review run {outcome.review_run_id} vanished", code="REVIEW_RUN_MISSING")
            run.status = outcome.status
            run.verdict = outcome.review.verdict
            run.raw_verdict = outcome.raw_verdict
            run.invariant_override = outcome.invariant_override
            run.summary = outcome.reason
            run.model_alias = outcome.model_alias or run.model_alias
            run.finished_at = datetime.now(UTC)
            rows: list[ReviewFindingRow] = []
            for f in outcome.review.findings:
                row = ReviewFindingRow(
                    id=uuid.uuid4(),
                    review_run_id=run.id,
                    severity=f.severity.value,
                    path=redact(f.path) or None,
                    summary=redact(f.summary),
                    evidence=redact(f.evidence) or None,
                    suggested_fix=redact(f.suggested_fix) or None,
                )
                rows.append(row)
                finding_ids.append(row.id)
            session.add_all(rows)
            await session.flush()
            for row in rows:
                await append_event(
                    session,
                    EventType.REVIEW_FINDING_CREATED,
                    source_type=SOURCE_TYPE,
                    source_id=ACTOR,
                    job_id=target.job_id,
                    step_id=target.step_id,
                    attempt_id=target.attempt_id,
                    severity=Severity.warning if row.severity != FindingSeverity.minor.value else Severity.info,
                    payload={
                        "review_run_id": str(run.id),
                        "finding_id": str(row.id),
                        "severity": row.severity,
                        "path": row.path or "",
                        "summary": clip(row.summary, 500),
                    },
                )
            counts = {s.value: 0 for s in FindingSeverity}
            for f in outcome.review.findings:
                counts[f.severity.value] += 1
            await append_event(
                session,
                EventType.REVIEW_FINISHED,
                source_type=SOURCE_TYPE,
                source_id=ACTOR,
                job_id=target.job_id,
                step_id=target.step_id,
                attempt_id=target.attempt_id,
                severity=Severity.error
                if outcome.status == "error"
                else (Severity.info if outcome.review.verdict == "pass" else Severity.warning),
                duration_ms=outcome.duration_ms,
                payload={
                    "review_run_id": str(run.id),
                    "status": outcome.status,
                    "verdict": outcome.review.verdict,
                    "raw_verdict": outcome.raw_verdict,
                    "invariant_override": outcome.invariant_override,
                    "override_reasons": list(outcome.override_reasons),
                    "fail_closed": outcome.fail_closed,
                    "error_code": outcome.error_code,
                    "reason": clip(outcome.reason, 1000),
                    "findings": counts,
                    "alias": outcome.model_alias,
                    "repair_attempts": outcome.repair_attempts,
                    "normalisation_notes": list(outcome.normalisation_notes[:_NOTES_IN_EVENT]),
                    "prompt": dict(outcome.prompt_stats),
                },
            )
            await session.commit()
        return replace(outcome, finding_ids=tuple(finding_ids))

    async def _finish_quietly(self, target: _Target, outcome: ReviewOutcome) -> None:
        try:
            await self._finish(target, outcome)
        except Exception as exc:  # pragma: no cover - best effort while cancelling
            log.warning("could not mark cancelled review run %s: %s", outcome.review_run_id, type(exc).__name__)
