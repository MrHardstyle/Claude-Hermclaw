# Runtime job driver (Bauplan §1, §11, §15, §16, §27)

`hermclaw/runtime/driver.py` – `RuntimeJobDriver` implements the scheduler's `JobDriver` protocol. The scheduler
(P25) decides *when* a phase runs, the driver decides *what* happens. All phases are idempotent (crash recovery).

## prepare(job_id) – queued → inventory → discovering → planning → running

1. **inventory** (only with a repository): `GitEngine.create_workspace` – isolated clone on the job branch (re-used if it exists).
2. **discovering**: fast-router triage (`fast-router`, Qwen3 8B, structured `TriageResult`: intent, needs_research, risk, summary;
   failures are non-fatal and reported as status event), repository inventory and goal-related context via `RepoContextProvider.inventory_summary/context_for`.
3. **planning**: `Planner.create_plan(job_id, PlannerInput)` with inventory, known paths, retrieved snippets, test hints and
   a research hint from triage. An existing plan (crash after planning) is reused – never planned twice. Planner and
   replanner calls hold the model lease of the planner profile (`ResourceManager.hold_model`, like coder and heavy
   review): with the planner on another model of the exclusive group, a plan call waits for a running step's lease
   instead of swapping the model under it (R-033, router-mode llama-server). The GPU of the model host belongs to that
   llama-server alone: Ollama, which there serves only the embedding model, runs with `CUDA_VISIBLE_DEVICES=-1`, so an
   embedding call in the gap of a model swap cannot take the VRAM the next model needs (BUG-313). The role's restart handlers reload systemd themselves, so a
   changed drop-in or unit takes effect on the restart that applies it (BUG-314).
4. Result `next_status=running`. Errors become job failures with stable codes (`PLANNER_INVALID_OUTPUT`, git/model codes).

## finalize(job_id) – committing → succeeded/failed

Steps commit themselves via `GitEngine.commit_verified` (commit only after a passed verification). Finalize then:
- `check_base` → if stale `update_to_base` (rebase by default) → `RegressionCheck.rerun` (P23 23.5); failure → `REGRESSION_FAILED` (the job fails, but its branch is still pushed as a draft MR, BUG-210)
- conflict → the workspace stays on its old base, the branch is pushed as a draft MR (`Draft:` title, conflicted files in the description and the report); the job succeeds with the note `Base-Konflikt` (BUG-210). The runtime never merges, so the person merging resolves the conflict.
- `push_job_branch` (+ GitLab merge request when configured) only if the branch carries commits
- deterministic final report `final_report.md` (artifact kind `report`, `runtime/report.py`) from persisted facts:
  steps/attempts, verification, review findings, research sources, commits/pushes/MR, model usage; `jobs.result_summary`.

## replan(job_id, reason, evidence)

`replan_reason_code()` maps scheduler evidence generically to the planner's `ReplanReason`
(handler-provided `replan_reason` wins; error-code families SCOPE/STAGNATION/VERIFIER/WORKER/MERGE…; manual → `operator_request`;
fallback `step_failed`). `Replanner.replan` builds the failure package, preserves completed steps and increments
`jobs.replan_count` (the scheduler does not count).

## Ports (`runtime/ports.py`)

- repository intelligence via the shared `RepoContextProvider` (`inventory_summary`, `context_for`)
- `RegressionCheck.rerun(job_id, workspace)` – adapter over the deterministic verifier (P21)

## Tests

`tests/integration/test_runtime_driver.py` (real PostgreSQL, git with file:// remote, real planner/replanner/scheduler; scripted models):
end-to-end push + report, base moved → rebase + regression, regression failure (no push), merge conflict, blocked step → Gemma replan,
triage failure + job without repository, invalid planner output, re-entrant prepare, reason mapping.
