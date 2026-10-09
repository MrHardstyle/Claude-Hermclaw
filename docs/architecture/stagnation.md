# Stagnation Detection (P20, Bauplan §20, Prompt §17)

## Purpose

Stop the coder from circling: repeated identical or equivalent attempts are detected deterministically and escalated
along a fixed ladder (warning → forced diagnosis + strategy switch → stop with a recommended outcome) instead of
burning the whole turn budget ("keine 60 identischen Runden"). Pure logic plus small persistence helpers; no model
calls, no clock, no randomness, no project- or benchmark-specific rules.

Package: `hermclaw/stagnation/`

| module | role |
|---|---|
| `fingerprints.py` | 20.1/20.2 pure fingerprints: action, tool sequence, decision label, error signature, failing-test set, changed files, diff hash, text normalisation |
| `detector.py` | 20.3/20.4 `StagnationDetector` (signals, workspace epochs, thresholds, JSON state) |
| `actions.py` | 20.5–20.8 escalation ladder (`decide`, `classify_cause`, `choose_recommendation`, `replan_hint`) |
| `persistence.py` | state in `step_attempts.fingerprints["stagnation"]`, prior escalations, events |
| `monitor.py` | `StagnationMonitor` – implements `hermclaw.coder.loop.StagnationHook`; `create_monitor`, `make_stagnation_factory` |

`hermclaw.stagnation` re-exports the pure API; `hermclaw.stagnation.monitor` is imported explicitly (it depends on the
coder package's `TurnObservation`/`StagnationDirective`, the stagnation core does not).

## Interfaces

```python
# fingerprints (all pure, redacted labels)
action_fingerprint(tool, args) -> "tool:<digest>"          # paths normalised, whitespace collapsed, path lists sorted
error_signature(tool, error_code, output, *, strip_line_numbers=True) -> ErrorSignature(digest, signature, error_code, tool)
normalise_text(text, *, strip_line_numbers=True) -> str
extract_failing_tests(output) -> tuple[str, ...]            # pytest, unittest, go, cargo, jest/vitest, mocha, phpunit
failing_tests_fingerprint(ids) / changed_files_fingerprint(paths) -> str | None
diff_hash(diff_text) -> str                                 # ignores blob ids, hunk offsets, trailing whitespace
tool_sequence(action_fps, n) -> str | None; decision_label(decision) -> str | None

# detector
StagnationDetector(policy: StagnationPolicy | None = None, *, tuning: DetectorTuning | None = None, initial_diff: str | None = None)
  .observe(Observation) -> StagnationVerdict
  .to_state() -> dict ; StagnationDetector.from_state(state, policy, *, tuning, initial_diff)
Observation.from_tool(turn, CoderAction, ToolResult, *, diff_text=None)
StagnationVerdict(turn, level: none|warning|diagnose|stop, reasons, repeated_signals, progress, research_used, diagnoses_issued)
RepeatedSignal(kind, key, count, level, label, tool, error_code, after_code_change, tests)

# ladder
decide(verdict, LadderContext(research_available, heavy_review_available, used, research_used_in_attempt)) -> EscalationDecision
EscalationDecision(level, kind: none|notice|diagnose|stop, cause, message, strategy, recommendation, reasons, details)
replan_hint(decision, verdict) -> ReplanHint(reason_code, evidence, detail)   # ReplanTrigger-compatible fields

# persistence (caller's transaction)
load_state / save_state / load_detector / prior_escalations / apply_decision / record_events

# coder integration
await create_monitor(sm, job_id=, step_id=, attempt_id=, policy=, diff_provider=, inherit_from_attempt_id=) -> StagnationMonitor
make_stagnation_factory(policy=, git=GitReader, workspace_for=resolver) -> ImplementDeps.stagnation_factory
```

## Signals and thresholds

Thresholds come from `policies.stagnation` (`warn_after=2`, `diagnose_after=3`, `stop_after=4`; validated
`1 <= warn <= diagnose <= stop`). A threshold counts *similar occurrences*: the 2nd similar occurrence warns, the 3rd
forces a diagnosis, the 4th stops. The verdict level is the highest level among the signals the current turn touched.

| signal | key | counted | cleared |
|---|---|---|---|
| `action` | normalised tool+args | every turn, within the workspace epoch | new workspace state |
| `tool_sequence` | n-gram (n=3) of action fingerprints, not all identical | within the epoch | new workspace state |
| `error` | tool + error code + key error lines (normalised, redacted) | failed turns | the producing action succeeds |
| `failing_tests` | set of failing test ids | test runs with failures | the same test action passes |
| `changed_files` | (files edited since the last run, failure afterwards) | failing run after edits | the run action succeeds |
| `no_diff_progress` | streak | mutating turn without a never-seen workspace state | a new workspace state |
| `decision` | normalised `CoderAction.decision` | consecutive failing turns with the same label | ok turn / other label |

**Workspace epochs / diff progress (20.3).** With a diff provider (production: `GitReader.diff`, which includes
untracked files) the state after each mutating turn is `diff_hash(full diff)`; the initial diff is captured when the
monitor is created. Without it, states are derived from the mutations (`write_file` content digest, chained digests for
other edits). Reverting to an earlier state, rewriting identical content, a failed or no-op edit all count as *no
progress*. Re-reading a file or re-running tests after a real change is legitimate because a new state starts a new
epoch. `after_code_change` on outcome signals is true when the workspace state differed between the first and the
latest occurrence.

**Error normalisation (20.2).** ANSI codes, ISO timestamps, dates, clock times, durations, memory figures, UUIDs,
memory addresses, temp paths (`/tmp`, `/var/folders`, `pytest-of-*`, `tmpXXXXXX`, Windows temp), random seeds, PIDs,
long hex ids, progress percentages and (default) line/column numbers are removed. The digest covers only the *key error
lines* (exception lines, `E` assertion details, compiler `error:` lines, runner FAIL lines …), so tracebacks and source
excerpts that shift with code changes do not hide a repeated error, while a different exception or assertion value
does produce a new signature.

Additional rules: at most `max_diagnoses` (default 2) forced diagnoses per attempt – a further one escalates to stop;
a stop is sticky; replayed turns (`turn <= last_turn`) are ignored; state maps are bounded (`max_entries`,
`max_seen_states`).

## Escalation ladder (20.5–20.8)

Cause classification (ordered, from the repeated signals only):

1. `scope` – scope/policy codes (`SCOPE_VIOLATION`, `PATH_FORBIDDEN`, `COMMAND_SCOPE_VIOLATION`, `SCOPE_EXPANSION_DENIED`, …)
2. `external` – `SANDBOX_ERROR`, `GIT_UNAVAILABLE`, `REPO_UNAVAILABLE`, `RESEARCH_FAILED`, `TOOL_INTERNAL_ERROR`, or generic network/disk/memory failure texts
3. `knowledge` – `ModuleNotFoundError`, `ImportError`, `command not found`, unknown option …
4. `test_after_change` – the same failing tests / test error persist although the code changed
5. `edit_mechanics` – `TEXT_NOT_FOUND`, `PATCH_FAILED`, `NO_CHANGE`, `NOT_FOUND` …, or no diff progress dominates
6. `loop` – everything else (repeated reads/commands/sequences, re-running tests without changes)

Text heuristics (2, 3) are never applied to failing assertions (`TESTS_FAILED`), so test names or assertion messages
cannot select an environment cause.

| level | directive | strategy (`strategy.changed`) |
|---|---|---|
| warning | notice injected into the next turn | – |
| diagnose (20.5/20.6) | forced diagnosis injected into the next turn | scope → `request_scope_expansion`; external → `block_external_failure`; knowledge → `switch_to_research` (if research is available and unused); edit → `reread_before_edit`; else `switch_approach` |
| stop (20.7/20.8) | coder loop ends `stagnated` with a recommendation | `switch_to_research` / `request_heavy_review` / `request_replan` / `block_step` |

Stop recommendation = first rung of the cause's ladder that is available and not yet used for this step (escalations of
earlier attempts are read from their `fingerprints`), `block` as last resort:

| cause | ladder |
|---|---|
| scope | replan → block |
| external | block |
| knowledge | research → heavy_review → replan → block |
| test_after_change | heavy_review → replan → block |
| edit_mechanics | heavy_review → replan → block |
| loop | heavy_review → replan → block |

So "same failing test after code changes" yields heavy review on the first stagnated attempt, replan on the next, then
block. `replan_hint` provides `reason_code` (`scope_unavailable` for scope, else `stagnation`) and deterministic evidence
for the replanner.

**No reasoning is stored or requested.** The diagnosis instruction tells the model to determine the root cause and a
different approach but to answer *only* with its next tool action and a short `decision` label; nothing the model
writes besides that action is used. Messages, reasons, labels, state and events contain only redacted, clipped labels,
digests and counts.

## Flows

1. `CoderLoop` executes a tool and calls `StagnationMonitor.observe(TurnObservation(turn, action, result))`.
2. After a mutating turn the monitor fetches the workspace diff (if configured; failures fall back to derived states).
3. `Observation.from_tool` → `StagnationDetector.observe` → `decide` → `apply_decision` (strategy/escalation recorded in
   the detector state).
4. One transaction: `save_state` (JSONB merge into `fingerprints`, guarded by `last_turn` so a stale writer cannot roll
   back) + `record_events`; commit.
5. The directive goes back to the loop: warning/diagnose message for the next turn, or stop + recommendation
   (the implement handler routes `heavy_review` to the reviewer, everything else to blocked + replan reason `stagnation`).

Restart: `create_monitor` loads the attempt's state and continues counting; `resume` attempts (via
`make_stagnation_factory`) inherit the previous attempt's counters unless it already stopped; correction attempts start
fresh but know the step's used escalations.

## Events

- `stagnation.detected` (source_type `stagnation`, severity info/warning/error for warning/diagnose/stop):
  `turn, level, cause, reasons[], signals[{kind, fingerprint, count, level, label, tool, error_code, after_code_change, tests}], directive, recommendation, diagnoses_issued`
- `strategy.changed` (diagnose: when the strategy differs from the previous one; stop: always):
  `turn, level, cause, from, to, recommendation, reason`

## Configuration

`policies.stagnation.{warn_after, diagnose_after, stop_after}` (config.yaml). Secondary knobs in `DetectorTuning`
(`sequence_length=3`, `max_diagnoses=2`, `strip_line_numbers=True`, `max_entries=128`, `max_seen_states=256`,
`history_size=20`). Ladder availability: `research_available`, `heavy_review_available`.

## Failure behaviour

- Invalid thresholds/tuning → `ConfigError` at construction.
- Missing, foreign-version or corrupt persisted state → fresh detector (logged); a corrupt stored `last_turn` or a
  non-object `fingerprints` column never blocks saving a fresh state.
- Diff provider errors → action-derived states for that turn (logged, no failure).
- Persistence/event errors → logged, `persist_failures` incremented, detection continues in memory; the coder loop is
  never broken by stagnation telemetry.
- Concurrent `observe` calls on one monitor are serialised by an `asyncio.Lock`.

## Tests

- `tests/unit/test_stagnation_fingerprints.py` – normalisation (hypothesis: runs differing only in timings, seeds,
  addresses, temp paths, PIDs and line numbers share a signature), runner parsers, action/diff/file fingerprints,
  redaction, linear-time normalisation of hostile output.
- `tests/unit/test_stagnation_detector.py` – threshold tables (incl. hypothesis over policies), action/epoch logic,
  diff progress (no-op, oscillation, revert in diff mode), outcome signals, decision streak, diagnosis cap, sticky stop,
  replay, serialisation round-trip and hypothesis restart-equivalence.
- `tests/unit/test_stagnation_actions.py` – classification table, ladder table, strategies/messages, cross-attempt
  heavy_review → replan → block, replan hint, error-code tables match `hermclaw.tools.errors`, secret redaction.
- `tests/integration/test_stagnation_persistence.py` – PostgreSQL state round trip/merge/stale-write guard, prior
  escalations, events per level, monitor restart/sticky stop, persistence failure tolerance, resume inheritance via the
  factory, and the real `CoderLoop` + tool engine + git + pytest stopping a looping coder and a "same failing test after
  changes" coder with `heavy_review`.
