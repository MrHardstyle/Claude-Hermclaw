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
ContextBuilderConfig.from_config(HermclawConfig, role="coder", **overrides)
ContextReport.to_event_payload() -> dict          # caller decides the event type
await record_context_report(session, report, event_type=..., job_id=, step_id=, attempt_id=, source_id=)
```

Dependencies: only the shared protocols `RepoContextProvider` (`inventory_summary`, `context_for`, `search`,
`read`) and `GitReader` (`status`, `changed_files`, `diff`), `ScopeContract`, the acceptance evidence union,
`hermclaw.scope.guard.any_match` (the single glob authority), `scope.engine.literal_path` and
`scope.expansion.is_test_path`.

## Messages (16.1)

* **system**: `SYSTEM CONTRACT` (runtime-owned rules, never truncated), `AVAILABLE TOOLS`, `COMPLETION CONDITIONS`
  and the fixed `RESPONSE PROTOCOL`: exactly one JSON object
  `{"tool": ..., "args": {...}, "status": "...", "decision": "..."}`.
* **user**: `STEP GOAL`, `SCOPE`, `CONSTRAINTS`, `ACCEPTANCE`, `CURRENT REPO FACTS`, `RELEVANT CODE`,
  `RELEVANT TESTS`, `CURRENT DIFF`, `LATEST FAILURE`, `SHORT STEP HISTORY`, then the closing line
  "Respond now with exactly one JSON action for turn N of M."

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

Default shares: system 3 %, goal 5 %, scope 4 %, constraints 3 %, acceptance 5 %, repo facts 3 %, **code 25 %**,
**tests 10 %**, diff 10 %, failure 12 %, history 6 %, tools 11 %, completion 3 %.

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
| test files named in acceptance `test`/`command` evidence: head | 2.0 | tests |
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
`min_snippet_cost` remains, otherwise skipped. Output is grouped by file (best rank first), ascending lines.

## Error preservation (16.5)

The latest failure is shown verbatim when it fits. Otherwise the first error line(s) (generic error markers plus
up to 6 following detail lines) and the final summary lines of test/build output (pytest/unittest/jest/go/cargo/
npm/make patterns plus the last non-empty line) are always kept, then head and tail lines alternately; every gap
is replaced by `[… n chars omitted …]` with the exact number of omitted characters. Only a single line larger
than the whole budget is clipped (marked with `…`). Correction evidence (verifier failures, review findings
sorted blocker → major → minor, with suggested fixes) shares the LATEST FAILURE budget (≤ 35 % reserved for it
when a failure text exists).

## Tool summary (16.6)

The last `history.full_turns` (6) turns are one line each: tool, args digest, `ok`/`FAILED [error_code]`, result
digest, changed paths. Older turns become `turns a-b (summary): tool xN (k ok, m failed: code xN)`, the last
failure per tool and all files changed so far. On budget pressure fewer turns are shown in full, then digests are
shortened. `TurnRecord` has no reasoning field; `strip_reasoning` removes `<think>`/`<thinking>`/`<reasoning>`/
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
