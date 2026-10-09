# Heavy Review (P22)

`hermclaw/review/` – Bauplan §3.4 "Heavy Reviewer", §22 "Review", Full-Build-Prompt §19 "Heavy Review", Phase 22
(steps 22.1–22.6). After the deterministic verifier passed, **Qwen3.8 27B** (model role `heavy`, alias
`heavy-review`, 24K–32K context, `think: false`) reviews exactly one completed plan step: diff review, risk review,
architecture/constraint violations, plausibility of the verifier results, structured correction recommendations.

Invariants (deterministic, enforced by the runtime – never left to the model):

- `major`/`blocker` finding → the verdict can never be `pass` (`enforce_review_invariant` of the contract);
- the verifier did not pass → the review can never pass either (facts beat opinions);
- every failure (invalid output after the gateway's repairs, timeout, model/infra error, missing heavy profile,
  unreadable diff, internal error, cancellation) is persisted as `status='error'` with the **fail-closed** verdict
  `fix_required` and a clear reason – no code path turns an error into a pass;
- a mutating step kind (`MUTATING_STEP_KINDS`) without change evidence (empty diff *and* no executed-command log)
  fails closed (`REVIEW_EMPTY_DIFF`) without calling the model;
- model reasoning is never requested, stored, logged or displayed: unknown answer fields (`reasoning`, `thinking`, …)
  are dropped, `<think>…</think>`-style markup is stripped from every text field;
- secrets never reach the prompt, a database row, an event or a correction request (`hermclaw.core.redaction` on
  every repository-derived text; files matching `policies.scope.always_forbidden` – `.env`, keys, `.git/**` – are
  listed by name only, their content is withheld, also for repo-context snippets);
- no task-, project- or benchmark-specific rule exists in the package (generic heuristics only).

## Modules

| Module | Content | Steps |
|---|---|---|
| `types.py` | `ReviewInput`, `ReviewOutcome` (`passed`, `verdict`, `blocking_findings`), `CodeSnippet`, `ReviewSettings`, error codes, override reasons, severity ranking | – |
| `prompt.py` | `REVIEW_SYSTEM_PROMPT`, `build_review_prompt` (context-window budget, fixed/variable sections, shrink loop), section renderers | 22.1 |
| `diff.py` | `split_diff` (git headers, quoted/space paths, renames, binaries, truncation notes), `fair_allocation` (max–min fair), `render_diff` (per-file budget, withheld/generated files, file limit) | 22.1 |
| `severity.py` | `ReviewDraft` (schema handed to `ChatModel.structured`, identical JSON schema to `ReviewContract`, tolerant parsing), `normalise_severity`/`normalise_verdict`, `normalise_findings` (canonical paths, `path:line`, de-duplication, blocker → major → minor), `redact_review` | 22.3, 22.4 |
| `invariant.py` | `apply_review_invariants(review, verifier_passed)` → `InvariantResult(review, raw_verdict, overridden, reasons)` | 22.5 |
| `reviewer.py` | `HeavyReviewer(chat, sessionmaker, config)` – `review(ReviewInput)`, `fail_closed(...)`, `should_review`, `profile()`; persistence + events | 22.2–22.5 |
| `correction.py` | `build_correction_request(...)` → `CorrectionRequest` (`to_dict`/`from_dict`/`items`/`render`) | 22.6 |
| `policy.py` | `should_review(policy, step_kind, verifier_passed)`, `requires_change_evidence(step_kind)` | – |
| `workspace.py` | `WorkspaceReviewer(reviewer, git: GitReader, repo: RepoContextProvider | None)` – the `ReviewerPort` used by the implement handler; `is_test_path` | – |

## Interfaces

```python
reviewer = HeavyReviewer(chat: ChatModel, sessionmaker, config: HermclawConfig | None = None, settings: ReviewSettings | None = None)
reviewer.should_review(step_kind: str, verifier_passed: bool) -> bool
await reviewer.review(ReviewInput(job_id, step_id, attempt_id, goal, step: StepContract, diff: str,
                                  verification: VerificationReport, snippets=(), command_log=(), changed_files=None)) -> ReviewOutcome
await reviewer.fail_closed(job_id=…, step_id=…, attempt_id=…, step_kind=…, error_code=…, reason=…) -> ReviewOutcome

adapter = WorkspaceReviewer(reviewer, git_reader, repo_context)          # implements coder.handler.ReviewerPort
adapter.should_review(step_kind, verifier_passed) -> bool
await adapter.review(step, workspace, verification, job_id=…, step_id=…, attempt_id=…) -> ReviewContract   # effective, fail-closed
await adapter.review_outcome(step, workspace, verification, job_id=…, step_id=…, attempt_id=…) -> ReviewOutcome

build_correction_request(step_id=…, attempt_id=…, verification: VerificationReport | None, review: ReviewOutcome | ReviewContract | None,
                         step: StepContract | None = None, verification_run_id=None, limits=None) -> CorrectionRequest
```

`ReviewOutcome.review` is always the *effective* contract (normalised, redacted, invariants applied).
`ReviewOutcome.passed` is true only for a completed, non-fail-closed review with effective verdict `pass`.

## Flow of `HeavyReviewer.review`

1. `review_runs` row (`status=running`, heavy alias) + `review.started` event, committed in its own transaction.
2. Deterministic pre-checks: mutating step without change evidence → `REVIEW_EMPTY_DIFF`; no enabled `heavy` profile →
   `REVIEW_PROFILE_MISSING`. The model is not called.
3. Prompt (22.1): job goal, plan step (key, title, kind, risk, capability, goal, constraints, acceptance criteria as
   compact JSON), explicit scope, verifier report (facts only: status, failed checks with evidence first, other checks,
   changed files), executed-command log (if any), size-budgeted diff, relevant code/test snippets. All
   repository-derived text is redacted and fenced with a fence longer than any backtick run inside it, so diff content
   can never close its block or pose as an instruction section. The system prompt makes the model a strict reviewer:
   findings must cite a repository path and concrete evidence, severity `minor|major|blocker`, output ONLY JSON per
   `ReviewContract`.
4. `ChatModel.structured(heavy_alias, messages, ReviewDraft, ctx=CallContext(purpose="review", …), max_repairs=2,
   max_tokens=profile.max_output_tokens, temperature=profile.temperature, timeout_seconds=min(profile, policy))`
   inside `asyncio.timeout(policies.review.timeout_seconds)` (22.2, 22.3).
5. Severity normalisation (22.4) → redaction → invariants (22.5).
6. One transaction: run finished (`status`, `verdict`, `raw_verdict`, `invariant_override`, `summary` = reason,
   `finished_at`), one `review_findings` row per finding, one `review.finding.created` event per finding and
   `review.finished`.

### Prompt budget (22.1)

`prompt_char_budget = (context_tokens − max_output_tokens − schema − template overhead − reserve) × 3.2 chars ×
0.9`. Fixed sections (goal/step/scope) are clipped per item until they take at most 40 % of the budget. The variable
part is shared: verifier ≤ 20 %, command log ≤ 15 %, snippets ≤ 25 % reserved (unused diff budget flows back to the
snippets), the diff gets the rest. Within the diff every changed file keeps its header (path, change kind, `+/-`
counts); bodies get a **max–min fair share** (small files complete, large files share the remainder, truncated at line
boundaries with an explicit `…[N more diff lines of <path> omitted]` marker; below 400 chars only the header plus an
omission note). Generated files (`policies.verifier.generated_file_globs`) are capped at 400 chars, protected files
are withheld, files beyond 80 are listed by name. The result is validated with `hermclaw.models.tokens.context_budget`;
if the conservative estimate does not fit, the budget shrinks (×0.85, ≤ 6 rounds). Prompt statistics are stored in
the `review.finished` event (`prompt`).

### Severity normalisation (22.4)

Before validation (wrap validator of `ReviewDraft`): verdict synonyms (`approved` → `pass`, `changes requested` →
`fix_required`, unknown → left alone so validation fails and the gateway repairs), field aliases (`issues`,
`file`, `description`, `fix`, `line`, …), severity synonyms (`critical`/`security` → blocker, `high`/`medium`/`warning`
→ major, `nit`/`low`/`info` → minor, **unknown → major**, fail-closed), textual "none"/"n/a" findings → empty list, other free text → one major finding, clipping to the contract limits, ≤ 50 findings
(most severe kept), unknown fields dropped (noted by *name* only). After validation: canonical repository paths (`./`,
`a/`/`b/` diff prefixes, `path:line` → evidence `line N: …`; absolute or traversing paths are removed from the
`path` field and only quoted in the evidence, so no later stage can open a file by them), de-duplication (same path + summary keeps the highest
severity), stable order blocker → major → minor. Normalisation notes go to the `review.finished` event.

## Correction request (22.6)

`CorrectionRequest` is the input format the correction pipeline (P23) passes to the next coder attempt:

| Field | Content |
|---|---|
| `step_id`, `attempt_id`, `review_run_id`, `verification_run_id` | traceability |
| `verifier_failures` | blocking `fail`/`error` checks in report order: `check_type`, `name`, `status`, `message`, `evidence` excerpt (stderr/stdout/output/… first, then compact JSON, redacted, ≤ 600 chars), `path`, `suggested_fix` |
| `review_findings` | blocker → major → minor (stable), de-duplicated among themselves (highest severity wins) and against the verifier |
| `required_changes` | one entry per verifier failure and per major/blocker finding (suggested fix preferred); `REVIEW_EMPTY_DIFF` adds "the previous attempt produced no changes"; a review error without other evidence is stated; minor findings are optional |
| `constraints` | step constraints, explicit scope summary, runtime rules (scope, no special cases, no test weakening, runtime-controlled Git) |
| `review_error`, `review_error_code` | fail-closed reason of an errored review |
| `items` (in `to_dict`) | flat `{source, label, message, path, suggested_fix}` list – the format `hermclaw.coder.handler.corrections_from_input` / `context_builder.CorrectionItem` read |

De-duplication rule: a review finding *restates* a verifier failure when the paths are compatible (equal or one
empty) and the finding names the check (`type:name`), contains the failure message (≥ 12 chars), is contained in the
failure message/evidence, or shares ≥ 60 % of its significant tokens with it. Restating findings are dropped (count
in `dropped_duplicates`); their suggested fix is kept on the verifier failure, so the coder sees each problem once,
with the fact first.

## Policy

`policies.review.required_for_kinds` (default `implement, database, docker, deploy, ssh`) – `should_review(kind,
verifier_passed)` is true only for listed kinds (case-insensitive) and, by default, only after the verifier passed: a
failed verification already yields deterministic correction evidence and can never pass review
(`ReviewSettings.review_failed_verification=True` reviews anyway for richer correction input).
`policies.review.timeout_seconds` (default 1200/1500) bounds the whole review including repairs.

## Events

| Event | Payload |
|---|---|
| `review.started` | `review_run_id`, `alias`, `step_kind`, `verifier_passed`, `changed_files`, `diff_chars`, `snippets` |
| `review.finding.created` | `review_run_id`, `finding_id`, `severity`, `path`, `summary` (≤ 500 chars); severity `warning` for major/blocker, `info` for minor |
| `review.finished` | `review_run_id`, `status`, `verdict`, `raw_verdict`, `invariant_override`, `override_reasons`, `fail_closed`, `error_code`, `reason`, finding counts, `alias`, `repair_attempts`, `normalisation_notes`, prompt statistics; severity `error` on error, `warning` on fix_required, `info` on pass |

## Workspace adapter

`WorkspaceReviewer.collect` (bounded by `ReviewSettings.context_timeout_seconds`): job goal (`jobs.title` +
`jobs.prompt`), `GitReader.diff(workspace, max_bytes=400 000)` + `GitReader.changed_files(workspace)` (committed,
uncommitted and untracked against `base_sha`), the attempt's `command_runs` (≤ 60, oldest first, redacted; fall back
to the step when no attempt id is known), and `RepoContextProvider.context_for(workspace, step title/goal/acceptance +
changed files)` snippets (tests first, protected or non-repository paths dropped, ≤ 12). A diff/DB failure fails the
review closed (`REVIEW_CONTEXT_ERROR`, persisted); a repo-context failure only drops the snippets.

## Failure behaviour

| Situation | Outcome |
|---|---|
| model answer invalid after `max_repairs` | `error`, `MODEL_OUTPUT_INVALID`, reason with attempt count and last validation error |
| overall timeout / gateway `ModelTimeout` | `error`, `REVIEW_TIMEOUT` |
| other `HermclawError` (model unavailable, residency, …) | `error`, the error's code, redacted message |
| unexpected exception (call or post-processing) | `error`, `REVIEW_INTERNAL_ERROR` (exception type only) |
| no enabled `heavy` profile | `error`, `REVIEW_PROFILE_MISSING`, model not called |
| mutating step without changes | `error`, `REVIEW_EMPTY_DIFF`, model not called, correction request demands changes |
| diff unreadable | `error`, `REVIEW_CONTEXT_ERROR` |
| task cancelled | run finished as `error`/`REVIEW_CANCELLED` (shielded), `CancelledError` re-raised |
| DB unavailable | exception propagates to the caller (never a pass); a run that was already started stays `running` (stale, visible in the UI) |

All error outcomes carry `verdict=fix_required`, `fail_closed=True`, no findings.

## Tests

- `tests/unit/test_review_severity.py` – severity/verdict synonyms, tolerant `ReviewDraft`, schema identity, repair
  triggers, reasoning stripping, caps, path canonicalisation/dedupe/order, redaction, invariants, policy, text helpers.
- `tests/unit/test_review_prompt.py` – sections and order, system-prompt rules, verifier facts, fair per-file budget,
  context fit (24K and 6K profiles), secrets/withheld paths, generated caps, fence escape, file limit, command log,
  diff parser (added/deleted/renamed/binary/quoted/space paths/truncation note), fair allocation.
- `tests/unit/test_review_correction.py` – ordering, dedupe against verifier facts (message, label, token overlap,
  path mismatch), dedupe among findings, empty-diff/error outcomes, caps + redaction, JSON round trip, item format,
  tolerant `from_dict`, rendering.
- `tests/integration/test_review_reviewer.py` (PostgreSQL) – pass, fix_required with rows/events in severity order,
  pass-with-major overridden, synonym normalisation before the invariant, failed verifier never passes, minor still
  passes, repairs, secrets/reasoning never persisted, concurrent reviews, policy.
- `tests/integration/test_review_workspace.py` (PostgreSQL + real git) – goal/diff/untracked/commands/snippets
  collection, `.env` withheld, attempt-scoped command log, unchanged workspace fails closed, unreadable diff fails
  closed, repo-context failure tolerated, test-path heuristic.
- `tests/failure/test_review_failures.py` (PostgreSQL) – invalid output, overall timeout, gateway timeout, model error
  (redacted), unexpected exception, empty diff, command evidence, non-mutating empty diff, missing profile,
  cancellation, explicit fail-closed run.

Live verification against the real Qwen3.8 27B on the model host is blocked by BLOCKER-001 (no target hosts in the
build environment); the gateway path is covered by the models component's tests.
