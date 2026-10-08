# Gemma Planner and Replanner (P14, P24)

`hermclaw/planner/` turns a job into a validated, enriched and persisted plan (a DAG of steps) and, when execution
hits a problem, into a new plan version that keeps the work already done. Bauplan §3.2, §15, §16.

Gemma (model role `planner`, LiteLLM alias `planner-gemma`) plans and replans. It never writes code, never runs
tools and never mutates git. Everything the model returns is checked by the runtime before it is persisted.
Changing job state is the orchestrator's job (`planning`, `replanning`). The planner only writes plans, plan
versions, steps, dependencies and events.

## Modules

| Module | Responsibility | Build plan |
|---|---|---|
| `inputs.py` | `PlannerInput` (Bauplan §15 input), `PlannerSettings`, test-command detection, known paths and symbols | 14.1 |
| `prompt.py` | system prompts, budgeted user payload, deterministic line-safe truncation, repair message | 14.1 |
| `parsing.py` | JSON extraction from the final answer, schema validation with located error messages | 14.2, 14.3 |
| `loop.py` | one schema-constrained call plus at most 2 repair turns, attempt history | 14.2, 14.4 |
| `validation.py` | semantic rules (capabilities, kinds, DAG/dependency rules, grounding, acceptance sanity, network) | 14.5, 14.6 |
| `enrich.py` | deterministic research steps, acceptance generation, risk floor | 14.7, 14.8, 14.9 |
| `persist.py` | plans / plan_versions / steps / step_dependencies, `step.created` events | 14.x, 24.3 |
| `planner.py` | `Planner.create_plan`, shared `PlannerBase` (model profile, events, fallback recording) | 14.x |
| `failure_package.py` | replan input collected from PostgreSQL (evidence, attempts, scope, research) | 24.1 |
| `replan_contract.py` | `ReplanTrigger`, `ReplanContract` / `ReplanStep` (`rerun_reason`) | 24.2 |
| `replanner.py` | `Replanner.replan`, merge with completed steps, supersede/cancel, scope refresh | 24.2-24.6 |

## Interfaces

```python
Planner(chat: ChatModel, sessionmaker, config: HermclawConfig | None = None, *, settings: PlannerSettings | None = None)
    async create_plan(job_id: UUID, inputs: PlannerInput) -> PlanResult

Replanner(chat: ChatModel, sessionmaker, config=None, *, settings=None)
    async replan(job_id: UUID, trigger: ReplanTrigger, inputs: PlannerInput | None = None) -> PlanResult

ReplanTrigger(reason_code: scope_unavailable | stagnation | test_architecture_conflict | missing_dependency
              | worker_unavailable | research_changed_assumptions | repeated_verifier_failure | repository_changed,
              failed_step_id: UUID | None, evidence: dict, detail: str)

PlanResult(job_id, plan_id, plan_version_id, version, source, model_alias, plan: PlanContract,
           step_ids: dict[step_key, UUID], created_step_keys, repair_attempts, validation_errors, fallback_used,
           notes, preserved_step_keys, superseded_step_keys, rerun_reasons, duration_ms)

plan_json_schema() / replan_json_schema()   # JSON schema sent as structured-output constraint
```

`PlannerInput` holds `repository_inventory`, `retrieved_context: list[ContextSnippet]` (`ContextSnippet.from_hit(RepoHit)`),
`research_summary`, `capabilities` (subset of configured capabilities, `None` means all), `constraints`,
`existing_tests`, `risk_policy` (overrides that can only make the policy stricter), `known_paths`, `known_symbols`,
`test_command`, `test_framework`. The `job` section comes from the `jobs` row and `job_inputs` constraints.

Errors (all `HermclawError`, stable codes):

| Error | Code | When |
|---|---|---|
| `PlannerError` | `PLANNER_INVALID_OUTPUT` | output still invalid after 2 repair attempts; `details.history` holds every attempt's errors |
| `ReplanLimitReached` | `REPLAN_LIMIT_REACHED` | `jobs.replan_count >= policies.correction.max_replans_per_job` |
| `PlanConflict` | `PLAN_EXISTS` | second initial plan, or another planner persisted first (concurrent planning) |
| `PlanConflict` | `PLAN_CHANGED` | the plan version, replan count or completed set changed while the model ran; stale failed step |
| `PlanConflict` | `NO_PLAN` | replan for a job without a plan |
| `NotFoundError` | `NOT_FOUND` | unknown job |
| `ValidationFailed` | `VALIDATION_FAILED` | invalid `risk_policy` override (unknown field, kind or risk) |

Model and gateway errors (`ModelError`, timeouts) pass through unchanged. The 12B technical fallback is chosen by
the gateway, never by the planner.

## Planning flow (`create_plan`)

1. Load the job and refuse it if a plan already exists. Build the `ValidationContext` (available capabilities,
   kind->capability map from `capabilities.step_kind_capability`, known paths and symbols, test command, policy
   forbidden paths and command patterns). Build the prompt and emit `planner.invoked` (input statistics and the
   SHA-256 of the input, never the content). No transaction stays open while the model runs.
2. **Structured output (14.2):** `ChatModel.chat(alias_of_role("planner"), …, json_schema=PlanContract schema)`
   with the profile's `max_output_tokens`, `temperature` and `timeout_seconds`.
3. **Validation**, in this order. Any failure raises an internal `PlanInvalid(phase, errors)`:
   - `parse`: the JSON object of the final content. Fences and surrounding prose are tolerated. An empty answer is an error even when the model produced hidden reasoning, which is never accepted as a plan;
   - `schema` (14.3): `PlanContract` (unknown fields rejected, `S###` ids, duplicate ids, unknown or self dependencies, cycles), with located messages such as `steps[0](S001).kind: …`;
   - `semantic` (14.5/14.6): see the rules below;
   - `enrichment`: the enriched plan must still validate.
4. **Repair (14.4):** a repair turn is the base prompt, then the previous answer, then the exact error list and the
   remaining budget. The previous answer is echoed only if it parsed: it is redacted and re-serialised as compact
   JSON. Prose, markdown and unparseable text are never echoed. At most `max_repair_attempts = 2` repairs in total
   (3 calls). After that the planner raises `PlannerError` and emits `planner.failed`. If an answer was cut off
   (`finish_reason == "length"`), the error list says so.
5. **Persist** in one transaction under `SELECT … FOR UPDATE` of the job. The plan check is repeated, so a
   concurrent planner gets `PLAN_EXISTS`. The transaction writes:
   - one `plans` row and `plan_versions` v1 (`source` is `planner`, or `fallback` when the gateway reported `fallback_used`), with `model_alias`, the enriched `plan_json`, the `validation_errors` history and `repair_attempts`;
   - `steps` with status `pending` and their `step_dependencies`;
   - `jobs.current_plan_version = 1` and the metadata key `planner_model_alias`;
   - the events `planner.plan.created` and `step.created` (one per step).

Step rows take `capability`, `acceptance` JSON, `constraints`, `repo_hints`, `allowed_new_paths`,
`forbidden_paths`, `network` and `risk` from the plan. `turn_budget = policies.coder.max_turns` applies to kinds
whose capability uses the `coder` model role, otherwise `0`. `max_attempts = policies.correction.max_attempts_per_step`.
`current_scope_version` stays `NULL`, because the scope engine (P15) creates scopes.

### Semantic rules (14.5 DAG, 14.6 dependencies)

- Kinds `plan` and `replan` are runtime-only. Every other kind must be available for the job, and its capability must be the configured `step_kind_capability`. Capabilities must exist in config and be available for the job.
- At most `max_steps` (30) new steps, counting research requests that are not yet covered. The complete merged plan has at most 60 steps.
- No duplicate work: the same kind with the same normalised goal or title is duplicate work. Repeating a completed step gets a hint to depend on it instead.
- Information steps (`research`, `discover`, `inventory`) must have a dependant whenever the plan contains other work. Otherwise they are unreachable.
- `review` and `verify` steps must depend on the steps they check.
- A research step must not depend on mutating steps: research runs before the work that needs it.
- `research_needed` requires the `research` capability.
- Grounding (no invented paths):
  - a `repo_hint` is either a repository-relative path or glob, or a symbol;
  - a path must exist among the known paths. These are the explicit list, the inventory's `files` and `paths`, every path-shaped string elsewhere in the inventory (values and keys), the retrieved-context paths and the files of `existing_tests` (`a.py::test_x` counts as `a.py`);
  - a symbol must be a known symbol or appear as a whole word in the provided context;
  - absolute paths and `..` are rejected, and so are policy-forbidden paths.
- Scope stays explicit: catch-all patterns made only of wildcards and slashes (`*`, `**`, `**/*`) are rejected as `repo_hints` and as `allowed_new_paths`.
- Acceptance sanity:
  - remote mutating kinds (`ssh`, `deploy`) need substantive model-provided acceptance;
  - regexes must compile;
  - presence, absence, schema and diff paths must be repository-relative;
  - commands must not match `policies.commands.forbidden_patterns`.
- Network (Bauplan §30, "Netzwerk nur bei Step Capability"): `network: true` on a step, or on command evidence, is only valid when the step's capability has `network: true`.

Steps that a replan keeps as completed (`preserved`) take part in the structural checks only.

### Deterministic enrichment

- **Research requests (14.9):** each `research_needed` entry that no research step covers yet becomes a `research`
  step. Duplicates are matched by normalised containment. The new step gets the next free `S###` id (ids reserved
  by the replan are skipped), the research capability and that capability's network flag. Every *first* new work
  step (a non-information step with no new non-information dependency) depends on all generated research steps,
  so research is ordered before all new work.
- **Acceptance (14.8):** applies to workspace-mutating kinds (`implement`, `database`, `docker`, `documentation`).
  - If the model gave no substantive criterion, two are added: `DiffEvidence(must_change=<path-like repo_hints, directories as dir/>, falling back to allowed_new_paths; must_not_change=forbidden_paths; allow_empty=false)` and, when a test command was detected, `TestEvidence(command, framework)`.
  - `ScopeEvidence` and `SecurityEvidence` are always added when missing.
  - `ssh` and `deploy` work on remote systems, so the model must supply their checks (see the semantic rules).
- **Risk (14.7):** risk is never lowered below the model's value. `deploy`, `ssh` and `database` are at least
  `medium` (`RiskPolicy.min_risk_by_kind`). An `implement` step without test evidence that touches at least
  `many_paths_threshold` (6) paths becomes `high`. Touched paths are counted as follows: exact hints once,
  glob and directory hints as the number of known paths they match, plus `allowed_new_paths`. Job-level
  `risk_policy` overrides can only raise floors or lower the threshold. Unknown kinds or fields are rejected.

Every enrichment is recorded in `PlanResult.notes` and in the `planner.plan.created` / `replan.created` payloads.

## Replanning flow (`replan`)

1. **Limit and state (24.1):** lock the job. Enforce `max_replans_per_job` (`ReplanLimitReached`). Load the plan,
   the current version, the active steps and their dependency keys. The failed step must be an active step,
   otherwise the call fails with `PLAN_CHANGED`.
2. **Failure package (24.1):** built from PostgreSQL only, redacted and size-bounded. Outputs are truncated from the
   tail and never mid-line. It contains:
   - the trigger (reason code, detail, failed step key);
   - the current plan JSON;
   - the completed steps (key, goal, dependencies, summary from `steps.result` or the latest attempt);
   - the failed step (goal, hints, acceptance, attempt and correction counts, error code and message, latest attempts);
   - deterministic evidence: the trigger evidence, the failed checks of the latest verification run, test runs with output tails, failing command runs (stderr/stdout tails), the latest scope contract decision, review findings;
   - the other open steps and the job's research syntheses.

   Model reasoning is never stored, so it can never appear here. The replan payload also carries the original
   goal and the §15 sections at a reduced share of the budget. `replan.started` is emitted.
3. **Gemma replan (24.2):** the `planner` alias with the `ReplanContract` schema and the same 2-repair budget.
   The validation chain is:
   - the `ReplanContract` schema;
   - the rerun rule: listing a completed id requires a non-empty `rerun_reason`, and `rerun_reason` on a non-completed id is an error;
   - the merged plan (kept completed steps first) through `PlanContract`, so new dependencies on completed ids resolve and cycles are detected;
   - the semantic rules with `preserved = kept completed keys`;
   - enrichment (reserved ids are the active step keys);
   - the no-blind-repeat rule: for `scope_unavailable`, `stagnation`, `test_architecture_conflict` and `repeated_verifier_failure`, a new step identical to the failed step (same kind, capability, normalised goal, hints, dependencies and acceptance) is rejected.
4. **Persist (24.3-24.6)** in one transaction under the job lock. If the version, replan count or completed set
   changed while the model ran, the call fails with `PLAN_CHANGED` and nothing is written.
   - **24.3:** the next `plan_versions` row, with `source` `replanner` or `fallback`, `reason` set to `reason_code: detail` plus a line per rerun, and the validation history. `plans.current_version`, `jobs.current_plan_version` and `jobs.replan_count += 1` are updated. `jobs.metadata.last_replan` records the version and reason code.
   - **24.4:** completed steps stay as rows with status `completed` and `superseded=false`. They are never re-run unless the model gives a `rerun_reason`. In that case the old row becomes `superseded=true` and keeps `completed` as history, and a new `pending` row is created. Every other active step becomes `superseded=true`. Pending and in-flight steps go to `cancelled` through the step state machine (`step.transition` event). `failed` steps keep `failed` as history, because the state machine forbids `failed -> cancelled`.
   - **24.5:** dependencies of new steps on kept completed keys are mapped to the existing rows. Dependencies on a re-run key are mapped to the new row.
   - **24.6:** new and changed steps have no scope (`current_scope_version = NULL`). The scope engine creates one. Active `scope_contracts` of superseded steps are set to `superseded`, with the reason `superseded by plan version N (reason_code)`. Scopes of kept steps stay unchanged.
   - `replan.created` lists the preserved, rerun, superseded and new steps, and the number of superseded scopes.

## Events

| Event | Severity | Payload (never prompt or answer content) |
|---|---|---|
| `planner.invoked` | info | mode, alias, max repair attempts, input statistics + SHA-256 |
| `planner.repair` | warning | mode, attempt, phase, exact (redacted) error list, repairs remaining, served alias, fallback flag |
| `planner.fallback.used` | warning | mode, requested alias, served aliases, attempts (also a log warning and job metadata `planner_model_fallback=true`) |
| `planner.plan.created` | info | version, source, model alias, step keys, repair attempts, research steps, enrichment notes |
| `step.created` | info | step key, title, kind, capability, risk, dependencies, plan version, acceptance types |
| `planner.failed` | error | mode, error code, message, attempts/history (errors only), fallback flag, reason code |
| `replan.started` | info | reason code, failed step, from version, replan count/limit, completed keys, input statistics |
| `replan.created` | info | from/to version, source, preserved/rerun/superseded/new steps, scopes superseded, notes |

## Security and privacy

- Prompt input is redacted with `hermclaw.core.redaction.DEFAULT_REDACTOR`. This covers the whole job section, the inventory, context snippets, research, constraints, tests and the failure package.
- Validation errors are redacted before they are stored, emitted or sent back to the model. So is the echoed answer.
- Only final content is parsed. The gateway returns the length of hidden reasoning, never its text, and an answer without content is an error. No chain of thought is stored or displayed.
- Path hygiene uses `contracts.scope.normalise_path` (no absolute paths, no `..`). Glob semantics come only from `hermclaw.scope.guard.path_matches` / `any_match`.
- Model-proposed commands are checked against the policy's forbidden patterns. No command is executed by the planner.
- All database access goes through the SQLAlchemy ORM. Concurrent planning and replanning are serialised through the job row lock, and the persist step re-checks the snapshot.

## Configuration

- `models.by_role("planner")`: alias, `context_tokens`, `max_output_tokens`, `temperature`, `timeout_seconds`. The prompt budget is (context - output) x 3.2 chars per token x 0.85 safety factor, minus the system prompt.
- `capabilities.capabilities[*]` (`name`, `network`, `model_role`, `worker_kind`, `description`) and `capabilities.step_kind_capability`.
- `policies.correction.max_replans_per_job` and `max_attempts_per_step`, `policies.coder.max_turns`, `policies.scope.always_forbidden`, `policies.commands.forbidden_patterns`.
- `PlannerSettings`: `max_repair_attempts=2`, `max_steps=30`, `many_paths_threshold=6`, `max_echo_chars`, `max_errors_reported`.

## Failure behaviour

| Situation | Behaviour |
|---|---|
| Invalid output (parse, schema, semantic, enrichment) | repair turns with the exact error list. After 2 repairs: `PLANNER_INVALID_OUTPUT` and `planner.failed`, with no plan rows written |
| Gateway fallback (12B) | plan accepted. `source=fallback`, job metadata, `planner.fallback.used` warning and a log warning. There is no silent model switch |
| Gateway or model failure | propagates unchanged. `planner.failed` carries the error code |
| Concurrent initial planning | the first commit wins. The other planner gets `PLAN_EXISTS` and `planner.failed` |
| Concurrent replan, or a step completed while replanning | `PLAN_CHANGED`. Nothing is written, and the orchestrator may retry with fresh state |
| Replan budget exhausted | `REPLAN_LIMIT_REACHED` before any model call |
| Event store unavailable while reporting a failure | logged. The original error is re-raised |

## Tests

All tests use the scripted fake `ChatModel` in `tests/unit/test_planner_support.py`. It covers five unrelated
fixtures: a Python FastAPI service, a PHP application, a React/TypeScript app, a YAML-only config repository and a
Linux administration task without a repository. Everything else is production code against real PostgreSQL.

- `tests/unit/test_planner_prompt.py`: truncation, budgets, redaction, payload keys, system prompts.
- `tests/unit/test_planner_loop.py`: structured call, repair budget, echo hardening, redaction, truncation hint.
- `tests/unit/test_planner_validation.py`: parsing, schema, semantic rules, grounding sources, catch-alls, network.
- `tests/unit/test_planner_enrich.py`: acceptance generation, risk floor and path counting, risk policy, research steps.
- `tests/integration/test_planner_repos.py`: plans for all five fixtures, DB rows and events, the repair loop, failure after 2 repairs, fallback, conflicts and races.
- `tests/integration/test_planner_replan.py`: preservation and supersede, failure package, rerun reason, no blind repeat, research in replans, limit, conflicts and races, fallback.
