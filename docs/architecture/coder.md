# Main coder and correction pipeline (P19, P23)

## CoderLoop (`hermclaw/coder/loop.py`)

`CoderLoop(chat, builder, engine, *, settings=CoderSettings(), stagnation=None, sessionmaker=None)`
`await loop.run(step=StepBrief, workspace, job_id, step_id, attempt_id, token, history=(), start_turn=1, last_failure=None, correction=()) -> CoderResult`

Per turn: `ContextBuilder.build` (exact budget for the coder profile, 32K) → `ChatModel.structured("coder-main", CoderAction)`
(Qwen3-Coder 30B, ≤2 repairs) → `ToolEngine.execute` → `TurnRecord` digest (redacted, never reasoning) persisted to
`step_attempts.history/turns_used` → `context.built` telemetry + user-visible `status` line (the model's short status note) →
stagnation hook (`warning/diagnose` inject a notice into the next turn, `stop` ends the loop with a recommendation).
Outcomes: `completed | blocked | replan | budget_exhausted (20 turns) | stagnated | model_failed | cancelled | checkpointed`.
`CoderResult.checkpoint()` / `history_from_checkpoint()` resume a paused loop in a later attempt.
After a refused repeat the repeated kind of tool leaves the catalogue with a turn notice: `run_test`/`run_command` until something changes (BUG-181), `read_file`/`read_range` until a file changes (a write, or a command that changed files). The content of a refused re-read is already shown among the files read (BUG-271).
Before every attempt the handler adopts uncommitted changes another unfinished step left in the work tree (`ScopeEngine.adopt_leftovers`, BUG-206); files the step changed itself (`file.changed` events) are never adopted, so its own scope violations stay violations (BUG-255). Before that, a leftover that another open step of the job declares in its `allowed_new_paths` (and this step neither declares nor hints) is parked (`GitEngine.park_paths`) instead of adopted; the owning step gets it back when it starts (`unpark_paths` over its own `allowed_new_paths`/`repo_hints`). Otherwise the first step to run after a replan was verified and reviewed on another step's files (BUG-257). A correction attempt hands the paths of its correction items to the tool engine (`ToolEngine.expect_corrections`), which refuses the first `complete_step` while one of those files is untouched (BUG-258), and the commands of its failed lint/test checks (`CorrectionItem.command`, `ToolEngine.expect_rechecks`), which `complete_step` re-runs and refuses with `CHECK_STILL_FAILING` while one still fails (BUG-276), and the files of its failed secret checks (`ToolEngine.expect_secret_rechecks`), which `complete_step` rescans (BUG-279). A resumed correction attempt whose interrupted attempt changed files re-runs the verifier first and replaces the verifier items with the current findings (none if it passes); review and stagnation items stay (BUG-259).
An answer cut off at the output limit is invalid; a cut-off `write_file` is salvaged (`hermclaw/coder/salvage.py`): its complete lines (at least 20) are written through the engine like any write, and the next turn is told the last written lines and to continue with `"append": true` (BUG-252). Other cut-off answers get the write-in-parts note and one extra turn (BUG-223).

## ImplementStepHandler (`hermclaw/coder/handler.py`, kinds `implement`, `documentation`)

1. workspace of the job (GitEngine), scope via `ScopeEngine.current_contract/create_scope` (unavailable → blocked `scope_unavailable`)
2. `ToolEngine` with `ScopeGuard`, executor from `executor_factory` (local sandbox / remote `.222`), `ToolPermissions.for_step`,
   callbacks: research port, `ScopeExpansionHandler`
3. `CoderLoop` (turn budget = min(step.turn_budget, policies.coder.max_turns); requested output = the context builder's `max_output_tokens`, i.e. max(profile, policies.coder.max_output_tokens), BUG-235)
4. completed → `ScopeAuditor.audit_status` → `VerifierPort.verify` (P21) → `ReviewerPort.review` (P22, `enforce_review_invariant`)
   → `GitEngine.stage_allowed` + `commit_verified(verification_run_id)` – **commit only after a passed verification**

Ports: `VerifierPort.verify(step, workspace, …) -> VerificationOutcome(report, run_id)`, `ReviewerPort.should_review/review`,
`ResearchPort.ask`, `ExecutorFactory`, `StagnationFactory`.

## Correction pipeline (P23)

- failure sources: scope audit, verifier, review (`fix_required` or major/blocker), turn budget, stagnation→heavy review
- verifier outage (BUG-303): when every blocking failure is a command the executor could not run (`executor_failure`), the handler verifies again after 5 s and 20 s (`VERIFIER_RETRY_SECONDS`); still unable, the attempt ends `failed` with `VERIFIER_UNAVAILABLE`, a step outage code: the scheduler retries after the outage backoff without counting the attempt, and no correction is spent
- unmet change demands (BUG-278): when the only blocking failures are diff checks whose `must_change` files stayed unchanged (the step changed other files; no forbidden, empty or oversized change, nothing else failed) and the step kind is reviewed, the handler verifies again on the step without those globs (`without_change_demands`, which adds a constraint asking the review whether the goal needs them) and runs the heavy review; a pass emits `acceptance.waived {must_change, reason}` and commits, a `fix_required` (e.g. a blocker naming the file) becomes a normal correction. Before, a plan demanding changes to files the step only reads (input data, an entry point it runs) was uncorrectable and ended in replans
- turn budget exhausted after file changes: the verifier (and review) still run – a pass completes and commits the step, a failure becomes a correction carrying both the turn-budget note and the verifier evidence (BUG-230); without changes it is a plain correction
- `_correction`: writes `step_attempts.correction_input = {pending, source, items[], detail}` with the real evidence,
  increments `steps.correction_count`, emits `correction.started`, returns a retryable failure with `retry_delay_seconds=0`
  → the scheduler starts attempt kind `correction` whose prompt contains the evidence (`CorrectionItem`s)
- bounded by `policies.correction.max_corrections_per_step` (2) and `steps.max_attempts` (3); counters live in PostgreSQL
- every attempt is fully re-verified (regression rerun); after a base update the runtime driver reruns verification (`RegressionCheck`)
- budget exhausted → blocked `repeated_verifier_failure` → scheduler → Gemma replan (completed steps preserved)
- verified state kept (BUG-315): a review `fix_required` without a blocker finding on work that passed verification saves
  that work (`GitEngine.save_verified`: changed, new and deleted files in the workspace's git directory, per step). When
  the step later ends blocked (coder block, replan request, stagnation, exhausted corrections), `_keep_verified` restores
  it (`restore_verified`), verifies it again and commits it with the event `correction.fell_back`; a failing re-verification
  keeps the block. A failed correction never costs verified work.

## Tests

- `tests/integration/test_coder_loop.py` (8): real tool engine/context builder/git workspace/PostgreSQL, scripted model
- `tests/integration/test_coder_handler.py` (4): full pipeline through scheduler + runtime driver: happy path commit+push,
  verifier correction with evidence, review invariant correction, exhausted corrections → replan → success
