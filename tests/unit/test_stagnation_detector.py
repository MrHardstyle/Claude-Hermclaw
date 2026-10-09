"""P20 20.1-20.4 detector: thresholds, progress, signals, serialisation round-trip."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hermclaw.contracts.tools import CoderAction, ToolName, ToolResult
from hermclaw.core.config import StagnationPolicy
from hermclaw.core.errors import ConfigError
from hermclaw.stagnation.detector import (
    DetectorTuning,
    Observation,
    SignalKind,
    StagnationDetector,
    StagnationLevel,
    StagnationVerdict,
    level_for,
)
from hermclaw.stagnation.fingerprints import action_fingerprint

L = StagnationLevel
FAIL_OUT = "FAILED tests/test_app.py::test_add - assert 3 == 4\n1 failed, 2 passed in {d}s"


def read(turn: int, path: str = "app.py", **kw: Any) -> Observation:
    return Observation(turn=turn, tool="read_file", args={"path": path}, **kw)


def edit(
    turn: int, new: str, path: str = "app.py", *, changed: bool = True, ok: bool = True, code: str | None = None, **kw: Any
) -> Observation:
    return Observation(
        turn=turn,
        tool="replace_text",
        args={"path": path, "old": "x", "new": new},
        ok=ok,
        error_code=code,
        output="" if ok else f"{code}: text not found in {path}",
        changed_files=(path,) if changed and ok else (),
        mutating=True,
        **kw,
    )


def trun(
    turn: int, *, failing: tuple[str, ...] = ("tests/test_app.py::test_add",), cmd: str = "pytest -q", dur: str = "0.1"
) -> Observation:
    if not failing:
        return Observation(turn=turn, tool="run_test", args={"command": cmd}, ok=True, output="3 passed", failing_tests=())
    return Observation(
        turn=turn,
        tool="run_test",
        args={"command": cmd},
        ok=False,
        error_code="TESTS_FAILED",
        output=FAIL_OUT.format(d=dur),
        failing_tests=failing,
    )


def levels(det: StagnationDetector, observations: list[Observation]) -> list[StagnationLevel]:
    return [det.observe(o).level for o in observations]


# --------------------------------------------------------------------------------------------------- 20.4 table
@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, L.none), (1, L.none), (2, L.warning), (3, L.diagnose), (4, L.stop), (5, L.stop), (60, L.stop)],
)
def test_default_threshold_table(count: int, expected: StagnationLevel) -> None:
    assert level_for(count, StagnationPolicy()) is expected


@pytest.mark.parametrize(
    ("policy", "count", "expected"),
    [
        (StagnationPolicy(warn_after=3, diagnose_after=5, stop_after=7), 2, L.none),
        (StagnationPolicy(warn_after=3, diagnose_after=5, stop_after=7), 4, L.warning),
        (StagnationPolicy(warn_after=3, diagnose_after=5, stop_after=7), 6, L.diagnose),
        (StagnationPolicy(warn_after=3, diagnose_after=5, stop_after=7), 7, L.stop),
        (StagnationPolicy(warn_after=1, diagnose_after=1, stop_after=1), 1, L.stop),
    ],
)
def test_custom_threshold_table(policy: StagnationPolicy, count: int, expected: StagnationLevel) -> None:
    assert level_for(count, policy) is expected


@pytest.mark.parametrize(("w", "d", "s"), [(0, 1, 2), (3, 2, 4), (2, 5, 4)])
def test_invalid_policy_is_rejected(w: int, d: int, s: int) -> None:
    with pytest.raises(ConfigError):
        StagnationDetector(StagnationPolicy(warn_after=w, diagnose_after=d, stop_after=s))


def test_invalid_tuning_is_rejected() -> None:
    with pytest.raises(ConfigError):
        DetectorTuning(sequence_length=1)
    with pytest.raises(ConfigError):
        DetectorTuning(max_diagnoses=0)


@settings(max_examples=40, deadline=None)
@given(w=st.integers(1, 4), dd=st.integers(0, 3), ds=st.integers(0, 3))
def test_identical_read_only_actions_follow_the_ladder(w: int, dd: int, ds: int) -> None:
    policy = StagnationPolicy(warn_after=w, diagnose_after=w + dd, stop_after=w + dd + ds)
    det = StagnationDetector(policy, tuning=DetectorTuning(max_diagnoses=99))
    got = levels(det, [read(t) for t in range(1, policy.stop_after + 3)])
    assert got == [level_for(n, policy) for n in range(1, policy.stop_after + 1)] + [L.stop, L.stop]
    assert got.index(L.stop) == policy.stop_after - 1  # never "60 identical rounds"


# ------------------------------------------------------------------------------------------------ 20.1 actions
def test_identical_reads_warn_diagnose_stop_at_2_3_4() -> None:
    det = StagnationDetector()
    verdicts = [det.observe(read(t)) for t in range(1, 5)]
    assert [v.level for v in verdicts] == [L.none, L.warning, L.diagnose, L.stop]
    dom = verdicts[3].dominant
    assert dom is not None and dom.kind is SignalKind.action and dom.count == 4 and dom.label == "read_file app.py"
    assert verdicts[3].reasons[0].startswith("same action 4x")
    assert det.stopped and det.diagnoses_issued == 1


def test_reading_different_files_is_not_stagnation() -> None:
    det = StagnationDetector()
    assert levels(det, [read(t, f"src/m{t}.py") for t in range(1, 15)]) == [L.none] * 14


def test_reread_after_real_change_is_legitimate() -> None:
    det = StagnationDetector()
    obs: list[Observation] = []
    t = 0
    for i in range(6):
        t += 1
        obs.append(read(t))
        t += 1
        obs.append(edit(t, f"v{i}"))
    assert levels(det, obs) == [L.none] * 12
    assert det.epoch == 6


def test_equivalent_args_count_as_the_same_action() -> None:
    det = StagnationDetector()
    got = levels(det, [read(1, "app.py"), read(2, "./app.py"), read(3, ".//app.py")])
    assert got == [L.none, L.warning, L.diagnose]


def test_tool_sequence_cycle_without_progress_is_detected() -> None:
    det = StagnationDetector(tuning=DetectorTuning(sequence_length=3))
    cycle = [("read_file", {"path": "a.py"}), ("find_text", {"pattern": "x"}), ("git_status", {})]
    verdicts = [det.observe(Observation(turn=i + 1, tool=tool, args=args)) for i, (tool, args) in enumerate(cycle * 2)]
    last = verdicts[-1]
    kinds = {s.kind for s in last.repeated_signals}
    assert SignalKind.tool_sequence in kinds and SignalKind.action in kinds and last.level is L.warning
    seq = next(s for s in last.repeated_signals if s.kind is SignalKind.tool_sequence)
    assert seq.label == "read_file>find_text>git_status" and seq.count == 2


# -------------------------------------------------------------------------------------------- 20.3 diff progress
def test_rewriting_identical_content_is_no_progress() -> None:
    det = StagnationDetector()
    first = Observation(1, "write_file", {"path": "a.py", "content": "x = 1\n"}, changed_files=("a.py",), mutating=True)
    noop = [Observation(t, "write_file", {"path": "a.py", "content": "x = 1\n"}, mutating=True) for t in range(2, 5)]
    v1 = det.observe(first)
    assert v1.progress and v1.level is L.none
    vs = [det.observe(o) for o in noop]
    assert [v.level for v in vs] == [L.warning, L.diagnose, L.stop]
    assert {s.kind for s in vs[0].repeated_signals} == {SignalKind.action}
    assert {s.kind for s in vs[1].repeated_signals} >= {SignalKind.no_diff_progress, SignalKind.action}
    assert det.count(SignalKind.no_diff_progress, "") == 3


def test_oscillating_content_is_no_progress_in_fallback_mode() -> None:
    det = StagnationDetector()
    contents = ["A", "B", "A", "B", "A"]
    obs = [
        Observation(t + 1, "write_file", {"path": "a.py", "content": c}, changed_files=("a.py",), mutating=True)
        for t, c in enumerate(contents)
    ]
    verdicts = [det.observe(o) for o in obs]
    assert [v.progress for v in verdicts] == [True, True, False, False, False]
    assert [v.level for v in verdicts][2:] == [L.none, L.warning, L.diagnose]


def test_diff_mode_detects_revert_to_original_state() -> None:
    det = StagnationDetector(initial_diff="")
    change = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n"
    v1 = det.observe(edit(1, "b", diff_text=change))
    v2 = det.observe(edit(2, "a", diff_text=""))  # reverted: back to the initial (clean) state
    v3 = det.observe(edit(3, "b", diff_text=change))  # same change as before
    assert v1.progress and not v2.progress and not v3.progress
    assert det.count(SignalKind.no_diff_progress, "") == 2 and v3.level is L.warning


def test_failed_edits_count_as_no_progress_and_reads_do_not_reset_the_streak() -> None:
    det = StagnationDetector()
    seq: list[Observation] = []
    for i in range(4):
        seq.append(edit(2 * i + 1, f"try{i}", ok=False, code="TEXT_NOT_FOUND"))
        seq.append(read(2 * i + 2, f"other{i}.py"))
    got = levels(det, seq)
    assert got[0::2] == [L.none, L.warning, L.diagnose, L.stop]


def test_command_that_changes_files_is_a_mutating_turn() -> None:
    det = StagnationDetector()
    fmt = Observation(1, "run_command", {"command": "ruff format ."}, changed_files=("a.py",), mutating=True)
    v = det.observe(fmt)
    assert v.progress and det.epoch == 1


# ------------------------------------------------------------------------------------------------- 20.2 outcomes
def test_same_failing_test_after_code_changes() -> None:
    det = StagnationDetector()
    verdicts: list[StagnationVerdict] = []
    t = 0
    for i in range(4):
        t += 1
        det.observe(edit(t, f"fix{i}"))
        t += 1
        verdicts.append(det.observe(trun(t, dur=f"0.{i}")))
    assert [v.level for v in verdicts] == [L.none, L.warning, L.diagnose, L.stop]
    dom = verdicts[-1].dominant
    assert dom is not None and dom.kind is SignalKind.failing_tests and dom.after_code_change and dom.count == 4
    assert dom.tests == ("tests/test_app.py::test_add",)
    kinds = {s.kind for s in verdicts[-1].repeated_signals}
    assert {SignalKind.error, SignalKind.changed_files} <= kinds


def test_rerunning_failing_tests_without_changes_is_not_after_code_change() -> None:
    det = StagnationDetector()
    verdicts = [det.observe(trun(t)) for t in range(1, 4)]
    dom = verdicts[-1].dominant
    assert verdicts[-1].level is L.diagnose and dom is not None and not dom.after_code_change


def test_passing_tests_clear_the_failure_counters() -> None:
    det = StagnationDetector()
    obs = [edit(1, "a"), trun(2), edit(3, "b"), trun(4), edit(5, "c"), trun(6, failing=()), edit(7, "d"), trun(8)]
    got = levels(det, obs)
    assert got == [L.none, L.none, L.none, L.warning, L.none, L.none, L.none, L.none]


def test_changing_failure_set_is_progress() -> None:
    det = StagnationDetector()
    obs = [
        edit(1, "a"),
        trun(2, failing=("t::a", "t::b")),
        edit(3, "b"),
        trun(4, failing=("t::b",)),
        edit(5, "c"),
        trun(6, failing=("t::c",)),
    ]
    for o in obs:
        v = det.observe(o)
        assert not any(s.kind is SignalKind.failing_tests for s in v.repeated_signals)


def test_same_error_from_different_commands() -> None:
    det = StagnationDetector()
    out = "Traceback (most recent call last):\n  File \"x.py\", line {n}\nModuleNotFoundError: No module named 'foo'"
    verdicts = [
        det.observe(
            Observation(
                t, "run_command", {"command": f"python script{t}.py"}, ok=False, error_code="COMMAND_FAILED", output=out.format(n=t)
            )
        )
        for t in range(1, 4)
    ]
    assert [v.level for v in verdicts] == [L.none, L.warning, L.diagnose]
    dom = verdicts[-1].dominant
    assert dom is not None and dom.kind is SignalKind.error and "ModuleNotFoundError" in dom.label and dom.error_code == "COMMAND_FAILED"


def test_success_of_the_producing_action_clears_its_error() -> None:
    det = StagnationDetector()
    bad = Observation(1, "read_file", {"path": "new.py"}, ok=False, error_code="NOT_FOUND", output="NOT_FOUND: 'new.py' does not exist")
    det.observe(bad)
    det.observe(Observation(2, "write_file", {"path": "new.py", "content": "x"}, changed_files=("new.py",), mutating=True))
    det.observe(Observation(3, "read_file", {"path": "new.py"}))
    v = det.observe(
        Observation(4, "read_file", {"path": "new.py"}, ok=False, error_code="NOT_FOUND", output="NOT_FOUND: 'new.py' does not exist")
    )
    assert not any(s.kind is SignalKind.error for s in v.repeated_signals)


def test_decision_label_streak_on_failing_turns() -> None:
    det = StagnationDetector()
    obs = [
        Observation(
            t,
            "replace_text",
            {"path": "a.py", "old": f"o{t}", "new": "n"},
            ok=False,
            error_code="TEXT_NOT_FOUND",
            output=f"'o{t}' not found",
            decision="Fix import",
            mutating=True,
        )
        for t in range(1, 5)
    ]
    verdicts = [det.observe(o) for o in obs]
    decision = [next((s for s in v.repeated_signals if s.kind is SignalKind.decision), None) for v in verdicts]
    assert decision[0] is None and decision[1] is not None and decision[1].label == "fix-import"
    assert [v.level for v in verdicts] == [L.none, L.warning, L.diagnose, L.stop]


def test_decision_streak_breaks_on_success_or_new_label() -> None:
    det = StagnationDetector()
    det.observe(Observation(1, "run_command", {"command": "a"}, ok=False, error_code="COMMAND_FAILED", output="err1", decision="probe"))
    det.observe(Observation(2, "list_files", {"path": "."}, decision="probe"))
    det.observe(Observation(3, "run_command", {"command": "b"}, ok=False, error_code="COMMAND_FAILED", output="err2", decision="probe"))
    assert det.count(SignalKind.decision, "probe") == 1
    det.observe(Observation(4, "run_command", {"command": "c"}, ok=False, error_code="COMMAND_FAILED", output="err3", decision="other"))
    assert det.count(SignalKind.decision, "probe") == 0 and det.count(SignalKind.decision, "other") == 1


# ------------------------------------------------------------------------------------------- ladder bookkeeping
def test_diagnoses_are_capped_then_stop() -> None:
    det = StagnationDetector(tuning=DetectorTuning(max_diagnoses=2))
    files = ["a.py", "b.py", "c.py"]
    got: list[StagnationLevel] = []
    t = 0
    for f in files:  # three different files, each read three times in a row -> three diagnoses
        for _ in range(3):
            t += 1
            got.append(det.observe(read(t, f)).level)
    assert got == [L.none, L.warning, L.diagnose, L.none, L.warning, L.diagnose, L.none, L.warning, L.stop]
    assert det.stop_verdict is not None and any("forced diagnosis already issued 2x" in r for r in det.stop_verdict.reasons)


def test_stop_is_sticky_and_replayed_turns_are_ignored() -> None:
    det = StagnationDetector()
    levels(det, [read(t) for t in range(1, 5)])
    again = det.observe(read(5, "other.py"))
    assert again.level is L.stop and again.reasons[0].startswith("stagnation stop already issued")
    det2 = StagnationDetector()
    det2.observe(read(1))
    replay = det2.observe(read(1))
    assert (
        replay.level is L.none
        and det2.turns_observed == 1
        and det2.count(SignalKind.action, action_fingerprint("read_file", {"path": "app.py"})) == 1
    )


def test_research_use_is_tracked() -> None:
    det = StagnationDetector()
    v = det.observe(Observation(1, "request_research", {"question": "how does x work?"}))
    assert det.research_used and v.research_used


def test_observation_from_tool_results() -> None:
    fail = ToolResult(tool=ToolName.run_test, ok=False, output=FAIL_OUT.format(d="0.2"), error_code="TESTS_FAILED")
    obs = Observation.from_tool(3, CoderAction(tool=ToolName.run_test, args={"command": "pytest"}, decision="verify"), fail)
    assert obs.failing_tests == ("tests/test_app.py::test_add",) and not obs.mutating and obs.decision == "verify" and obs.turn == 3
    ok = ToolResult(tool=ToolName.run_test, ok=True, output="3 passed")
    assert Observation.from_tool(4, CoderAction(tool=ToolName.run_test, args={"command": "pytest"}), ok).failing_tests == ()
    cmd = ToolResult(tool=ToolName.run_command, ok=False, output="boom", error_code="COMMAND_FAILED")
    assert Observation.from_tool(5, CoderAction(tool=ToolName.run_command, args={"command": "x"}), cmd).failing_tests is None
    w = ToolResult(tool=ToolName.write_file, ok=True, output="written", mutated_paths=["b.py", "a.py", "a.py"])
    wo = Observation.from_tool(6, CoderAction(tool=ToolName.write_file, args={"path": "a.py", "content": "1"}), w, diff_text="d")
    assert wo.mutating and wo.changed_files == ("a.py", "b.py") and wo.diff_text == "d" and wo.output == ""
    fmt = ToolResult(tool=ToolName.run_command, ok=True, output="fmt", mutated_paths=["a.py"])
    assert Observation.from_tool(7, CoderAction(tool=ToolName.run_command, args={"command": "fmt"}), fmt).mutating


# ------------------------------------------------------------------------------------------------ serialisation
def _scenario() -> list[Observation]:
    seq: list[Observation] = [read(1), read(2)]
    seq += [edit(3, "a"), trun(4), edit(5, "b"), trun(6), Observation(7, "write_file", {"path": "n.py", "content": "z"}, mutating=True)]
    seq += [Observation(8, "run_command", {"command": "x"}, ok=False, error_code="COMMAND_FAILED", output="E1", decision="probe")]
    seq += [edit(9, "c"), trun(10), read(11), read(12)]
    return seq


def test_state_is_json_and_round_trips() -> None:
    det = StagnationDetector()
    for o in _scenario():
        det.observe(o)
    det.escalations.append("heavy_review")
    det.strategies.append("switch_approach")
    state = det.to_state()
    restored = StagnationDetector.from_state(json.loads(json.dumps(state)))
    assert restored.to_state() == state
    assert restored.escalations == ["heavy_review"] and restored.last_turn == 12


@settings(max_examples=40, deadline=None)
@given(cut=st.integers(0, 12), choices=st.lists(st.integers(0, 5), min_size=12, max_size=12))
def test_restart_mid_attempt_yields_identical_verdicts(cut: int, choices: list[int]) -> None:
    def make(t: int, c: int) -> Observation:
        return [
            read(t),
            read(t, "b.py"),
            edit(t, f"v{t % 3}"),
            trun(t),
            trun(t, failing=()),
            edit(t, "x", ok=False, code="TEXT_NOT_FOUND"),
        ][c]

    obs = [make(t + 1, c) for t, c in enumerate(choices)]
    uninterrupted = StagnationDetector()
    expected = [uninterrupted.observe(o) for o in obs]
    first = StagnationDetector()
    got = [first.observe(o) for o in obs[:cut]]
    resumed = StagnationDetector.from_state(json.loads(json.dumps(first.to_state())))
    got += [resumed.observe(o) for o in obs[cut:]]
    assert [v.to_dict() for v in got] == [v.to_dict() for v in expected]
    assert resumed.to_state() == uninterrupted.to_state()


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        {"v": 999, "last_turn": 3},
        {"v": 1},
        {"v": 1, "last_turn": "x", "turns_observed": 1},
        {"v": 1, "last_turn": 1, "turns_observed": 1, "max_level": "bogus"},
    ],
)
def test_corrupt_or_foreign_state_starts_fresh(state: dict[str, Any] | None) -> None:
    det = StagnationDetector.from_state(state)
    assert det.last_turn == -1 and det.turns_observed == 0 and not det.stopped


def test_verdict_dict_round_trip() -> None:
    det = StagnationDetector()
    v = [det.observe(o) for o in [edit(1, "a"), trun(2), edit(3, "b"), trun(4)]][-1]
    assert StagnationVerdict.from_dict(json.loads(json.dumps(v.to_dict()))) == v


def test_state_size_is_bounded() -> None:
    det = StagnationDetector(tuning=DetectorTuning(max_entries=8, max_seen_states=8))
    for t in range(1, 200):
        det.observe(Observation(2 * t, "write_file", {"path": f"f{t}.py", "content": str(t)}, changed_files=(f"f{t}.py",), mutating=True))
        det.observe(
            Observation(2 * t + 1, "run_command", {"command": f"c{t}"}, ok=False, error_code="COMMAND_FAILED", output=f"E{t}Error: x")
        )
    state = det.to_state()
    assert det.turns_observed == 398
    assert len(state["seen_states"]) <= 8 and all(len(v) <= 8 for v in state["counters"].values())
    assert len(json.dumps(state)) < 64_000
