# Context Builder (P16)

`hermclaw/context_builder/` – builds the context of **one** coder turn from persistent state (Bauplan §18
"Context Builder", Full-Build-Prompt §15, Phase 16; steps 16.1–16.9).

> Persistent state → fresh context per turn. Never the whole chat, never the whole repository.

Every turn the runtime (coder loop, P19) passes the step contract, the workspace, the compact tool history and
the latest failure; the builder collects repository facts, ranked code and test snippets and the current diff,
fits every section into a fixed token budget and returns exactly two chat messages plus a telemetry report. The
output is deterministic for equal input (stable ordering, no clocks, no randomness) – the report carries a
SHA-256 fingerprint of the messages.

## Modules

| Module | Content | Steps |
|---|---|---|
| `sections.py` | `SectionName` (13 sections), message layout, fixed system contract, response protocol, `RenderedSection` | 16.1 |
| `tokens.py` | `estimate_tokens`, `char_cost`, exact integer cost arithmetic (3.2 chars/token, D-008) | 16.2 |
| `budget.py` | `SectionBudgets` (fixed shares), `plan_budget` → `BudgetPlan`, `split_elastic` (redistribution) | 16.2 |
| `snippets.py` | `Snippet`, ranking key, `dedupe_snippets` (merge/dedup), `pack_snippets` (first-fit + clipping), fences | 16.3, 16.4 |
| `failure.py` | `preserve_failure` (verbatim / head+tail with markers), `first_error_line`, `truncate_middle` | 16.5 |
| `history.py` | `TurnRecord`, `render_history` (full digests + summary), `strip_reasoning` | 16.6 |
| `diff.py` | `split_diff`, `render_diff` (per-file water-filling truncation, exclusions) | 16.7 |
| `render.py` | renderers for goal, scope, constraints, acceptance, repo facts, failure + correction evidence, tools, completion; `ToolPromptSpec`, `CorrectionItem` | 16.1 |
| `builder.py` | `ContextBuilder`, `TurnContextInput`, `StepBrief`; relevance gathering (16.3), tests (16.8) | 16.1–16.8 |
| `report.py` | `ContextReport`, `SectionReport`, `DroppedItem`, `BuiltContext`, `record_context_report` | 16.9 |
| `config.py` | `ContextBuilderConfig` (+ `from_config`, `from_profile`) | 16.2 |

## Public interfaces

```python
ContextBuilder(repo: RepoContextProvider, git: GitReader, config: ContextBuilderConfig | None = None,
               *, redactor: Redactor | None = None)
await ContextBuilder.build(inp: TurnContextInput) -> BuiltContext       # BuiltContext(messages, report)

TurnContextInput(step: StepBrief, workspace: WorkspaceHandle, turn: int, max_turns: int,
                 tools: Sequence[ToolPromptSpec | {name, description, args_schema|parameters}] = (),
                 completion_contract: str = "", history: Sequence[TurnRecord] = (),
                 latest_failure: str | None = None, correction: Sequence[CorrectionItem] = ())
StepBrief(goal, kind, title="", step_key="", constraints=(), acceptance=(), scope: ScopeContract | None, repo_hints=())
StepBrief.from_contract(StepContract)
TurnRecord(turn, tool, args_digest="", ok=True, result_digest="", error_code=None, mutated_paths=())
TurnRecord.from_mapping(row)                     # unknown keys (status/decision/reasoning …) are ignored
CorrectionItem(source, label, message, path="", suggested_fix="")
CorrectionItem.from_verification(VerificationReport) / .from_review(ReviewContract)   # blocking failures / severity order
# from_review clips summary, evidence and fix to 800 chars each and cuts untagged model deliberation (BUG-332)
ContextBuilderConfig.from_config(HermclawConfig, role="coder", **overrides)
ContextReport.to_event_payload() -> dict          # caller decides the event type
await record_context_report(session, report, event_type=..., job_id=, step_id=, attempt_id=, source_id=)
```

Dependencies: only the shared protocols `RepoContextProvider` (`inventory_summary`, `context_for`, `search`,
`read`) and `GitReader` (`status`, `changed_files`, `diff`), `ScopeContract`, the acceptance evidence union,
`hermclaw.scope.guard.any_match` (the single glob authority), `scope.engine.literal_path` and
`scope.expansion.is_test_path`.

## Messages (16.1)

* **system**: `SYSTEM CONTRACT` (runtime-owned rules, never truncated; includes the rule to parse JSON/TOML/CSV/INI/XML with the language's own parser and to read written output back with it, BUG-245), `AVAILABLE TOOLS`, `COMPLETION CONDITIONS`
  and the fixed `RESPONSE PROTOCOL`: exactly one JSON object
  `{"tool": ..., "args": {...}, "status": "...", "decision": "..."}`; then the stable sections `STEP GOAL`, `SCOPE`,
  `CONSTRAINTS`, `ACCEPTANCE` – the same for the whole attempt (D-026; before, they were a user message of their own,
  D-021).
* **user (files)**: `CURRENT REPO FACTS`, `RELEVANT CODE`, `RELEVANT TESTS` – they change only when a file, a read or
  the correction evidence changes (D-026). The repository facts list the files and grow with every new one, so they
  open this message: at the end of the system message they voided its checkpoint on every new file (ladder A10 #3 on
  rc80). The snippets come from the targets, acceptance tests, reads of the attempt, dependency files, the correction
  evidence and searches for title and goal; their budget is the base share plus what the system and stable sections
  leave unused, less `failure_code_share` (0.2) – kept also while no run fails, else every passing run changed it.
  The context report gives each section a short `digest` of its body and the `labels` of the shown snippets, so which
  part changed from turn to turn is visible without the content.
* **user (turn)**: `CURRENT DIFF`, `LATEST FAILURE`, `SHORT STEP HISTORY`, then the
  closing line
  "Turn N/M, R left. Reply with one JSON action." (with two turns or fewer left: "Almost out of turns: complete_step,
  block_step or request_replan.").
* **Three cache levels (D-026):** llama.cpp keeps a checkpoint where the system message ends and where the last
  message starts. Measured on .224 (qwen3.6-35b, 2026-10-23, ~18k-token prompts, 4.4k system, 4.3k stable, 6.9k
  files, 2.3k turn): stable part as its own user message – 22 s prefill whatever changed; stable part in the system
  message and files as their own message – 6.4 s when the files are unchanged, 23 s when they changed. In ladder A10
  #2 the files part had changed in 75 % of the turns: the code at the latest failure (traceback regions) and the
  budget left by diff, failure and history moved it. Both now go to the turn message: `LATEST FAILURE` ends with
  "Code at the latest failure" (regions of the latest failure's references that `RELEVANT CODE/TESTS` do not
  already show whole, packed into the reserve plus what diff, failure and history leave unused), and the retrieval
  query no longer contains the first error line.
* **Stable prefix (D-021):** the system message and the stable user message are the same in every turn of an attempt,
  so llama.cpp reuses its cached prompt prefix and only processes what changed. They are separate messages because
  llama.cpp keeps a cache checkpoint for a hybrid model (Qwen3.6: recurrent layers, the cache cannot be cut at any
  position; `--checkpoint-min-step` 8192) only where the last message of a prompt starts. Measured on .224
  (qwen3.6-35b, 2026-10-21, ~6.5k-token prompts): turn number at the top 0 tokens from the cache, 24.5 s prefill;
  one user message with a changed middle 2,201 (the system message), 15.6 s; stable part as its own message 3,682,
  10.5 s; only the tail changed 6,093, 2.2 s. The turn count is
  only in the closing line; what the attempt changed so far is at the head of `CURRENT DIFF`
  (`render.render_progress`): "Changed vs base (N): …" with git status codes, "New files written" / "Not written
  yet" for the scope's new files (BUG-228) and "Acceptance i — NOT YET CHANGED: … / met (already changed)" per diff
  criterion (BUG-182). Without a diff the section carries these lines alone when something changed or a criterion
  waits for a change. The tool list is the same in every turn: checks and reads withheld after a refused repeat are
  named in the turn notice, not removed from the catalogue (three catalogue versions in 15 of 47 turns of ladder A10
  #4 voided the cached system message each time, D-026).
  `STEP GOAL` also carries the job request (job title and prompt, at most half of the section, cut in the middle)
  below the step goal: the planner's goal summarises the job and may lose details the review checks (BUG-212).
  Below that come the active lessons and conventions that fit goal and job (`policies.memory.coder_start_entries` at
  most, each at least `coder_start_min_score` similar; fetched once per attempt, so the prefix stays stable), whole
  entries nearest first in what goal and request leave of the section (12 %) – one that does not fit is left out, the
  next still comes (BUG-325, BUG-328, BUG-330).

Mandatory sections (always rendered): SYSTEM CONTRACT, STEP GOAL, SCOPE (read-only notice without a scope),
AVAILABLE TOOLS, COMPLETION CONDITIONS. All others are omitted when empty (`omitted_reason="empty"` in the
report). Repository content (code, tests, diff, failure output) is fenced with a fence longer than any backtick
run in it, so it can never pose as a section heading or instruction.

## Budget (16.2)

```
total     = context_tokens - max_output_tokens - max(safety_margin_tokens, ceil(context_tokens * safety_margin_fraction))
framing   = estimate(all headings + separators + protocol + closing line) + 2 * 8 (chat template) + 2 (rounding)
available = floor((total - framing) * 3.2)  character equivalents
section   = floor(available * share)        # SectionBudgets, defaults sum to 1.00
```

Default shares: system 3 %, goal 12 % (7 % until BUG-330: goal, job request and the knowledge that fits the step), scope 4 %, constraints 8 % (including the cited findings of research steps, up to
2400 characters each, BUG-231), acceptance 5 %, repo facts 3 %, **code 23 %**, **tests 10 %**, diff 6 %, failure 7 %,
history 5 %, tools 11 %, completion 3 %.

Token estimate (D-008): 3.2 characters per token, computed exactly with integers (`ceil(cost * 5 / 16)`);
non-ASCII code points cost 4 characters (≥ 1 token each) to stay conservative. Every renderer guarantees
`cost(body) <= budget`; budget not used by a section is redistributed to RELEVANT CODE / RELEVANT TESTS (split by
their shares; unused code budget then flows to tests and spare test budget back to code if code had to drop
snippets). A system contract larger than its share is never truncated – the deficit is taken from the elastic
pool (configs where it cannot be absorbed are rejected). The sum of the two message estimates plus chat-template
overhead is provably `<= total` (checked again at the end; `CONTEXT_BUDGET_EXCEEDED` would be an internal error).

The coder budget comes from `models.by_role("coder")` (`context_tokens`) and
`max(profile.max_output_tokens, policies.coder.max_output_tokens)`; the per-read cap from
`policies.coder.tool_output_chars`; exclusions from `policies.scope.always_forbidden`.

## Relevance (16.3) and tests (16.8)

Candidates (score → rank):

| Source | Score | Section |
|---|---|---|
| file:line references in the latest failure / correction evidence (`File "…", line N`, `path:N`, `path::test`), region ±`failure_region_lines` | 3.0 | code or tests |
| literal scope target files: head (`target_head_lines`, tests `test_head_lines`) | 2.0 | code or tests |
| files the completed dependency steps committed (`StepBrief.dependency_files`, transitive, nearest first, then the job's other completed steps newest first; origin `dependency`; also named in STEP GOAL, BUG-242/BUG-250) | 2.0 | code or tests |
| test files named in acceptance `test`/`command` evidence: head | 2.0 | tests |
| files read in this attempt (origin `read`, most recent first, up to 6): a `read_range` with its range (a repeated one as the lines it brought, from its span header, BUG-342), a `read_file` as the page it showed (the span header `[path: lines X-Y of N…]` of a clipped read, BUG-266), otherwise from line 1; each up to `max_snippet_lines` | 2.0; the latest read 3.5 – first in its section, clipped rather than left out (BUG-367); the latest read of another file 3.4 (BUG-368) | code or tests |
| `search(stem)` for each literal non-test target (`__init__`/`index` → package name) and code-like identifiers of the goal (`snake_case`, `camelCase`, `name(`), test paths only | 1.0 + normalised score | tests |
| `context_for(title + goal + first error line)` | normalised score (+1.0 inside a target file) | code or tests |

Ranges are always read fresh through `RepoContextProvider.read` (the index may be stale after edits); a
provider snippet is only a fallback and then never merged line-wise. Paths are normalised
(`normalise_path`); absolute paths are only accepted inside the workspace; `..`, directories and
`exclude_globs` (secret files, `.git/**`) are dropped (`excluded`) and never read. Limits: `max_target_files`,
`max_acceptance_test_files`, `max_failure_refs`, `max_test_queries`, `max_hits_per_query`, `max_code_snippets`,
`max_test_snippets` (`limit`).

## Deduplication (16.4)

Identical ranges are folded (`duplicate`, best score/origins kept), overlapping or nearly adjacent ranges
(`merge_gap_lines`) of the same file are merged line-accurately (the union is re-read when lines are missing;
`max_snippet_lines` caps a merge), non-exact snippets contained in an exact range are dropped (`contained`), and
identical content is never shown twice, e.g. vendored copies (`duplicate_content`). Packing is first-fit in rank
order; the snippet that does not fit is clipped to its head lines with a `read_range` hint when at least
`min_snippet_cost` remains, otherwise skipped. A merged snippet whose best-ranked part ranks above the rest and starts
further down (a read merged into its file's head) keeps that start as `focus` and is clipped from there (BUG-368).
Output is grouped by file (best rank first), ascending lines.

## Error preservation (16.5)

The latest failure is shown verbatim when it fits. Otherwise the first error line(s) (generic error markers plus
up to 6 following detail lines) and the final summary lines of test/build output (pytest/unittest/jest/go/cargo/
npm/make patterns plus the last non-empty line) are always kept, then head and tail lines alternately; every gap
is replaced by `[… n chars omitted …]` with the exact number of omitted characters. Only a single line larger
than the whole budget is clipped (marked with `…`). Correction evidence (verifier failures, review findings
sorted blocker → major → minor, with suggested fixes) shares the LATEST FAILURE budget (≤ 35 % reserved for it
when a failure text exists).

The latest successful output the files message does not show comes last in LATEST FAILURE, head first and titled
as a success (BUG-363): `recall`, `request_research`, `run_command`, `git_diff`, `find_text` and the like, when it is
longer than the history digest. Reads (shown in RELEVANT CODE/TESTS), writes and `complete_step` never count. It
lasts until a newer one replaces it; a file change ends it, except for knowledge (`recall`, `request_research`).
With a failure text it gets at most 35 % of the budget, after the failure and the correction evidence.

## Tool summary (16.6)

The last `history.full_turns` (6) turns are one line each: tool, args digest, `ok`/`FAILED [error_code]`, result
digest, changed paths. Older turns become `turns a-b (summary): tool xN (k ok, m failed: code xN)`, the last
failure per tool and all files changed so far. On budget pressure fewer turns are shown in full, then digests are
shortened. The history is rendered after RELEVANT CODE/TESTS are packed: a successful read whose lines a packed
snippet shows in full reads `ok: the lines read are shown in full in RELEVANT CODE/RELEVANT TESTS above` instead of
its clipped head, which looked like a cut-off output and was read again (BUG-338). A snippet the packer clips is kept as the part it shows (its range ends where the clip ends), and `BuiltContext.shown` lists these parts; the loop hands them to the tool engine every turn, whose read guards refuse a re-read only where the context shows it (BUG-362). `TurnRecord` has no reasoning field; `strip_reasoning` removes `<think>`/`<thinking>`/`<reasoning>`/
analysis-channel markup from every digest and review text.

## Current diff (16.7)

`GitReader.diff(workspace, max_bytes=diff_max_bytes)` is split per file; file diffs of excluded paths (old or new
name) are removed (`[changes to n excluded path(s) not shown]`). If everything fits it is shown completely;
otherwise the budget is water-filled (small diffs complete, large ones keep their header and first hunks, then
`[… n chars of the diff of <path> omitted; use git_diff with paths=["<path>"] …]`). Files that cannot get
`min_diff_file_cost` are listed by name.

## Telemetry (16.9)

`ContextReport`: budget figures (context window, output reserve, safety margin, total, framing, redistributed),
estimated prompt tokens, per section (budget, estimate, chars, truncated, items included/dropped, omitted reason),
dropped items with reasons (labels only – never content), merged-snippet count, provider warnings, fingerprint.
`to_event_payload()` is compact (≤ 40 dropped items, ≤ 20 warnings), JSON-safe and redacted; its keys avoid the
substring `token` because the shared event redactor masks such keys. The caller records it (typically with
`model.invocation.started` of the coder turn) – `record_context_report` appends it as
`{"context": payload}` with `source_type="context_builder"`.

## Failure behaviour

* Provider errors and timeouts (`provider_timeout_seconds`, per call) never fail a turn: the data is left out and
  a redacted warning is recorded. Unreadable failure references (library files etc.) are expected and silent.
  Garbage results (wrong types, NaN scores, invalid line numbers) are ignored or sanitised.
* Provider calls run concurrently with at most `max_concurrency` in flight (set 1 for providers that are not
  concurrency-safe); results are combined in a fixed order, so concurrency never changes the output.
  `asyncio.CancelledError` propagates.
* Secrets: every input (goal, constraints, failure, correction, history, inventory, code, diff, completion text)
  is redacted with `hermclaw.core.redaction` (plus caller literals via `redactor=`) before budgeting; redaction
  cannot be disabled. Secret files (`exclude_globs`) are never read or listed.
* Invalid input (`turn < 1`, `max_turns < 1`, empty goal) raises `ValidationFailed`; a context window that
  cannot hold a prompt, invalid shares or limits raise `ConfigError` at configuration time.
