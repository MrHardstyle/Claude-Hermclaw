"""Escalation ladder for stagnation (Bauplan §20, Prompt §17, P20 20.5-20.8).

Deterministic mapping from a :class:`~hermclaw.stagnation.detector.StagnationVerdict` to what the runtime does:

========  ==========================================================================================================
warning   a short notice is injected into the next coder turn ("you repeated X; the result will not change")
diagnose  20.5 forced diagnosis + 20.6 in-loop strategy switch: the next turn gets a diagnosis instruction and a
          concrete different strategy (e.g. ``switch_to_research``, ``request_scope_expansion``,
          ``reread_before_edit``, ``switch_approach``); a ``strategy.changed`` event is emitted
stop      20.7/20.8: the attempt ends with a recommended outcome ``research | heavy_review | replan | block`` chosen
          from what repeated and from the rungs already used for this step
========  ==========================================================================================================

The diagnosis instruction never asks the model to *write down* its reasoning: only its next tool action is used,
with a short decision label naming the new approach. Messages contain only redacted, clipped labels.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from hermclaw.stagnation.detector import RepeatedSignal, SignalKind, StagnationLevel, StagnationVerdict
from hermclaw.stagnation.fingerprints import clip


class Recommendation(StrEnum):
    research = "research"
    heavy_review = "heavy_review"
    replan = "replan"
    block = "block"


class StagnationCause(StrEnum):
    scope = "scope"  # the coder keeps hitting scope/permission/policy limits
    external = "external"  # sandbox, git, network, disk – nothing the coder can fix
    knowledge = "knowledge"  # missing module/API/command knowledge
    test_after_change = "test_after_change"  # the same tests keep failing although the code was changed
    edit_mechanics = "edit_mechanics"  # edits do not apply (text not found, patch failed, no change)
    loop = "loop"  # repeated actions/sequences without progress


class DirectiveKind(StrEnum):
    none = "none"
    notice = "notice"
    diagnose = "diagnose"
    stop = "stop"


# strategy labels (strategy.changed events) -------------------------------------------------------------------
STRATEGY_SWITCH_APPROACH = "switch_approach"
STRATEGY_REREAD = "reread_before_edit"
STRATEGY_SCOPE_EXPANSION = "request_scope_expansion"
STRATEGY_RESEARCH = "switch_to_research"
STRATEGY_BLOCK_EXTERNAL = "block_external_failure"
STOP_STRATEGY: dict[Recommendation, str] = {
    Recommendation.research: "switch_to_research",
    Recommendation.heavy_review: "request_heavy_review",
    Recommendation.replan: "request_replan",
    Recommendation.block: "block_step",
}

# stable tool error codes (see docs/architecture/tools.md); kept as data so the ladder has no import dependency
SCOPE_ERROR_CODES = frozenset(
    {
        "SCOPE_MISSING",
        "SCOPE_VIOLATION",
        "PATH_FORBIDDEN",
        "PATH_OUTSIDE_WORKSPACE",
        "PATH_INVALID",
        "SYMLINK_REFUSED",
        "PATCH_SYMLINK_REFUSED",
        "COMMAND_FORBIDDEN",
        "COMMAND_DESTRUCTIVE",
        "COMMAND_SCOPE_VIOLATION",
        "SCOPE_EXPANSION_DENIED",
        "SCOPE_EXPANSION_FAILED",
        "TOOL_NOT_ALLOWED",
    }
)
EXTERNAL_ERROR_CODES = frozenset(
    {"SANDBOX_ERROR", "GIT_UNAVAILABLE", "REPO_UNAVAILABLE", "RESEARCH_FAILED", "TOOL_INTERNAL_ERROR", "REPLAN_FAILED"}
)
EDIT_ERROR_CODES = frozenset(
    {
        "TEXT_NOT_FOUND",
        "TEXT_COUNT_MISMATCH",
        "PATCH_INVALID",
        "PATCH_FAILED",
        "NO_CHANGE",
        "RANGE_INVALID",
        "ARGS_INVALID",
        "NOT_FOUND",
        "NOT_A_FILE",
        "NOT_A_DIRECTORY",
        "BINARY_FILE",
        "FILE_TOO_LARGE",
        "REDACTED_PLACEHOLDER",
        "PATTERN_INVALID",
    }
)
TEST_ERROR_CODES = frozenset({"TESTS_FAILED", "TEST_ERROR"})

# generic output shapes (not project specific) --------------------------------------------------------------------
_EXTERNAL_TEXT = re.compile(
    r"(?i)could not resolve host|temporary failure in name resolution|name or service not known|network is unreachable"
    r"|connection (?:refused|reset|timed out)|no space left on device|disk quota exceeded|read-only file system"
    r"|cannot allocate memory|out of memory|permission denied \(publickey\)|tls handshake|certificate verify failed"
    r"|too many open files|resource temporarily unavailable"
)
_SCOPE_TEXT = re.compile(r"(?i)outside (?:the )?(?:step )?scope|not in scope|scope violation|forbidden path")
_KNOWLEDGE_TEXT = re.compile(
    r"ModuleNotFoundError|ImportError|No module named|cannot find module|Cannot find module|command not found"
    r"|unresolved import|undefined reference|unknown option|unrecognized arguments"
    r"|no such option|not a valid command|cannot find package|could not find (?:a )?(?:version|package|crate)"
    r"|unknown (?:command|flag|module|package)"
)

# candidate outcomes per cause, in escalation order (block is always the last resort)
LADDER: dict[StagnationCause, tuple[Recommendation, ...]] = {
    StagnationCause.scope: (Recommendation.replan, Recommendation.block),
    StagnationCause.external: (Recommendation.block,),
    StagnationCause.knowledge: (Recommendation.research, Recommendation.heavy_review, Recommendation.replan, Recommendation.block),
    StagnationCause.test_after_change: (Recommendation.heavy_review, Recommendation.replan, Recommendation.block),
    StagnationCause.edit_mechanics: (Recommendation.heavy_review, Recommendation.replan, Recommendation.block),
    StagnationCause.loop: (Recommendation.heavy_review, Recommendation.replan, Recommendation.block),
}


@dataclass(frozen=True)
class LadderContext:
    """What the ladder may use. ``used`` are recommendations already issued for this step (earlier attempts)."""

    research_available: bool = True
    heavy_review_available: bool = True
    used: tuple[str, ...] = ()
    research_used_in_attempt: bool = False


@dataclass(frozen=True)
class EscalationDecision:
    level: StagnationLevel
    kind: DirectiveKind
    cause: StagnationCause | None = None
    message: str = ""  # injected into the next turn (notice/diagnose); summary for stop
    strategy: str | None = None  # strategy.changed label (diagnose: in-loop switch, stop: escalation)
    recommendation: Recommendation | None = None  # stop only
    reasons: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def stops(self) -> bool:
        return self.kind is DirectiveKind.stop

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "kind": self.kind.value,
            "cause": self.cause.value if self.cause else None,
            "strategy": self.strategy,
            "recommendation": self.recommendation.value if self.recommendation else None,
            "reasons": list(self.reasons),
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ReplanHint:
    """Deterministic evidence for a stagnation replan (compatible with ``ReplanTrigger`` fields)."""

    reason_code: str
    evidence: dict[str, Any]
    detail: str


# =================================================================================================== classification
def _codes(signals: Iterable[RepeatedSignal]) -> list[str]:
    return [s.error_code for s in signals if s.error_code]


def _error_text(signals: Iterable[RepeatedSignal]) -> str:
    """Signatures of repeated non-assertion failures. Failing assertions (``TESTS_FAILED``) are about the code under
    test, so their text (test names, assertion messages) is never used to guess an environment or knowledge cause."""
    return "\n".join(s.label for s in signals if s.kind is SignalKind.error and s.error_code != "TESTS_FAILED")


def classify_cause(verdict: StagnationVerdict) -> StagnationCause:
    """Why the coder is stuck, derived from the repeated signals only (deterministic, ordered rules)."""
    signals = verdict.repeated_signals
    if not signals:
        return StagnationCause.loop
    codes = _codes(signals)
    text = _error_text(signals)
    if any(c in SCOPE_ERROR_CODES for c in codes) or _SCOPE_TEXT.search(text):
        return StagnationCause.scope
    if any(c in EXTERNAL_ERROR_CODES for c in codes) or _EXTERNAL_TEXT.search(text):
        return StagnationCause.external
    if _KNOWLEDGE_TEXT.search(text):
        return StagnationCause.knowledge
    dominant = signals[0]
    tests_repeat = any(s.kind is SignalKind.failing_tests for s in signals) or any(c in TEST_ERROR_CODES for c in codes)
    outcome_kinds = (SignalKind.failing_tests, SignalKind.error, SignalKind.changed_files)
    if tests_repeat and any(s.after_code_change for s in signals if s.kind in outcome_kinds):
        return StagnationCause.test_after_change
    if any(c in EDIT_ERROR_CODES for c in codes) or dominant.kind is SignalKind.no_diff_progress:
        return StagnationCause.edit_mechanics
    # repeated actions/sequences, or the same failure without a code change in between (re-running instead of fixing)
    return StagnationCause.loop


def choose_recommendation(cause: StagnationCause, ctx: LadderContext) -> Recommendation:
    """First rung of the cause's ladder that is available and not yet used for this step (block as last resort)."""
    used = set(ctx.used)
    for rec in LADDER[cause]:
        if rec is Recommendation.block:
            return rec
        if rec.value in used:
            continue
        if rec is Recommendation.research and (not ctx.research_available or ctx.research_used_in_attempt):
            continue
        if rec is Recommendation.heavy_review and not ctx.heavy_review_available:
            continue
        return rec
    return Recommendation.block


def in_loop_strategy(cause: StagnationCause, ctx: LadderContext) -> str:
    """20.6: the different strategy the coder is told to take after a forced diagnosis."""
    if cause is StagnationCause.scope:
        return STRATEGY_SCOPE_EXPANSION
    if cause is StagnationCause.external:
        return STRATEGY_BLOCK_EXTERNAL
    if cause is StagnationCause.knowledge and ctx.research_available and not ctx.research_used_in_attempt:
        return STRATEGY_RESEARCH
    if cause is StagnationCause.edit_mechanics:
        return STRATEGY_REREAD
    return STRATEGY_SWITCH_APPROACH


# ======================================================================================================== messages
_STRATEGY_TEXT: dict[str, str] = {
    STRATEGY_SCOPE_EXPANSION: (
        "The repeated failure is a scope/policy refusal; retrying will be refused again. Either call "
        "request_scope_expansion with the exact paths and a justification, or call block_step with "
        "reason_code 'scope_unavailable'."
    ),
    STRATEGY_BLOCK_EXTERNAL: (
        "The repeated failure comes from the environment (sandbox, git, network or disk), not from your change. "
        "Do not retry the same call. If no different action can work around it, call block_step with "
        "reason_code 'external_failure'."
    ),
    STRATEGY_RESEARCH: (
        "The failure points to missing knowledge (module, API, command or option). Call request_research with one "
        "precise question about it before changing the code again."
    ),
    STRATEGY_REREAD: (
        "Your edits are not being applied. Re-read the exact current content with read_range first, then edit with "
        "text copied exactly from it, or use a different edit tool."
    ),
    STRATEGY_SWITCH_APPROACH: (
        "Repeating the same action will produce the same result. Take a different action: inspect a different "
        "part of the code or the failing test itself, change the fix, or finish with complete_step/block_step."
    ),
}


def _what(verdict: StagnationVerdict) -> str:
    dom = verdict.dominant
    return dom.reason() if dom else "repeated actions without progress"


def warning_message(verdict: StagnationVerdict) -> str:
    """20.4 warning notice for the next turn (worded after the dominant signal)."""
    dom = verdict.dominant
    if dom is not None and dom.kind in (SignalKind.failing_tests, SignalKind.error, SignalKind.changed_files) and dom.after_code_change:
        advice = "Your last change did not resolve it; re-check the root cause before the next edit."
    elif dom is not None and dom.kind is SignalKind.no_diff_progress:
        advice = "Your edits did not change the workspace; check that the edit applies to the current file content."
    else:
        advice = "Repeating it will not change the result; choose a different next action."
    return clip(f"Stagnation warning: {_what(verdict)}. {advice}", 600)


def diagnosis_message(verdict: StagnationVerdict, cause: StagnationCause, strategy: str, previous_decision: str | None = None) -> str:
    """20.5 forced diagnosis instruction (+20.6 strategy). Asks for an action, never for written reasoning."""
    avoid = f" It must differ from '{previous_decision}'." if previous_decision else ""
    return clip(
        f"Forced diagnosis – stagnation detected: {_what(verdict)}. Determine the root cause and choose a "
        f"different approach. {_STRATEGY_TEXT.get(strategy, _STRATEGY_TEXT[STRATEGY_SWITCH_APPROACH])} "
        f"Do not explain your reasoning; answer only with the next tool action and set its 'decision' to a short label "
        f"naming the new approach.{avoid} Repeating the same action ends this attempt.",
        1200,
    )


def stop_message(verdict: StagnationVerdict, recommendation: Recommendation) -> str:
    return clip(f"Stagnation stop ({recommendation.value}): " + "; ".join(verdict.reasons[:4]), 1000)


# ========================================================================================================= ladder
def decide(verdict: StagnationVerdict, ctx: LadderContext | None = None) -> EscalationDecision:
    """Turn a verdict into the next runtime action (deterministic)."""
    context = ctx or LadderContext()
    if verdict.level is StagnationLevel.none:
        return EscalationDecision(level=verdict.level, kind=DirectiveKind.none)
    if verdict.research_used and not context.research_used_in_attempt:
        context = LadderContext(context.research_available, context.heavy_review_available, context.used, True)
    cause = classify_cause(verdict)
    details: dict[str, Any] = {"signals": [s.kind.value for s in verdict.repeated_signals]}
    if verdict.level is StagnationLevel.warning:
        return EscalationDecision(
            level=verdict.level,
            kind=DirectiveKind.notice,
            cause=cause,
            message=warning_message(verdict),
            reasons=verdict.reasons,
            details=details,
        )
    if verdict.level is StagnationLevel.diagnose:
        strategy = in_loop_strategy(cause, context)
        prev = next((s.label for s in verdict.repeated_signals if s.kind is SignalKind.decision), None)
        return EscalationDecision(
            level=verdict.level,
            kind=DirectiveKind.diagnose,
            cause=cause,
            message=diagnosis_message(verdict, cause, strategy, prev),
            strategy=strategy,
            reasons=verdict.reasons,
            details=details,
        )
    rec = choose_recommendation(cause, context)
    details["used_before"] = sorted(set(context.used))
    return EscalationDecision(
        level=verdict.level,
        kind=DirectiveKind.stop,
        cause=cause,
        message=stop_message(verdict, rec),
        strategy=STOP_STRATEGY[rec],
        recommendation=rec,
        reasons=verdict.reasons,
        details=details,
    )


def replan_hint(decision: EscalationDecision, verdict: StagnationVerdict) -> ReplanHint:
    """20.7: evidence for the replanner (deterministic signals only, never model reasoning)."""
    reason = "scope_unavailable" if decision.cause is StagnationCause.scope else "stagnation"
    evidence: dict[str, Any] = {
        "cause": decision.cause.value if decision.cause else None,
        "recommendation": decision.recommendation.value if decision.recommendation else None,
        "turn": verdict.turn,
        "signals": [
            {"kind": s.kind.value, "count": s.count, "label": s.label, "error_code": s.error_code, "tests": list(s.tests[:10])}
            for s in verdict.repeated_signals[:6]
        ],
    }
    return ReplanHint(reason_code=reason, evidence=evidence, detail=clip("; ".join(verdict.reasons), 4000))
