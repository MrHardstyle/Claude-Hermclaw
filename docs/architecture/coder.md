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

## ImplementStepHandler (`hermclaw/coder/handler.py`, kinds `implement`, `documentation`)

1. workspace of the job (GitEngine), scope via `ScopeEngine.current_contract/create_scope` (unavailable → blocked `scope_unavailable`)
2. `ToolEngine` with `ScopeGuard`, executor from `executor_factory` (local sandbox / remote `.222`), `ToolPermissions.for_step`,
   callbacks: research port, `ScopeExpansionHandler`
3. `CoderLoop` (turn budget = min(step.turn_budget, policies.coder.max_turns))
4. completed → `ScopeAuditor.audit_status` → `VerifierPort.verify` (P21) → `ReviewerPort.review` (P22, `enforce_review_invariant`)
   → `GitEngine.stage_allowed` + `commit_verified(verification_run_id)` – **commit only after a passed verification**

Ports: `VerifierPort.verify(step, workspace, …) -> VerificationOutcome(report, run_id)`, `ReviewerPort.should_review/review`,
`ResearchPort.ask`, `ExecutorFactory`, `StagnationFactory`.

## Correction pipeline (P23)

- failure sources: scope audit, verifier, review (`fix_required` or major/blocker), turn budget, stagnation→heavy review
- `_correction`: writes `step_attempts.correction_input = {pending, source, items[], detail}` with the real evidence,
  increments `steps.correction_count`, emits `correction.started`, returns a retryable failure with `retry_delay_seconds=0`
  → the scheduler starts attempt kind `correction` whose prompt contains the evidence (`CorrectionItem`s)
- bounded by `policies.correction.max_corrections_per_step` (2) and `steps.max_attempts` (3); counters live in PostgreSQL
- every attempt is fully re-verified (regression rerun); after a base update the runtime driver reruns verification (`RegressionCheck`)
- budget exhausted → blocked `repeated_verifier_failure` → scheduler → Gemma replan (completed steps preserved)

## Tests

- `tests/integration/test_coder_loop.py` (8): real tool engine/context builder/git workspace/PostgreSQL, scripted model
- `tests/integration/test_coder_handler.py` (4): full pipeline through scheduler + runtime driver: happy path commit+push,
  verifier correction with evidence, review invariant correction, exhausted corrections → replan → success
