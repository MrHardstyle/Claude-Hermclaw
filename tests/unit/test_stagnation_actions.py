"""P20 20.5-20.8 escalation ladder: forced diagnosis, strategy switch, replan, stop outcomes (deterministic)."""

from __future__ import annotations

import pytest

import hermclaw.tools.errors as tool_errors
from hermclaw.core.redaction import REDACTED
from hermclaw.stagnation.actions import (
    EDIT_ERROR_CODES,
    EXTERNAL_ERROR_CODES,
    LADDER,
    SCOPE_ERROR_CODES,
    STOP_STRATEGY,
    STRATEGY_BLOCK_EXTERNAL,
    STRATEGY_REREAD,
    STRATEGY_RESEARCH,
    STRATEGY_SCOPE_EXPANSION,
    STRATEGY_SWITCH_APPROACH,
    TEST_ERROR_CODES,
    DirectiveKind,
    LadderContext,
    Recommendation,
    StagnationCause,
    choose_recommendation,
    classify_cause,
    decide,
    replan_hint,
)
from hermclaw.stagnation.detector import Observation, RepeatedSignal, SignalKind, StagnationDetector, StagnationLevel, StagnationVerdict

L = StagnationLevel
R = Recommendation
C = StagnationCause


def sig(
    kind: SignalKind, *, code: str | None = None, label: str = "x", after: bool = False, count: int = 4, tool: str = "run_command"
) -> RepeatedSignal:
    return RepeatedSignal(kind, "k", count, L.stop, label, tool, code, after)


def verdict(*signals: RepeatedSignal, level: StagnationLevel = L.stop, research_used: bool = False) -> StagnationVerdict:
    return StagnationVerdict(
        turn=7, level=level, reasons=tuple(s.reason() for s in signals), repeated_signals=signals, research_used=research_used
    )


def test_error_code_tables_match_the_tool_layer() -> None:
    known = {v for k, v in vars(tool_errors).items() if k.isupper() and isinstance(v, str)}
    for codes in (SCOPE_ERROR_CODES, EXTERNAL_ERROR_CODES, EDIT_ERROR_CODES, TEST_ERROR_CODES):
        assert codes <= known, codes - known
    assert not (SCOPE_ERROR_CODES & EXTERNAL_ERROR_CODES) and not (SCOPE_ERROR_CODES & EDIT_ERROR_CODES)


@pytest.mark.parametrize(
    ("signals", "cause"),
    [
        ((sig(SignalKind.error, code="SCOPE_VIOLATION"),), C.scope),
        ((sig(SignalKind.error, code="PATH_FORBIDDEN", tool="write_file"),), C.scope),
        ((sig(SignalKind.error, code="COMMAND_SCOPE_VIOLATION"), sig(SignalKind.action)), C.scope),
        ((sig(SignalKind.error, code="SCOPE_EXPANSION_DENIED", tool="request_scope_expansion"),), C.scope),
        ((sig(SignalKind.error, code="SANDBOX_ERROR"),), C.external),
        ((sig(SignalKind.error, code="GIT_UNAVAILABLE", tool="git_diff"),), C.external),
        ((sig(SignalKind.error, code="COMMAND_FAILED", label="fatal: unable to access: Could not resolve host: x"),), C.external),
        ((sig(SignalKind.error, code="COMMAND_FAILED", label="OSError: [Errno 28] No space left on device"),), C.external),
        ((sig(SignalKind.error, code="COMMAND_FAILED", label="ModuleNotFoundError: No module named 'foo'"),), C.knowledge),
        (
            (sig(SignalKind.error, code="TEST_ERROR", tool="run_test", label="ERROR tests/a.py - ImportError: cannot import name"),),
            C.knowledge,
        ),
        ((sig(SignalKind.error, code="COMMAND_FAILED", label="bash: foo: command not found"),), C.knowledge),
        # a NameError/AttributeError in the coder's own code is a bug, not missing knowledge
        ((sig(SignalKind.error, code="COMMAND_FAILED", label="NameError: name 'compute' is not defined"),), C.loop),
        ((sig(SignalKind.failing_tests, code="TESTS_FAILED", after=True, tool="run_test"),), C.test_after_change),
        (
            (sig(SignalKind.changed_files, after=True, tool="run_test"), sig(SignalKind.error, code="TESTS_FAILED", tool="run_test")),
            C.test_after_change,
        ),
        # assertion texts never select an environment cause, even if they mention connections or imports
        (
            (sig(SignalKind.failing_tests, code="TESTS_FAILED", after=True, label="test_connection_refused_is_retried", tool="run_test"),),
            C.test_after_change,
        ),
        (
            (sig(SignalKind.error, code="TESTS_FAILED", after=True, label="E AssertionError: ImportError expected", tool="run_test"),),
            C.test_after_change,
        ),
        ((sig(SignalKind.failing_tests, code="TESTS_FAILED", after=False, tool="run_test"),), C.loop),
        ((sig(SignalKind.error, code="TEXT_NOT_FOUND", tool="replace_text"),), C.edit_mechanics),
        ((sig(SignalKind.error, code="PATCH_FAILED", tool="apply_patch"),), C.edit_mechanics),
        ((sig(SignalKind.no_diff_progress), sig(SignalKind.action)), C.edit_mechanics),
        ((sig(SignalKind.action, tool="read_file"),), C.loop),
        ((sig(SignalKind.tool_sequence),), C.loop),
        ((sig(SignalKind.decision),), C.loop),
        ((), C.loop),
    ],
)
def test_classification_table(signals: tuple[RepeatedSignal, ...], cause: StagnationCause) -> None:
    assert classify_cause(verdict(*signals)) is cause


@pytest.mark.parametrize(
    ("cause", "ctx", "expected"),
    [
        (C.test_after_change, LadderContext(), R.heavy_review),
        (C.test_after_change, LadderContext(used=("heavy_review",)), R.replan),
        (C.test_after_change, LadderContext(used=("heavy_review", "replan")), R.block),
        (C.test_after_change, LadderContext(heavy_review_available=False), R.replan),
        (C.scope, LadderContext(), R.replan),
        (C.scope, LadderContext(used=("replan",)), R.block),
        (C.external, LadderContext(), R.block),
        (C.knowledge, LadderContext(), R.research),
        (C.knowledge, LadderContext(research_used_in_attempt=True), R.heavy_review),
        (C.knowledge, LadderContext(research_available=False), R.heavy_review),
        (C.knowledge, LadderContext(used=("research", "heavy_review")), R.replan),
        (C.edit_mechanics, LadderContext(), R.heavy_review),
        (C.loop, LadderContext(), R.heavy_review),
        (C.loop, LadderContext(used=("heavy_review", "heavy_review")), R.replan),
        (C.loop, LadderContext(used=("heavy_review", "replan"), heavy_review_available=False), R.block),
    ],
)
def test_stop_ladder_table(cause: StagnationCause, ctx: LadderContext, expected: Recommendation) -> None:
    assert choose_recommendation(cause, ctx) is expected


def test_every_ladder_ends_with_block_and_has_a_stop_strategy() -> None:
    for rungs in LADDER.values():
        assert rungs[-1] is R.block
    assert {
        R.research: "switch_to_research",
        R.heavy_review: "request_heavy_review",
        R.replan: "request_replan",
        R.block: "block_step",
    } == STOP_STRATEGY


def test_none_and_warning_decisions() -> None:
    assert decide(verdict(level=L.none)).kind is DirectiveKind.none
    w = decide(verdict(sig(SignalKind.action, label="read_file app.py", count=2), level=L.warning))
    assert w.kind is DirectiveKind.notice and w.strategy is None and w.recommendation is None
    assert "read_file app.py" in w.message and "Stagnation warning" in w.message
    after = decide(verdict(sig(SignalKind.failing_tests, after=True, count=2, tool="run_test"), level=L.warning))
    assert "did not resolve" in after.message


@pytest.mark.parametrize(
    ("signal", "strategy", "phrase"),
    [
        (sig(SignalKind.error, code="SCOPE_VIOLATION"), STRATEGY_SCOPE_EXPANSION, "request_scope_expansion"),
        (sig(SignalKind.error, code="SANDBOX_ERROR"), STRATEGY_BLOCK_EXTERNAL, "external_failure"),
        (
            sig(SignalKind.error, code="COMMAND_FAILED", label="ModuleNotFoundError: No module named 'x'"),
            STRATEGY_RESEARCH,
            "request_research",
        ),
        (sig(SignalKind.error, code="TEXT_NOT_FOUND", tool="replace_text"), STRATEGY_REREAD, "read_range"),
        (sig(SignalKind.action, tool="read_file"), STRATEGY_SWITCH_APPROACH, "different action"),
        (sig(SignalKind.failing_tests, after=True, tool="run_test"), STRATEGY_SWITCH_APPROACH, "different action"),
    ],
)
def test_forced_diagnosis_and_strategy_switch(signal: RepeatedSignal, strategy: str, phrase: str) -> None:
    d = decide(verdict(signal, level=L.diagnose))
    assert d.kind is DirectiveKind.diagnose and d.strategy == strategy and d.recommendation is None
    assert d.message.startswith("Forced diagnosis") and phrase in d.message
    assert "Do not explain your reasoning" in d.message and "decision" in d.message


def test_research_strategy_is_not_repeated_once_research_was_used() -> None:
    s = sig(SignalKind.error, code="COMMAND_FAILED", label="ModuleNotFoundError: No module named 'x'")
    assert decide(verdict(s, level=L.diagnose, research_used=True)).strategy == STRATEGY_SWITCH_APPROACH
    assert decide(verdict(s, level=L.stop, research_used=True)).recommendation is R.heavy_review
    assert decide(verdict(s, level=L.stop)).recommendation is R.research


def test_diagnosis_names_the_previous_decision_label() -> None:
    d = decide(verdict(sig(SignalKind.decision, label="fix-import"), level=L.diagnose))
    assert "must differ from 'fix-import'" in d.message


def test_stop_decision_carries_recommendation_and_strategy() -> None:
    d = decide(
        verdict(sig(SignalKind.failing_tests, after=True, tool="run_test", label="tests/a.py::t")), LadderContext(used=("heavy_review",))
    )
    assert d.kind is DirectiveKind.stop and d.stops and d.recommendation is R.replan and d.strategy == "request_replan"
    assert d.cause is C.test_after_change and d.message.startswith("Stagnation stop (replan)")
    assert d.details["used_before"] == ["heavy_review"]
    assert d.to_dict()["recommendation"] == "replan"


def test_heavy_review_then_replan_across_attempts_end_to_end() -> None:
    used: list[str] = []
    outcomes: list[Recommendation | None] = []
    for _attempt in range(3):
        det = StagnationDetector()
        decision = None
        t = 0
        for i in range(4):
            t += 1
            det.observe(Observation(t, "replace_text", {"path": "a.py", "old": "x", "new": f"{i}"}, changed_files=("a.py",), mutating=True))
            t += 1
            v = det.observe(
                Observation(
                    t,
                    "run_test",
                    {"command": "pytest"},
                    ok=False,
                    error_code="TESTS_FAILED",
                    output="FAILED t.py::a - assert 0",
                    failing_tests=("t.py::a",),
                )
            )
            decision = decide(v, LadderContext(used=tuple(used)))
        assert decision is not None and decision.stops
        outcomes.append(decision.recommendation)
        used.append(decision.recommendation.value if decision.recommendation else "")
    assert outcomes == [R.heavy_review, R.replan, R.block]


def test_decisions_are_deterministic() -> None:
    v = verdict(sig(SignalKind.error, code="COMMAND_FAILED", label="boom"), sig(SignalKind.action))
    assert all(decide(v) == decide(v) for _ in range(5))


def test_replan_hint() -> None:
    v = verdict(sig(SignalKind.error, code="SCOPE_VIOLATION", label="write outside scope"))
    hint = replan_hint(decide(v), v)
    assert hint.reason_code == "scope_unavailable" and hint.evidence["cause"] == "scope" and hint.evidence["recommendation"] == "replan"
    v2 = verdict(sig(SignalKind.failing_tests, after=True, tool="run_test", label="t::a"))
    hint2 = replan_hint(decide(v2, LadderContext(used=("heavy_review",))), v2)
    assert (
        hint2.reason_code == "stagnation"
        and hint2.evidence["signals"][0]["kind"] == "failing_tests"
        and "same failing tests" in hint2.detail
    )


def test_messages_contain_only_redacted_labels() -> None:
    secret = "glpat-" + "x" * 24
    det = StagnationDetector()
    out = f"RuntimeError: cannot push with {secret}"
    v = None
    for t in range(1, 4):
        v = det.observe(
            Observation(
                t, "run_command", {"command": f"git push https://oauth2:{secret}@host/x"}, ok=False, error_code="COMMAND_FAILED", output=out
            )
        )
    assert v is not None
    d = decide(v)
    blob = d.message + " ".join(d.reasons) + str(d.to_dict()) + str(v.to_dict()) + str(det.to_state())
    assert secret not in blob and REDACTED in blob
