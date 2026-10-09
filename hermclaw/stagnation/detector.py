"""Stagnation detector (Bauplan §20, P20 20.1-20.4).

:class:`StagnationDetector` consumes one :class:`Observation` per coder turn and returns a
:class:`StagnationVerdict`. It is pure, deterministic logic: no I/O, no clock, no randomness. Its complete state is a
JSON-serialisable mapping (:meth:`StagnationDetector.to_state` / :meth:`StagnationDetector.from_state`) so it can be
persisted in ``step_attempts.fingerprints`` and survive restarts (see :mod:`hermclaw.stagnation.persistence`).

Signals (each is counted, the verdict level comes from the highest count touched by the current turn):

``action``            the same normalised action (tool + args) within the current *workspace epoch*
``tool_sequence``     the same n-gram of actions (a cycle such as read → edit → test) within the epoch
``error``             the same error signature; cleared when the action that produced it later succeeds
``failing_tests``     the same set of failing tests; cleared when the test action that produced it passes
``changed_files``     the same files edited and the same failure afterwards (an edit/test cycle that does not help)
``no_diff_progress``  consecutive mutating turns that did not move the workspace to a state never seen before
``decision``          consecutive failing turns announcing the same decision label

A *workspace epoch* ends whenever a mutating turn produces a workspace state (diff hash) that was never seen in the
attempt. Re-reading a file or re-running a test after a real change is therefore legitimate; reverting to an earlier
state or writing the same content again is not progress. Thresholds (policy ``warn_after``/``diagnose_after``/
``stop_after``, default 2/3/4) apply to the number of similar occurrences.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from hermclaw.contracts.tools import MUTATING_TOOLS, CoderAction, ToolName, ToolResult
from hermclaw.core.config import StagnationPolicy
from hermclaw.core.errors import ConfigError
from hermclaw.core.logging import get_logger
from hermclaw.stagnation.fingerprints import (
    EMPTY_DIFF_HASH,
    action_fingerprint,
    action_label,
    canonical_json,
    changed_files_fingerprint,
    clip,
    decision_label,
    diff_hash,
    digest,
    error_signature,
    extract_failing_tests,
    failing_tests_fingerprint,
    safe_label,
    sequence_label,
    tool_sequence,
)

log = get_logger(__name__)

STATE_VERSION = 1
_TEST_TOOLS = frozenset({ToolName.run_test.value})
_RUN_TOOLS = frozenset({ToolName.run_test.value, ToolName.run_command.value})
_MUTATING = frozenset(t.value for t in MUTATING_TOOLS)


class StagnationLevel(StrEnum):
    none = "none"
    warning = "warning"
    diagnose = "diagnose"
    stop = "stop"

    @property
    def rank(self) -> int:
        return _LEVEL_RANK[self]


_LEVEL_RANK = {StagnationLevel.none: 0, StagnationLevel.warning: 1, StagnationLevel.diagnose: 2, StagnationLevel.stop: 3}


class SignalKind(StrEnum):
    failing_tests = "failing_tests"
    error = "error"
    changed_files = "changed_files"
    no_diff_progress = "no_diff_progress"
    decision = "decision"
    action = "action"
    tool_sequence = "tool_sequence"


# tie-break for the dominant signal: outcome signals say more about *why* the coder is stuck than action repeats
SIGNAL_PRIORITY: tuple[SignalKind, ...] = tuple(SignalKind)
_COUNTED_KINDS = (SignalKind.action, SignalKind.tool_sequence, SignalKind.error, SignalKind.failing_tests, SignalKind.changed_files)
_EPOCH_KINDS = (SignalKind.action, SignalKind.tool_sequence)


@dataclass(frozen=True)
class DetectorTuning:
    """Secondary knobs (the primary thresholds come from :class:`StagnationPolicy`)."""

    sequence_length: int = 3  # n of the action n-gram ("same tool sequence")
    max_diagnoses: int = 2  # a further forced diagnosis is escalated to stop
    strip_line_numbers: bool = True  # line/column numbers do not distinguish error signatures
    max_entries: int = 128  # per counter map (oldest entries are evicted)
    max_seen_states: int = 256
    history_size: int = 20  # verdicts kept in the state for audit

    def __post_init__(self) -> None:
        if self.sequence_length < 2:
            raise ConfigError("stagnation sequence_length must be >= 2")
        if self.max_diagnoses < 1 or self.max_entries < 8 or self.max_seen_states < 8 or self.history_size < 0:
            raise ConfigError("invalid stagnation detector tuning")


def validate_policy(policy: StagnationPolicy) -> None:
    if not 1 <= policy.warn_after <= policy.diagnose_after <= policy.stop_after:
        raise ConfigError(
            "stagnation thresholds must satisfy 1 <= warn_after <= diagnose_after <= stop_after "
            f"(got {policy.warn_after}/{policy.diagnose_after}/{policy.stop_after})"
        )


def level_for(count: int, policy: StagnationPolicy) -> StagnationLevel:
    """Map a repetition count onto the threshold ladder (20.4)."""
    if count >= policy.stop_after:
        return StagnationLevel.stop
    if count >= policy.diagnose_after:
        return StagnationLevel.diagnose
    if count >= policy.warn_after:
        return StagnationLevel.warning
    return StagnationLevel.none


# ================================================================================================ observations
@dataclass(frozen=True)
class Observation:
    """What the detector needs to know about one coder turn (no model text besides the decision label)."""

    turn: int
    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    ok: bool = True
    error_code: str | None = None
    decision: str = ""
    output: str = ""  # tool output; only used (normalised) for failed turns and test runs
    failing_tests: tuple[str, ...] | None = None  # None: not a test run; (): test run without failures
    diff_text: str | None = None  # full workspace diff after a mutating turn, if the caller can provide it
    changed_files: tuple[str, ...] = ()  # paths this turn mutated
    mutating: bool = False

    @classmethod
    def from_tool(cls, turn: int, action: CoderAction, result: ToolResult, *, diff_text: str | None = None) -> Observation:
        """Build an observation from the coder's action and the tool engine's result."""
        tool = action.tool.value
        changed = tuple(sorted({p for p in result.mutated_paths if p}))
        failing: tuple[str, ...] | None = None
        if tool in _TEST_TOOLS:
            failing = () if result.ok else extract_failing_tests(result.output)
        elif tool in _RUN_TOOLS and not result.ok:
            ids = extract_failing_tests(result.output)
            failing = ids or None
        return cls(
            turn=turn,
            tool=tool,
            args=dict(action.args),
            ok=result.ok,
            error_code=result.error_code,
            decision=action.decision,
            output=result.output if (not result.ok or tool in _RUN_TOOLS) else "",
            failing_tests=failing,
            diff_text=diff_text,
            changed_files=changed,
            mutating=is_mutating(tool, changed),
        )


def is_mutating(tool: str, changed_files: Iterable[str] = ()) -> bool:
    """A turn is mutating if it used a write tool or a command reported changed files."""
    return tool in _MUTATING or any(True for _ in changed_files)


# ==================================================================================================== verdicts
@dataclass(frozen=True)
class RepeatedSignal:
    kind: SignalKind
    key: str
    count: int
    level: StagnationLevel
    label: str
    tool: str | None = None
    error_code: str | None = None
    after_code_change: bool = False
    tests: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "key": self.key,
            "count": self.count,
            "level": self.level.value,
            "label": self.label,
            "tool": self.tool,
            "error_code": self.error_code,
            "after_code_change": self.after_code_change,
            "tests": list(self.tests),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RepeatedSignal:
        return cls(
            kind=SignalKind(str(data["kind"])),
            key=str(data.get("key", "")),
            count=int(data.get("count", 0)),
            level=StagnationLevel(str(data.get("level", "none"))),
            label=str(data.get("label", "")),
            tool=_opt_str(data.get("tool")),
            error_code=_opt_str(data.get("error_code")),
            after_code_change=bool(data.get("after_code_change", False)),
            tests=tuple(str(t) for t in data.get("tests") or ()),
        )

    def reason(self) -> str:
        n = self.count
        if self.kind is SignalKind.failing_tests:
            where = " after code changes" if self.after_code_change else " without code changes"
            return clip(f"same failing tests {n}x{where}: {self.label}", 300)
        if self.kind is SignalKind.error:
            where = " after code changes" if self.after_code_change else ""
            return clip(f"same error {n}x{where} ({self.error_code or 'ERROR'}, {self.tool}): {self.label}", 300)
        if self.kind is SignalKind.changed_files:
            return clip(f"same files edited and same failure afterwards {n}x: {self.label}", 300)
        if self.kind is SignalKind.no_diff_progress:
            return f"no workspace change across {n} mutating turns"
        if self.kind is SignalKind.decision:
            return clip(f"same decision '{self.label}' on {n} consecutive failing turns", 300)
        if self.kind is SignalKind.action:
            return clip(f"same action {n}x without workspace progress: {self.label}", 300)
        return clip(f"same tool sequence {n}x without workspace progress: {self.label}", 300)


@dataclass(frozen=True)
class StagnationVerdict:
    """Result of one observation. ``repeated_signals`` holds the signals that reached a level this turn."""

    turn: int
    level: StagnationLevel = StagnationLevel.none
    reasons: tuple[str, ...] = ()
    repeated_signals: tuple[RepeatedSignal, ...] = ()
    progress: bool = False  # this turn moved the workspace to a new state
    research_used: bool = False  # request_research succeeded earlier in this attempt
    diagnoses_issued: int = 0

    @property
    def dominant(self) -> RepeatedSignal | None:
        return self.repeated_signals[0] if self.repeated_signals else None

    @property
    def stagnating(self) -> bool:
        return self.level is not StagnationLevel.none

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "level": self.level.value,
            "reasons": list(self.reasons),
            "signals": [s.to_dict() for s in self.repeated_signals],
            "progress": self.progress,
            "research_used": self.research_used,
            "diagnoses_issued": self.diagnoses_issued,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StagnationVerdict:
        return cls(
            turn=int(data.get("turn", 0)),
            level=StagnationLevel(str(data.get("level", "none"))),
            reasons=tuple(str(r) for r in data.get("reasons") or ()),
            repeated_signals=tuple(RepeatedSignal.from_dict(s) for s in data.get("signals") or () if isinstance(s, Mapping)),
            progress=bool(data.get("progress", False)),
            research_used=bool(data.get("research_used", False)),
            diagnoses_issued=int(data.get("diagnoses_issued", 0)),
        )


# ==================================================================================================== counters
@dataclass
class _Counter:
    count: int
    label: str
    first_turn: int
    last_turn: int
    tool: str = ""
    error_code: str = ""
    action: str = ""  # fingerprint of the action that last produced this entry (cleared when it succeeds)
    first_state: str = ""
    last_state: str = ""
    tests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "label": self.label,
            "first_turn": self.first_turn,
            "last_turn": self.last_turn,
            "tool": self.tool,
            "error_code": self.error_code,
            "action": self.action,
            "first_state": self.first_state,
            "last_state": self.last_state,
            "tests": list(self.tests),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> _Counter:
        return cls(
            count=max(0, int(data.get("count", 0))),
            label=str(data.get("label", "")),
            first_turn=int(data.get("first_turn", 0)),
            last_turn=int(data.get("last_turn", 0)),
            tool=str(data.get("tool", "")),
            error_code=str(data.get("error_code", "")),
            action=str(data.get("action", "")),
            first_state=str(data.get("first_state", "")),
            last_state=str(data.get("last_state", "")),
            tests=[str(t) for t in data.get("tests") or ()][:20],
        )


def _opt_str(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


# ==================================================================================================== detector
class StagnationDetector:
    """Deterministic per-attempt stagnation detector (one instance per step attempt)."""

    def __init__(
        self,
        policy: StagnationPolicy | None = None,
        *,
        tuning: DetectorTuning | None = None,
        initial_diff: str | None = None,
    ) -> None:
        self.policy = policy or StagnationPolicy()
        validate_policy(self.policy)
        self.tuning = tuning or DetectorTuning()
        self._diff_mode = initial_diff is not None
        self._state = diff_hash(initial_diff) if initial_diff is not None else self._files_state({})
        self._seen_states: list[str] = [self._state]
        self._file_states: dict[str, str] = {}
        self._epoch = 0
        self._epoch_actions: list[str] = []
        self._counters: dict[SignalKind, dict[str, _Counter]] = {k: {} for k in _COUNTED_KINDS}
        self._no_progress = 0
        self._decision_label: str | None = None
        self._decision_streak = 0
        self._pending_edits: set[str] = set()
        self._last_turn = -1
        self._turns_observed = 0
        self._diagnoses = 0
        self._warnings = 0
        self._max_level = StagnationLevel.none
        self._research_used = False
        self._stop_verdict: StagnationVerdict | None = None
        self._history: list[dict[str, Any]] = []
        self.escalations: list[str] = []  # recommendations issued for this attempt (written by the escalation ladder)
        self.strategies: list[str] = []  # strategy labels issued for this attempt

    # ------------------------------------------------------------------------------------------------ queries
    @property
    def stopped(self) -> bool:
        return self._stop_verdict is not None

    @property
    def stop_verdict(self) -> StagnationVerdict | None:
        return self._stop_verdict

    @property
    def turns_observed(self) -> int:
        return self._turns_observed

    @property
    def last_turn(self) -> int:
        return self._last_turn

    @property
    def workspace_state(self) -> str:
        return self._state

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def research_used(self) -> bool:
        return self._research_used

    @property
    def diagnoses_issued(self) -> int:
        return self._diagnoses

    def count(self, kind: SignalKind, key: str) -> int:
        """Current count of a counted signal (0 if unknown); for tests and diagnostics."""
        if kind is SignalKind.no_diff_progress:
            return self._no_progress
        if kind is SignalKind.decision:
            return self._decision_streak if self._decision_label == key else 0
        entry = self._counters[kind].get(key)
        return entry.count if entry else 0

    # ---------------------------------------------------------------------------------------------- observing
    def observe(self, obs: Observation) -> StagnationVerdict:
        """Fold one turn into the state and return the verdict for it (20.1-20.4)."""
        if self._stop_verdict is not None:
            return StagnationVerdict(
                turn=obs.turn,
                level=StagnationLevel.stop,
                reasons=("stagnation stop already issued for this attempt", *self._stop_verdict.reasons),
                repeated_signals=self._stop_verdict.repeated_signals,
                research_used=self._research_used,
                diagnoses_issued=self._diagnoses,
            )
        if self._turns_observed and obs.turn <= self._last_turn:
            # a replayed turn (e.g. after a restart) must not be counted twice
            return StagnationVerdict(turn=obs.turn, reasons=("turn already observed",), research_used=self._research_used)
        self._last_turn = obs.turn
        self._turns_observed += 1
        touched: list[tuple[SignalKind, str]] = []
        action_fp = action_fingerprint(obs.tool, obs.args)

        # 20.3 diff progress ---------------------------------------------------------------------------------
        progress = False
        if obs.mutating:
            new_state = self._next_state(obs, action_fp)
            if new_state is not None and new_state not in self._seen_states:
                progress = True
                self._enter_state(new_state)
                self._start_epoch()
                self._no_progress = 0
            else:
                if new_state is not None:
                    self._state = new_state
                self._no_progress += 1
                touched.append((SignalKind.no_diff_progress, ""))
            if obs.changed_files:
                self._pending_edits.update(obs.changed_files)

        # 20.1 actions and tool sequences (within the workspace epoch) ---------------------------------------
        self._epoch_actions.append(action_fp)
        if len(self._epoch_actions) > 4 * self.tuning.sequence_length:
            del self._epoch_actions[: len(self._epoch_actions) - 4 * self.tuning.sequence_length]
        self._bump(SignalKind.action, action_fp, obs, label=action_label(obs.tool, obs.args), action=action_fp)
        touched.append((SignalKind.action, action_fp))
        n = self.tuning.sequence_length
        seq = tool_sequence(self._epoch_actions, n)
        if seq is not None and len(set(self._epoch_actions[-n:])) > 1:
            self._bump(SignalKind.tool_sequence, seq, obs, label=sequence_label(self._epoch_actions, n), action=action_fp)
            touched.append((SignalKind.tool_sequence, seq))

        # 20.2 outcomes ----------------------------------------------------------------------------------------
        failure_fp: str | None = None
        if obs.ok:
            self._clear_outcomes(action_fp)
            self._decision_label, self._decision_streak = None, 0
            if obs.tool == ToolName.request_research.value:
                self._research_used = True
        else:
            sig = error_signature(obs.tool, obs.error_code, obs.output, strip_line_numbers=self.tuning.strip_line_numbers)
            self._bump(SignalKind.error, sig.digest, obs, label=sig.signature, action=action_fp, error_code=sig.error_code)
            touched.append((SignalKind.error, sig.digest))
            failure_fp = f"e:{sig.digest}"
            label = decision_label(obs.decision)
            if label is not None and label == self._decision_label:
                self._decision_streak += 1
            else:
                self._decision_label, self._decision_streak = label, (1 if label else 0)
            if label is not None:
                touched.append((SignalKind.decision, label))
        if obs.failing_tests:
            tests_fp = failing_tests_fingerprint(obs.failing_tests)
            if tests_fp is not None:
                shown = ", ".join(obs.failing_tests[:3]) + (f" (+{len(obs.failing_tests) - 3} more)" if len(obs.failing_tests) > 3 else "")
                self._bump(
                    SignalKind.failing_tests,
                    tests_fp,
                    obs,
                    label=safe_label(shown, 200),
                    action=action_fp,
                    error_code=obs.error_code or "",
                    tests=list(obs.failing_tests[:20]),
                )
                touched.append((SignalKind.failing_tests, tests_fp))
                failure_fp = f"t:{tests_fp}"

        # edit/test cycles: same files edited, same failure afterwards ---------------------------------------
        if obs.tool in _RUN_TOOLS:
            if failure_fp is not None and self._pending_edits:
                files_fp = changed_files_fingerprint(self._pending_edits) or ""
                key = digest(canonical_json([files_fp, failure_fp]))
                files = sorted(self._pending_edits)
                shown = ", ".join(files[:4]) + (f" (+{len(files) - 4} more)" if len(files) > 4 else "")
                self._bump(SignalKind.changed_files, key, obs, label=safe_label(shown, 200), action=action_fp, tests=files[:20])
                touched.append((SignalKind.changed_files, key))
            if failure_fp is not None or obs.ok:
                self._pending_edits.clear()

        verdict = self._evaluate(obs.turn, touched, progress)
        self._remember(verdict)
        return verdict

    # ------------------------------------------------------------------------------------------------ internals
    def _files_state(self, files: Mapping[str, str]) -> str:
        return digest(canonical_json(["files", sorted(files.items())]))

    def _next_state(self, obs: Observation, action_fp: str) -> str | None:
        """Workspace state after a mutating turn; ``None`` when the turn changed nothing."""
        if not obs.ok and not obs.changed_files:
            return None
        if obs.diff_text is not None:
            self._diff_mode = True
            return diff_hash(obs.diff_text)
        if not obs.changed_files:
            return None  # e.g. write_file with identical content: the tool reports no mutated path
        if self._diff_mode:
            # the diff could not be obtained this turn: derive a state from the previous one and this action
            return digest(canonical_json(["chain", self._state, action_fp, sorted(obs.changed_files)]))
        content = obs.args.get("content")
        for path in obs.changed_files:
            if obs.tool == ToolName.write_file.value and isinstance(content, str) and len(obs.changed_files) == 1:
                self._file_states[path] = digest(canonical_json(["content", content]))
            else:
                self._file_states[path] = digest(canonical_json(["edit", self._file_states.get(path, "base"), action_fp]))
        return self._files_state(self._file_states)

    def _enter_state(self, state: str) -> None:
        self._state = state
        self._seen_states.append(state)
        if len(self._seen_states) > self.tuning.max_seen_states:
            # never forget the initial state: reverting to it is the most common non-progress
            del self._seen_states[1 : len(self._seen_states) - self.tuning.max_seen_states + 1]

    def _start_epoch(self) -> None:
        self._epoch += 1
        self._epoch_actions = []
        for kind in _EPOCH_KINDS:
            self._counters[kind].clear()

    def _bump(
        self,
        kind: SignalKind,
        key: str,
        obs: Observation,
        *,
        label: str,
        action: str,
        error_code: str = "",
        tests: list[str] | None = None,
    ) -> None:
        entries = self._counters[kind]
        entry = entries.get(key)
        if entry is None:
            entry = _Counter(count=0, label=label, first_turn=obs.turn, last_turn=obs.turn, tool=obs.tool, first_state=self._state)
            entries[key] = entry
            if len(entries) > self.tuning.max_entries:
                oldest = min(entries, key=lambda k: entries[k].last_turn)
                entries.pop(oldest, None)
        entry.count += 1
        entry.last_turn = obs.turn
        entry.label = label or entry.label
        entry.tool = obs.tool
        entry.action = action
        entry.error_code = error_code or entry.error_code
        entry.last_state = self._state
        if tests is not None:
            entry.tests = tests

    def _clear_outcomes(self, action_fp: str) -> None:
        """A succeeding action resolves the failures it produced before (e.g. the failing test now passes)."""
        for kind in (SignalKind.error, SignalKind.failing_tests, SignalKind.changed_files):
            entries = self._counters[kind]
            for key in [k for k, v in entries.items() if v.action == action_fp]:
                del entries[key]

    def _signal(self, kind: SignalKind, key: str) -> RepeatedSignal | None:
        tool: str | None = None
        code: str | None = None
        after = False
        tests: tuple[str, ...] = ()
        if kind is SignalKind.no_diff_progress:
            count, label = self._no_progress, "mutating turns"
        elif kind is SignalKind.decision:
            if self._decision_label != key:
                return None
            count, label = self._decision_streak, key
        else:
            entry = self._counters[kind].get(key)
            if entry is None:
                return None
            count, label, tool, code, tests = entry.count, entry.label, entry.tool or None, entry.error_code or None, tuple(entry.tests)
            if kind is SignalKind.changed_files:
                after = True  # by construction: files were edited between the repeated failures
            elif kind in (SignalKind.error, SignalKind.failing_tests):
                after = entry.first_state != entry.last_state
        level = level_for(count, self.policy)
        if level is StagnationLevel.none:
            return None
        return RepeatedSignal(kind, key, count, level, label, tool, code, after, tests)

    def _evaluate(self, turn: int, touched: list[tuple[SignalKind, str]], progress: bool) -> StagnationVerdict:
        signals: dict[tuple[SignalKind, str], RepeatedSignal] = {}
        for kind, key in touched:
            sig = self._signal(kind, key)
            if sig is not None:
                signals[(kind, key)] = sig
        ordered = sorted(signals.values(), key=lambda s: (-s.level.rank, -s.count, SIGNAL_PRIORITY.index(s.kind)))
        level = ordered[0].level if ordered else StagnationLevel.none
        reasons = [s.reason() for s in ordered]
        if level is StagnationLevel.diagnose:
            if self._diagnoses >= self.tuning.max_diagnoses:
                level = StagnationLevel.stop
                reasons.append(f"forced diagnosis already issued {self._diagnoses}x without resolving the stagnation")
            else:
                self._diagnoses += 1
        elif level is StagnationLevel.warning:
            self._warnings += 1
        verdict = StagnationVerdict(
            turn=turn,
            level=level,
            reasons=tuple(reasons),
            repeated_signals=tuple(ordered),
            progress=progress,
            research_used=self._research_used,
            diagnoses_issued=self._diagnoses,
        )
        if level.rank > self._max_level.rank:
            self._max_level = level
        if level is StagnationLevel.stop:
            self._stop_verdict = verdict
        return verdict

    def _remember(self, verdict: StagnationVerdict) -> None:
        if verdict.level is StagnationLevel.none or self.tuning.history_size == 0:
            return
        kinds = [s.kind.value for s in verdict.repeated_signals]
        self._history.append({"turn": verdict.turn, "level": verdict.level.value, "kinds": kinds})
        del self._history[: max(0, len(self._history) - self.tuning.history_size)]

    # -------------------------------------------------------------------------------------------- persistence
    def to_state(self) -> dict[str, Any]:
        """Complete JSON-serialisable state (digests, counts and redacted labels only – never model text)."""
        return {
            "v": STATE_VERSION,
            "policy": self.policy.model_dump(),
            "last_turn": self._last_turn,
            "turns_observed": self._turns_observed,
            "diff_mode": self._diff_mode,
            "state": self._state,
            "seen_states": list(self._seen_states),
            "file_states": dict(sorted(self._file_states.items())),
            "epoch": self._epoch,
            "epoch_actions": list(self._epoch_actions),
            "counters": {k.value: {key: c.to_dict() for key, c in self._counters[k].items()} for k in _COUNTED_KINDS},
            "no_progress": self._no_progress,
            "decision": {"label": self._decision_label, "streak": self._decision_streak},
            "pending_edits": sorted(self._pending_edits),
            "diagnoses": self._diagnoses,
            "warnings": self._warnings,
            "max_level": self._max_level.value,
            "research_used": self._research_used,
            "stop": self._stop_verdict.to_dict() if self._stop_verdict else None,
            "history": list(self._history),
            "escalations": list(self.escalations),
            "strategies": list(self.strategies),
        }

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, Any] | None,
        policy: StagnationPolicy | None = None,
        *,
        tuning: DetectorTuning | None = None,
        initial_diff: str | None = None,
    ) -> StagnationDetector:
        """Restore a detector. A missing, foreign-version or corrupt state yields a fresh detector (logged)."""
        det = cls(policy, tuning=tuning, initial_diff=initial_diff)
        if not state:
            return det
        if state.get("v") != STATE_VERSION:
            log.warning("stagnation state version mismatch; starting fresh", extra={"version": str(state.get("v"))})
            return det
        try:
            det._load(state)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            log.warning("stagnation state unreadable; starting fresh", extra={"error": type(exc).__name__})
            return cls(policy, tuning=tuning, initial_diff=initial_diff)
        return det

    def _load(self, s: Mapping[str, Any]) -> None:
        self._last_turn = int(s["last_turn"])
        self._turns_observed = int(s["turns_observed"])
        self._diff_mode = bool(s.get("diff_mode", self._diff_mode))
        self._state = str(s.get("state") or EMPTY_DIFF_HASH)
        seen = [str(x) for x in s.get("seen_states") or []]
        self._seen_states = seen or [self._state]
        if self._state not in self._seen_states:
            self._seen_states.append(self._state)
        self._file_states = {str(k): str(v) for k, v in (s.get("file_states") or {}).items()}
        self._epoch = int(s.get("epoch", 0))
        self._epoch_actions = [str(x) for x in s.get("epoch_actions") or []]
        raw_counters = s.get("counters") or {}
        for kind in _COUNTED_KINDS:
            entries = raw_counters.get(kind.value) or {}
            self._counters[kind] = {str(k): _Counter.from_dict(v) for k, v in entries.items() if isinstance(v, Mapping)}
        self._no_progress = max(0, int(s.get("no_progress", 0)))
        decision = s.get("decision") or {}
        self._decision_label = _opt_str(decision.get("label"))
        self._decision_streak = max(0, int(decision.get("streak", 0)))
        self._pending_edits = {str(p) for p in s.get("pending_edits") or []}
        self._diagnoses = max(0, int(s.get("diagnoses", 0)))
        self._warnings = max(0, int(s.get("warnings", 0)))
        self._max_level = StagnationLevel(str(s.get("max_level", "none")))
        self._research_used = bool(s.get("research_used", False))
        stop = s.get("stop")
        self._stop_verdict = StagnationVerdict.from_dict(stop) if isinstance(stop, Mapping) else None
        self._history = [dict(h) for h in s.get("history") or [] if isinstance(h, Mapping)]
        self.escalations = [str(x) for x in s.get("escalations") or []]
        self.strategies = [str(x) for x in s.get("strategies") or []]
