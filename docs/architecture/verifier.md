# Deterministic Verifier (P21)

`hermclaw/verifier/` – Bauplan §21 "Deterministic Verifier", Full-Build-Prompt §18, Phase 21 (steps 21.1–21.14).
The verifier judges a step's workspace change **without any model** and **without task-specific production rules**:
it runs generic checks (scope, forbidden paths, syntax, compile, lint, secrets, conflict markers, generated files,
changed-file count, deletion policy, test requirement) plus the machine-checkable acceptance evidence the planner
attached to the step (`presence`, `absence`, `command`, `test`, `diff`, `scope`, `schema`, `security`, `artifact`).

Invariants:

- the verdict depends only on the change, the policies and the step's acceptance evidence – nothing in the package
  knows a repository, project, file name or benchmark (regression test
  `test_verdict_depends_only_on_generic_checks_and_evidence`);
- every command runs through the sandbox `CommandExecutor` (never a local shell); the only local processes are
  read-only git plumbing and the parse-only `bash -n`; project code is never executed on the orchestrator
  (Python is checked with `compile()`, JSON/YAML/TOML with in-process parsers);
- the verifier never leaves a trace: the workspace is snapshotted before the first command and every side effect of
  verifier commands (formatters, caches, build output, even `git commit`) is reverted, so what was verified is exactly
  what gitops may commit (`commit_verified` only accepts a `passed` run and only its `changed_files`);
- secret values never reach a check, event, log or database row (masked previews, `hermclaw.core.redaction` on every
  message/evidence/excerpt; `append_event` redacts again);
- security checks cannot be switched off by acceptance evidence (`security` / `scope` criteria only *mirror* the
  always-on generic checks).

## Modules

| Module | Content | Steps |
|---|---|---|
| `engine.py` | `Verifier` (`run` → `VerificationOutcome`, `verify` → `VerificationReport`), evaluation order, per-workspace lock, crash containment, `_SideEffectGuard` (snapshot / revert via `tools.snapshot.WorkspaceTracker`) | 21.1–21.14 |
| `types.py` | `VerificationStep` (`build`, `from_row`), `load_step`, `parse_acceptance`, `FileChange`/`ChangeSet`, `AddedLine`/`AddedContent`, `ArtifactRecord`, `ArtifactLookup`, `db_artifact_lookup`, `VerificationOutcome` | – |
| `changes.py` | `collect_changes` (GitReader + base tree → create/modify/delete), added lines (`git diff -U0 <base>` per file, untracked = whole file, GitReader-diff fallback), inventory, safe file reads | 21.1, 21.7, 21.8 |
| `checks.py` | scope + forbidden, generated files, changed-file count, deletions, secrets, conflicts, syntax orchestration, compile, lint, implement-step test requirement | 21.1–21.8, 21.12 |
| `evidence.py` | presence, absence, diff, command, test (framework-aware), schema, artifact, mirrored scope/security criteria | 21.5, 21.6, 21.9–21.13 |
| `syntax.py` | in-process parsers, local `bash -n`, sandbox batches for `php -l` / `node --check` with nonce markers | 21.2 |
| `secrets.py` | `SecretScanner` (redaction patterns + prefixed assignments + well-known token shapes + entropy heuristic) | 21.7 |
| `conflicts.py` | conflict marker scan | 21.8 |
| `schema.py` | JSON/YAML/TOML parsing, `jsonschema` (if importable) or built-in validator | schema evidence |
| `commands.py` | `CommandRunner` (executor wrapper: timeout + grace, error capture, redacted excerpts, `command_runs`/`test_runs` records) | 21.3–21.6, 21.11 |
| `report.py` | `build_report` (passed iff no blocking fail/error, summary), `run_status` | 21.14 |
| `store.py` | `start_run` / `finish_run` / `abort_run` – rows + VERIFIER_* events | 21.14 |
| `languages.py`, `text.py`, `context.py` | language detection, test-suite detection, lock files; PostgreSQL-safe text; per-run context + `make_check` (bounded, redacted evidence) | – |

Reused from finished components (no duplicated logic): `ScopeGuard`/`path_matches` (the only path-permission and glob
logic), `tools.gitlocal.LocalGit` (read-only plumbing), `tools.snapshot.WorkspaceTracker` (side-effect revert),
`tools.testparse` (runner summaries), `tools.workspace.regex_is_risky`/`is_binary`.

## Interface

```python
verifier = Verifier(sessionmaker, config_or_policies, executor: CommandExecutor, git: GitReader,
                    artifacts: ArtifactLookup | None = None)
step = VerificationStep.build(key="S003", kind="implement", acceptance=[...], scope=scope_contract, network=False)
# or: step = await load_step(session, step_id)   # steps row + newest active scope_contracts row
outcome = await verifier.run(step, workspace, job_id=..., step_id=..., attempt_id=..., artifacts=None)
outcome.run_id      # verification_runs.id – pass to GitEngine.commit_verified(...)
outcome.report      # VerificationReport(passed, checks, changed_files, summary); report.failures for correction
outcome.status      # passed | failed | error
report = await verifier.verify(...)  # same, report only
```

`ArtifactLookup = Callable[[job_id, step_id, kind], Awaitable[Sequence[ArtifactRecord]]]`; the default reads
`artifacts` rows of the job + step.

## Flow

1. `start_run`: `verification_runs` row (`running`) + `VERIFIER_STARTED`, committed (visible live).
2. Per-workspace lock (in-process, refcounted). Missing workspace → `workspace` error check.
3. `collect_changes`: `GitReader.changed_files` (authoritative), `GitReader.status` (renames' source paths), base tree
   via `git ls-tree <base_sha>` → `create` (not in base) / `modify` / `delete` (in base, gone). Without a readable
   base tree the porcelain codes decide. Failure → `changed_files` error check, run status `error`.
4. Local checks: `forbidden_paths`, `scope` (ScopeGuard audit; no scope contract + changes = fail),
   `generated_files` (`policies.verifier.generated_file_globs`), `changed_file_count`
   (`policies.verifier.max_changed_files`), `deleted_files` (`allow_deletions` **and** scope allows `delete`),
   `secrets:*` / `conflicts:*` over **added lines only**, presence/absence/diff/schema/artifact criteria.
5. Commands (serialised; snapshot before the first one): syntax batches (PHP, JS; shell when bash is missing
   locally), compile (`npx tsc --noEmit` if `tsconfig.json` + `node_modules/typescript`, `go build ./...` if `go.mod`,
   `cargo check [--offline]` if `Cargo.toml` – only when changed files are relevant to the toolchain), lint
   (`policies.verifier.lint_commands[language]`, `{files}` = shell-quoted changed files of that language), command
   and test criteria in declaration order. Afterwards `workspace_integrity`: side effects reverted (non-blocking info)
   or not restorable / snapshot impossible (blocking error).
6. Derived: mirrored `scope`/`security` criteria, `test_evidence` (21.12).
7. `build_report` → `finish_run`: one `verification_checks` row per check, `command_runs` (classification
   `verifier:<purpose>`) and `test_runs` rows, `VERIFIER_CHECK_FAILED` per fail/error check (max. 50, severity warning
   when blocking, info otherwise), `VERIFIER_FINISHED` (passed, status, counts, failure labels, duration).

## Checks

| check_type / name | Pass condition | Notes |
|---|---|---|
| `forbidden` / `forbidden_paths` | no changed path matches `policies.scope.always_forbidden` + contract `forbidden_paths` | always on |
| `scope` / `scope` | every change allowed by the step's ScopeGuard | create/modify/delete judged against the base |
| `generated` / `generated_files` | no created/modified path matches the generated globs | |
| `changed_files` / `changed_file_count` | count ≤ `max_changed_files` | |
| `deletions` / `deleted_files` | no deletions, or policy + scope allow each | |
| `secrets` / `secrets[:path]` | no finding in added lines | always blocking (no test/fixture exception) |
| `conflicts` / `conflict_markers[:path]` | no `<<<<<<<`/`|||||||`/`>>>>>>>` (and bare `=======` outside markup) at line start | added lines |
| `syntax` / `syntax:<path>` | file parses | skip with reason: TS/JSX (compile), templated YAML, binary, symlink, > 2 MB, missing tool (exit 126/127) |
| `compile` / `compile:<lang>` | toolchain command exits 0 | skip: no marker, no relevant change, no `node_modules`, missing tool |
| `lint` / `lint:<lang>` | lint command exits 0 | missing configured linter = error |
| `unit`/`integration`/`contract` / `acceptance[i]:test…` | exit 0, no failures/errors, `passed ≥ min_passed` (when counts are parseable) | category from the evidence wording |
| `test_evidence` / `test_evidence` | implement step in a repo with tests: ≥ 1 test criterion executed and passed | skip for other kinds / repos without tests |
| `presence` | ≥ `min_matches` paths (no pattern) or regex matches over the glob | samples with path/line/snippet |
| `absence` | no path matches (no pattern) or **0** regex matches; failing files/lines reported | literal paths found even when git-ignored |
| `command` | `exit == expect_exit_code` and optional stdout regex | network only if evidence **and** step allow it |
| `diff` | `must_change` hit, `must_not_change` untouched, `max_changed_files`, `allow_empty` | |
| `schema` | file parses (json/yaml/toml) and validates against `json_schema` | `jsonschema` or built-in validator, no remote `$ref` |
| `artifact` | ≥ `min_count` artifacts of `kind` matching `name_glob` with ≥ `min_size_bytes` | via `ArtifactLookup` |
| `scope`/`security` criteria | mirror the generic checks | cannot disable them |
| `side_effects` / `workspace_integrity` | verifier commands left no unrestorable change | non-blocking when everything was reverted |

`VerificationReport.passed` is true iff no **blocking** check has status `fail` or `error`; `skip` never fails.
Run status: `passed`, `failed`, or `error` when all blocking problems are errors (the verifier could not judge).

## Secret scan (21.7)

Sources: the patterns of `hermclaw.core.redaction` (token shapes first, then key/value, bearer and URL-credential
shapes), a prefixed-key assignment rule (`DB_PASSWORD=…`, `spring.datasource.password=…`), literal secrets registered
in `DEFAULT_REDACTOR`, private key headers, Slack/Google/Stripe/npm tokens, JWTs, and a high-entropy heuristic
(≥ 32 chars, mixed case + digits, entropy ≥ 4.3, literal context; hex only with a keyword nearby; lock/data files
excluded). Assignment matches count only for literal values (quoted in code files) that are not placeholders
(`${VAR}`, `<token>`, `changeme`, `example`, `xxxx` …). Lines a GitReader already redacted (`***REDACTED***`) count as
findings. Findings carry path, line, rule and a masked preview only.

## Configuration

`policies.verifier`: `max_changed_files` (40), `allow_deletions` (true), `generated_file_globs`, `lint_commands`
(`{language: command}`; languages: `python`, `javascript`, `typescript`, `json`, `yaml`, `toml`, `shell`, `php`,
`go`, `rust`, … see `languages.LANGUAGE_BY_EXTENSION`). `policies.scope` (forbidden globs), `policies.sandbox.
default_timeout_seconds` (compile/lint timeout). Evidence timeouts come from the criteria.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| GitReader down | `changed_files` error, run `error`, persisted |
| executor raises / returns no result | the affected check is `error` (message redacted), other checks continue |
| executor hangs | bounded by `timeout_seconds + EXECUTOR_GRACE_SECONDS` → `error` |
| command times out | `fail` with "timed out" |
| a check group crashes | that group becomes one `error` check; the run finishes |
| `_evaluate` crashes | single `verifier` error check, run `error`, persisted |
| result cannot be persisted | `abort_run` marks the run `error`, exception propagates |
| cancellation | `abort_run` (shielded) marks the run `error`, `CancelledError` propagates |
| snapshot impossible / side effect not restorable | blocking `workspace_integrity` error |
| unsafe files (symlink escape, binary, oversized) | skipped with a reason, never followed or executed |
| risky/invalid regex in evidence | criterion `error` (nested quantifiers rejected) |
| NUL bytes / non-UTF-8 names | stored escaped (`pg_safe`); escaped names do not match gitops paths → commit is refused (fail closed) |

Residual risks: regexes that evade the nested-quantifier heuristic can still backtrack (bounded per file to 2 MB and
in total to 256 MB of searched content); the per-workspace lock is in-process (one runtime process verifies a
workspace at a time); a Python syntax check uses the orchestrator interpreter's grammar.
