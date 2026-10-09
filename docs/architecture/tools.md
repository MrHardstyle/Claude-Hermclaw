# Tool Engine (P17)

`hermclaw/tools/` – the **only** way an LLM touches a workspace (Bauplan §19 "Coder Tool Loop", Phase 17, steps
17.1–17.13). The coder emits exactly one `CoderAction` per turn; `ToolEngine.execute` validates it, enforces the
step's policies, runs it against the workspace and returns a bounded, redacted `ToolResult`.

Invariants:

- reads are confined to the workspace (no `..`, no absolute paths, no symlink escape); `.git` internals and secret
  files (`policies.scope.always_forbidden`) are never readable, listable, searchable or diffable through tools;
- every file mutation (tool writes **and** command side effects) is checked with the step's `ScopeGuard` – the single
  path-permission authority; there is no other glob logic in this package;
- there is no git mutation tool; `git commit/push/checkout/reset/stash/add/…` inside commands is refused and any change
  to git metadata (HEAD, refs, config, hooks, index entries) is reverted. Commits belong to gitops;
- nothing reaches a prompt, the database, an event or a log without `hermclaw.core.redaction`; no model reasoning is
  stored (actions carry only a short user-visible `status` and a `decision` label; unknown fields are refused).

## Modules

| Module | Content | Steps |
|---|---|---|
| `engine.py` | `ToolEngine`, `ActionRejected`; all 19 handlers, policy pre-checks, output budget, scope enforcement | 17.1–17.13 |
| `registry.py` | `ToolSpec`, strict Pydantic argument models, `TOOL_SPECS`, `tool_schemas()`, `render_tool_catalog()`, `validate_args()`, `parse_action()` | 17.13 |
| `context.py` | `ToolPermissions` (+`for_step`), `ToolCallbacks` protocol, `ScopeExpansionOutcome`, `NullCallbacks`, `CallContext` | 17.5, 17.7–17.9 |
| `recorder.py` | `ToolRecorder` – `tool_calls`, `command_runs`, `test_runs`, `steps.checkpoint` + events (redacted) | 17.13 |
| `workspace.py` | `WorkspaceFS` (confinement, atomic writes, binary detection), listing/search helpers | 17.1, 17.3 |
| `patch.py` | unified-diff parser (`parse_patch`, targets incl. renames/deletes, numstat cross-check) | 17.4 |
| `gitlocal.py` | `LocalGit` – minimal git plumbing on the orchestrator copy (ls-files, status, ls-tree, apply, checkout-index) | 17.2, 17.4, 17.5 |
| `snapshot.py` | `WorkspaceTracker` – snapshot / detect / revert of command side effects, git-metadata protection | 17.5 |
| `classify.py` | `CommandClassifier` (forbidden/destructive/mutate/read/unknown, obfuscation-resistant views, built-in git-mutation detection) | 17.5 |
| `testparse.py` | runner output parsers (pytest, unittest, jest, vitest, mocha, phpunit, go test, cargo test, generic) + verdict | 17.6 |
| `executors.py` | `LocalSandboxExecutor`, `SubprocessExecutor` (dev/test only), `SandboxUnavailable` | 17.5 |
| `errors.py` / `output.py` | `ToolError` + stable error codes; clipping helpers | – |

## Public interfaces

```python
ToolEngine(sessionmaker, policies: PoliciesConfig, workspace: WorkspaceHandle, scope_guard: ScopeGuard | None,
           executor: CommandExecutor, git: GitReader, repo: RepoContextProvider, callbacks: ToolCallbacks | None = None,
           *, permissions: ToolPermissions | None = None, redactor: Redactor | None = None, worker_id: str | None = None,
           execution_target: str = "sandbox", max_read_bytes: int = 2_000_000)
await engine.execute(action: CoderAction, *, job_id, step_id, attempt_id, turn) -> ToolResult
await engine.execute_payload(raw: Mapping | str, *, job_id, step_id, attempt_id, turn) -> ToolResult | ActionRejected
engine.tool_catalog() -> list[dict]          # JSON schemas of the tools this step may use (prompt)
engine.scope_guard / engine.finished / engine.finished_by / engine.max_turns

ToolPermissions(allowed_tools=None, allow_destructive_commands=False, network=False, image=None,
                max_command_timeout_seconds=None, max_turns=None, max_research_requests=3, max_scope_requests=3)
ToolPermissions.for_step(kind=..., network=..., turn_budget=..., capability=CapabilityConfig | None)

class ToolCallbacks(Protocol):
    async def on_research(self, question: str) -> str                      # summary or ticket
    async def on_scope_expansion(self, request: ScopeExpansionRequest) -> ScopeExpansionOutcome
    async def on_replan(self, reason: str) -> None
ScopeExpansionOutcome(granted: bool, message: str, contract: ScopeContract | None = None, data: dict = {})

LocalSandboxExecutor(policy: SandboxPolicy, *, settings=None, max_output_bytes=200_000, runner=None)  # CommandExecutor
SubprocessExecutor(policy, *, settings=None, max_output_bytes=200_000)                              # dev/test only
```

One engine serves one step attempt; calls are serialised with a lock (one workspace, one attempt). The engine holds
per-attempt state: the active `ScopeGuard` (swapped after a granted expansion), research/expansion counters, the
"finished" flag of terminal tools and the base-commit file list.

## Tools

| Tool | Kind | Behaviour |
|---|---|---|
| `list_files` | read | git `ls-files` (tracked + untracked, not ignored) or a walk for non-git workspaces; `path`, `recursive`, glob `pattern`, `max_entries` |
| `read_file` | read | whole UTF-8 file (≤ 2 MB), binary refused (`BINARY_FILE`), output clipped with a `read_range` hint |
| `read_range` | read | numbered lines `start..end` (streamed) |
| `find_text` | read | literal (default) or regex search, `glob`, `ignore_case`, result limit + time budget; regexes with nested quantifiers are refused (ReDoS) |
| `search_repo` / `search_symbol` | read | `RepoContextProvider.search` / `find_symbol`; hits on hidden paths are dropped |
| `git_status` / `git_diff` | git | `GitReader.status` / `diff` (diff sections of protected files are omitted) |
| `write_file` | write | create/overwrite, scope-checked, atomic (temp file + fsync + rename), mode preserved, redaction marker refused |
| `replace_text` | write | exact match, `count` must equal the number of occurrences (default 1), CRLF-aware, helpful not-found hints |
| `apply_patch` | write | unified diff (`-p1` git style or `-p0`); every target validated → numstat cross-check → `git apply --check` → `git apply` (worktree only) inside a tracker audit |
| `run_command` | exec | classified, executed via `CommandExecutor`, side effects audited (see below) |
| `run_test` | exec | like `run_command` + parsed counts, `test_runs` row, `test.*` events |
| `request_research` | request | forwarded to `on_research`; per-attempt limit |
| `request_scope_expansion` | request | protected paths refused locally, forwarded to `on_scope_expansion`; a granted newer contract replaces the guard |
| `request_replan` | control, terminal | forwarded to `on_replan` |
| `checkpoint` | control | `steps.checkpoint` = notes, progress, next actions, turn, attempt, changed files, scope version |
| `complete_step` | control, terminal | `CompletionReport` validated; reported vs. actual changed files in `data` (verification follows) |
| `block_step` | control, terminal | `BlockReport` validated |

Mutating tools and write-permission semantics: the operation is judged **relative to the workspace base commit**
(like the verifier's scope audit) – editing or removing a file that does not exist at `base_sha` is a *create*
(non-git workspaces: a file created during this attempt). Paths through symlinked directories are never written.

## Flows

**Tool call (17.13)**

```text
execute(action)
  lock
  insert tool_calls(status=running, arguments=redacted+compacted) + tool.call.started      [tx 1]
  pre-checks: finished? tool allowed? turn <= max_turns (terminal tools always allowed)? args valid?
  handler (may emit test.started in its own tx, write command_runs/test_runs rows + events)
  output redacted, clipped to policies.coder.tool_output_chars (truncated flag), terminal flag set
  update tool_calls(status=succeeded|refused|failed, result_summary, error_code, duration_ms)
    + collected events (file.changed, scope.violation, checkpoint.created) + tool.call.finished(duration) [tx n]
```

Cancellation records `status=cancelled` (shielded) and re-raises. Handler exceptions become
`TOOL_INTERNAL_ERROR` (exception type only; the redacting logger keeps the traceback).

**Commands (17.5/17.6)**

```text
classify (policies.commands + built-in git-mutation detection)
  forbidden -> COMMAND_FORBIDDEN; destructive -> COMMAND_DESTRUCTIVE unless permissions.allow_destructive_commands
cwd validated (inside workspace, directory) -> "cd -- '<cwd>' || exit 97\n<command>"
tracker.snapshot()  (lstat of candidates, byte backups of dirty/untracked files, git metadata + index fingerprint)
executor.run(workspace, ExecutionRequest(timeout<=permission/policy limit, network=permissions.network, image, purpose))
tracker.audit(decide = ScopeGuard.decide on base-relative operations; deny-all without scope)
  new out-of-scope files deleted, modified/deleted tracked files restored with git checkout-index,
  dirty/untracked files restored from backups, git metadata/index restored, up to 3 rounds (.gitignore tricks),
  new untracked generated caches (verifier.generated_file_globs, __pycache__, …) that are not git-ignored removed
command_runs row (redacted stdout/stderr excerpts) + command.run; scope.violation / file.changed when applicable
run_test: test.started before, summarise(output, exit code) -> test_runs + test.passed / test.failed
```

Result codes: `COMMAND_SCOPE_VIOLATION` (takes precedence), `SANDBOX_ERROR`, `COMMAND_TIMEOUT`, `COMMAND_FAILED`;
for tests `TESTS_FAILED` / `TEST_ERROR` (timeout, no tests ran, no exit code). Allowed side effects are reported as
`mutated_paths`.

## Events

| Event | When | Payload (redacted) |
|---|---|---|
| `tool.call.started` | before every call | tool, turn, status, decision, mutating, args preview |
| `tool.call.finished` | after every call (`duration_ms`) | tool, ok, error_code, terminal, truncated, mutated_paths, summary |
| `file.changed` | successful writes / patches / allowed command side effects | tool, changes[{path, operation}] |
| `scope.violation` | refused write targets, reverted command side effects | tool, source (`tool`/`command_side_effect`), scope_version, violations |
| `command.run` | every executed command | command, cwd, classification, exit_code, timed_out, network, purpose, sandbox, reverted, changed_files |
| `test.started` / `test.passed` / `test.failed` | run_test | command, framework, status, counts, exit_code, note |
| `checkpoint.created` | checkpoint | progress, notes (clipped), counts |

Scope expansion events (`scope.expansion.requested` / `scope.expanded`) are emitted by the scope expansion handler
behind `on_scope_expansion`, research events by the research service.

## Configuration

- `policies.coder.tool_output_chars` – output budget per result; `policies.coder.max_turns` – default turn budget.
- `policies.commands.*_patterns` – classification regexes (invalid regex -> `ConfigError`);
  `policies.commands.max_output_bytes` – pass as `max_output_bytes` to the executors (capture limit).
- `policies.sandbox.default_timeout_seconds` – default and maximum command timeout (unless the permissions set one);
  `policies.sandbox.env_allowlist` – environment of the subprocess fallback.
- `policies.scope.always_forbidden` – hidden from all read tools and never writable.
- `policies.verifier.generated_file_globs` – generated caches excluded from snapshots and cleaned when not ignored.
- `HERMCLAW_ENV=production` – the subprocess fallback refuses to run; `LocalSandboxExecutor` then requires
  `worker.execution.sandbox`.

## Failure behaviour

- Invalid arguments / unknown keys -> `ARGS_INVALID` with the JSON schema in `data`; unknown tools and malformed
  actions -> `ActionRejected` (persisted as `refused` tool calls).
- Path violations -> `PATH_INVALID`, `PATH_OUTSIDE_WORKSPACE`, `PATH_FORBIDDEN`, `SYMLINK_REFUSED`; scope violations ->
  `SCOPE_VIOLATION` (nothing written; `apply_patch` is all-or-nothing); no scope -> `SCOPE_MISSING`.
- Writes containing the redaction marker (`***REDACTED***`) are refused (`REDACTED_PLACEHOLDER`) unless the file
  already contains it – masked secrets can never be written back.
- Sandbox/executor exceptions -> the side-effect audit still runs, then `SANDBOX_ERROR` (or the scope violation).
- Broken `GitReader` / `RepoContextProvider` / callbacks -> `GIT_UNAVAILABLE`, `REPO_UNAVAILABLE`, `RESEARCH_FAILED`,
  `SCOPE_EXPANSION_FAILED`, `REPLAN_FAILED` (never terminal).
- Database errors are not swallowed: a call that cannot be recorded is not executed (start) or surfaces to the
  runtime (finish), which handles the attempt.
- The subprocess fallback offers no filesystem/network isolation and is refused in production; it kills the whole
  process group on timeout and after the command (no background survivors), and keeps head and tail of the output.
