# Scope Engine (P15)

`hermclaw/scope/` – runtime generation, versioning, expansion and auditing of the explicit write scope of a step
(Bauplan §17 "Scope Contract", §14 "Explicit Scope", Phase 15; steps 15.1–15.9).

Every step gets a `ScopeContract` from the **runtime** – never from a worker. The planner (Gemma) only supplies
hints; repository intelligence supplies evidence; the runtime validates, decides and persists. Workers can only
*request* more scope (`request_scope_expansion`), and every real change is audited against the active version.
There is no silent access.

All path-permission decisions go through `hermclaw.scope.guard.ScopeGuard` / `path_matches` (the single
glob/permission authority). The scope engine only decides *what goes into* a contract.

## Modules

| Module | Content | Steps |
|---|---|---|
| `engine.py` | `ScopeEngine`, `ScopeDecision`, `ScopeEngineSettings`, workspace listing (`list_workspace_files`, `WorkspaceFiles`), hint classification, confidence selection, shared persistence helpers (`persist_scope_version`, `current_scope_row`, `designated_deletes` …) | 15.1–15.6, 15.8 |
| `expansion.py` | `ScopeExpansionHandler`, `ExpansionDecision`, `PathAssessment`, mechanical-relation evidence (`is_test_path`, `find_import_of`, `language_of`) | 15.7 |
| `audit.py` | `ScopeAuditor`, `ScopeAuditReport`, `derive_changes_from_status`, `unresolved_renames` | 15.9 |
| `guard.py` | `ScopeGuard`, `path_matches` (pre-existing, shared) | – |

## Public interfaces

```python
ScopeEngine(sessionmaker, config: HermclawConfig, repo: RepoContextProvider, *, settings: ScopeEngineSettings | None = None)
await ScopeEngine.create_scope(step_id, workspace: WorkspaceHandle) -> ScopeDecision
await ScopeEngine.current_contract(step_id) -> ScopeContract | None      # active version only
await ScopeEngine.guard_for(step_id) -> ScopeGuard                        # deny-all when no active scope

ScopeDecision(status: "active" | "unavailable", contract: ScopeContract | None, version: int, scope_id: UUID,
              reason: str, reason_code: str | None, evidence: dict)       # .runnable == (status == "active")

ScopeExpansionHandler(sessionmaker, config, repo, *, settings=None)
await ScopeExpansionHandler.handle(step_id, request: ScopeExpansionRequest, workspace, *,
                                   attempt_id=None, requested_by="worker") -> ExpansionDecision
ExpansionDecision(outcome: "granted" | "needs_replan" | "rejected", reason_code, reason,
                  classification: "mechanical" | "semantic" | None, contract, version, previous_version,
                  changed: bool, paths: list[PathAssessment])            # .granted / .needs_replan

ScopeAuditor(sessionmaker, config)
await ScopeAuditor.audit_changes(step_id, changes: Iterable[(path, op)], *, attempt_id=None, phase="attempt",
                                 unauditable: Iterable[(path, reason)] = ()) -> ScopeAuditReport
await ScopeAuditor.audit_status(step_id, entries: Iterable[GitStatusEntry], *, attempt_id=None, phase="attempt")
derive_changes_from_status(entries) -> list[(path, "create" | "modify" | "delete")]
```

Dependencies are the shared protocols only: `WorkspaceHandle`, `RepoContextProvider` (`find_symbol`, `search`,
`read`), `GitStatusEntry`; persistence via `scope_contracts` / `steps`; events via `append_event`.

## Generation flow (`create_scope`)

1. **Hint intake (15.1)** – a detached snapshot of the step: `repo_hints`, `allowed_new_paths`,
   `forbidden_paths`, `acceptance`, `constraints`, `kind`. Closed (`completed`/`cancelled`) or superseded steps
   and a workspace of another job raise `ConflictError`; no DB session is held during repository I/O.
2. **Workspace listing** – `git -c core.fsmonitor=false ls-files -z --cached --others --exclude-standard`
   (tracked + untracked, not ignored; `GIT_*` env stripped), filtered by real existence as regular files; `.git/`
   entries, symlinks leaving the workspace and files below symlinked directories leaving the workspace are
   dropped. Non-git directories fall back to a walk (`listing: "walk"` in evidence).
3. **Repository evidence (15.2)** – each hint is canonicalised (`canonical_path`: `./`, `//`, `.` segments
   collapsed; absolute, `..` and repository-root hints are invalid), location suffixes are stripped when the
   file exists (`a.py:12`, `a.py:3-9`, `a.py#L3-L9`, `t.py::test_x`), then classified:
   - `path` / `glob` / `directory` → resolved against the workspace file list (an existing literal path wins,
     so `routes/[id].tsx` is a file, not a glob). Unknown bare file names resolve via a *unique* basename match;
     several matches are `ambiguous`.
   - `symbol` → `find_symbol` (full name, then last component), falling back to `search`; `text` → `search`.
     Only hits on existing workspace files with `score >= min_symbol_score` (0.75) / `min_search_score` (0.85)
     **and** `>= relative_score_floor` (0.8) × best score are accepted; more than `max_files_per_hint` (5)
     confident files makes the hint `ambiguous` (nothing taken). Every hit's score, lines and signals are evidence.
   - Provider errors and timeouts (`repo_timeout_seconds`, 60 s per call) become `status: "error"` evidence.
   - For writing kinds a path hint that does not exist yet becomes an allowed new path (if creatable: no parent
     component is a file); a missing directory hint allows creation below it.
4. **Acceptance / constraints** – `PresenceEvidence` on a missing concrete path → allowed new path;
   `AbsenceEvidence` without `pattern` on existing files → **delete** targets; a non-negated constraint such as
   "delete legacy/x.py", "remove x from the repository", "x.py löschen" → delete targets. "remove X from Y" /
   "die Zeile aus Y entfernen" edit Y and never grant a deletion.
5. **Policy merge (15.3) / forbidden paths (15.5)** – `policies.scope.always_forbidden` + step forbidden paths
   (canonicalised, de-duplicated, invalid ones recorded) form `forbidden_paths`. Excluded with evidence
   (`evidence.excluded`): forbidden targets / concrete new paths, unbounded creation patterns (`**`, `*`,
   `**/*.py`, `**.md`, `*/x/`), implausible creation paths (`:` or control characters). An allowed new path that
   already exists is promoted to a target.
6. **Generation (15.4)** – `ScopeContract(source="planner_and_repo_intelligence", strict_target_paths=True,
   target_paths=<existing files, glob-escaped>, allowed_new_paths, forbidden_paths, allowed_operations)` with
   `create` iff new paths exist, `modify` iff targets exist, `delete` iff delete targets remain after the policy
   merge. The designated delete files are kept in `evidence.delete_paths` (see audit).
7. **Unavailable (15.8)** – no target and no new path (`no_resolvable_scope`), or a cap exceeded
   (`too_many_target_paths` / `too_many_new_paths`; caps are never truncated) → `status: "unavailable"`, a
   deny-all contract is persisted with the full evidence, `scope.unavailable` is emitted, the decision has
   `contract=None`. The caller blocks or replans the step. Nothing is restored, committed or pushed.
8. **Read-only kinds** – only `implement`, `database`, `docker` and `documentation` write workspace files. All
   other kinds (`inventory`, `discover`, `research`, `test`, `verify`, `review`, `image`, … and `ssh`/`deploy`,
   which mutate remote hosts under the SSH/deployment policies) get an *active* deny-all contract with
   `evidence.read_only = true`.
9. **Versioning (15.6)** – under the step row lock (`SELECT … FOR UPDATE`, re-checking closed/superseded) the
   next version `max+1` is inserted; every previous non-superseded version (active or unavailable) becomes
   `superseded` and gets `evidence.superseded = {by_version, previous_status, at}`;
   `steps.current_scope_version` moves to the new version. Invariant: at most one non-superseded row per step.

## Expansion flow (15.7)

`handle()` snapshots the step + current version, lists the workspace, evaluates, then commits under the step
lock with an optimistic check (`current_scope_version` unchanged, otherwise re-evaluate; 3 tries, then
`ConflictError scope_expansion_conflict`).

- **Path validation** – canonical, repository-relative, explicit *files* (no `*`/`?`, no directories); `[`/`]`
  are literal file-name characters and are glob-escaped into the contract (`[id].ts` → `[[]id].ts`), so the guard
  matches exactly that file. Not forbidden; existing (→ modify/delete) or creatable (→ create); paths that exist
  on disk but are ignored, implausible paths and deletes of missing files are invalid.
- **Rejected** – step closed/superseded (`step_closed`), no active scope (`no_active_scope`), any invalid
  (`invalid_paths`) or forbidden (`forbidden_paths`) path.
- **Already in scope** – `granted`, `changed=False`, no new version.
- **Mechanical** (every pending path) – evidence read through `RepoContextProvider.read`: a test of a current scope
  file (name relation `test_x.py`/`x.spec.ts`/`XTest.java`, or the test imports it), a direct import/dependency of
  a current scope file (an import line references the path: Python, JS/TS, PHP, Ruby, C/C++, Rust, Go, Java …),
  or the same directory and language as a current scope file. Granted as a new version
  (`source="runtime_expansion"`, delete designation carried forward), `scope.expanded`.
- **Semantic** – anything else, and every deletion → `needs_replan` (`semantic_expansion`); the replanner decides.
  The `max_expansions_per_step` (3) limit (`expansion_limit_reached`) and policy caps (`scope_caps_exceeded`)
  also yield `needs_replan`.
- `scope.expansion.requested` is emitted for **every** request (warning severity when rejected); each request is
  recorded (redacted justification) in `evidence.expansion_requests` of the version it was evaluated against
  (last 20).

## Audit flow (15.9)

`audit_changes` locks the step and its current row, builds a `ScopeGuard` from the active version (deny-all when
the step has no active scope) and checks every unique `(path, op)`:

- guard denial (forbidden, operation not allowed, not a target, outside `allowed_new_paths`, invalid path);
- `delete` of a target that the version does not designate for deletion (`evidence.delete_paths`) – a
  contract-level `delete` only covers designated files;
- `unauditable` entries supplied by the caller (fail closed).

`audit_status` maps porcelain XY codes via `derive_changes_from_status` (`??`→create, `A`→create, `D`→delete,
`R`/`C`→create new + delete origin for renames, `!!` skipped, everything else → modify; quoted paths unquoted).
Because `GitReader.status` currently drops the origin of staged renames, `unresolved_renames` reports them and the
audit records a violation for each ("deletion of its source path cannot be audited").

The result is appended to `evidence.audits` of the audited version (last 20, `evidence.last_audit_ok`); any
violation emits one `scope.violation` (severity error) with up to 50 violations.

## Events

| Event | When | Payload (excerpt) |
|---|---|---|
| `scope.created` | active version generated | version, previous_version, source, operations, target/new/forbidden paths (≤50), counts, read_only |
| `scope.unavailable` | unavailable version generated (warning) | same + reason, reason_code, unresolved_hints |
| `scope.expansion.requested` | every expansion request | outcome, reason_code, classification, paths, assessments, redacted justification |
| `scope.expanded` | mechanical expansion granted | new contract summary, added paths/operations, classification |
| `scope.violation` | audit with violations (error) | scope_version/status, phase, checked, violation_count, violations |

All events carry `job_id`/`step_id` (and `attempt_id` when given); `source_type="runtime"`,
`source_id` = `scope_engine` / `scope_expansion` / `scope_audit`. Payloads and evidence are redacted
(`hermclaw.core.redaction`); no model output or reasoning is stored – only hints, paths, scores and decisions.

## Configuration

| Setting | Source | Default |
|---|---|---|
| `always_forbidden` | `policies.scope` | `.git/**`, `**/.env`, `**/*.pem`, `**/*.key`, `**/id_rsa*`, `**/id_ed25519*` |
| `max_target_paths` / `max_new_paths` | `policies.scope` | 25 / 25 |
| `min_symbol_score` / `min_search_score` / `relative_score_floor` | `ScopeEngineSettings` | 0.75 / 0.85 / 0.8 |
| `max_files_per_hint`, `search_k` | `ScopeEngineSettings` | 5, 20 |
| `max_expansions_per_step` | `ScopeEngineSettings` | 3 |
| `read_max_chars`, `list_timeout_seconds`, `repo_timeout_seconds` | `ScopeEngineSettings` | 40 000, 30 s, 60 s |

## Failure behaviour

| Situation | Behaviour |
|---|---|
| unknown step | `NotFoundError step_not_found` |
| step completed/cancelled/superseded (also when closed during generation) | `ConflictError scope_step_closed`, nothing written |
| workspace of another job | `ConflictError scope_workspace_mismatch` |
| workspace path missing | `ScopeEngineError scope_workspace_missing`, nothing written |
| file listing hangs | `ScopeEngineError scope_listing_timeout` (process killed) |
| repository intelligence error/timeout | hint evidence `status: "error"`; generation continues (may become unavailable) |
| nothing resolvable / cap exceeded | persisted `unavailable` version + `scope.unavailable`; caller blocks/replans |
| corrupt JSONB step fields | non-string hints / invalid acceptance items are ignored, invalid paths recorded |
| concurrent generation / expansion / audit | serialised by the step row lock; unique `(step_id, version)`; expansion retries |
| unreadable file during expansion | no import evidence (classification falls back to other relations / semantic) |
| no active scope at audit | deny-all: every change is a violation |

## Tests

- `tests/unit/test_scope_engine_units.py`, `tests/unit/test_scope_hardening_units.py` – classification, canonical
  paths, unbounded patterns, location suffixes, confidence selection, delete constraints, import evidence,
  porcelain mapping, rename origins, symlink containment.
- `tests/integration/test_scope_engine.py` – resolution of paths/globs/directories/symbols, forbidden paths,
  delete derivation, unavailable, caps, versioning, closed steps, redaction (real PostgreSQL + real git).
- `tests/integration/test_scope_expansion.py` – mechanical (test/import/same dir) vs semantic, limits, caps,
  rejected requests, redaction.
- `tests/integration/test_scope_audit.py` – allowed/violating changes, real `git status`, deny-all, history bound.
- `tests/integration/test_scope_hardening.py` – review regressions (closed during generation, repo timeouts,
  unavailable supersede provenance, designated deletes, rename fail-closed, bracket file names,
  expire-on-commit sessions).
- `tests/failure/test_scope_failures.py` – provider outage, missing workspace, listing timeout, concurrency.

## Known limitations / shared follow-ups

- `GitStatusEntry` has no `orig_path`; `hermclaw/gitops/reader.py` drops rename origins. The audit fails closed
  until the producer carries the origin (`derive_changes_from_status` already reads `orig_path` when present).
- `ScopeContract` has no per-path operations; the designated-delete restriction is enforced by the audit, not
  by tool-time `ScopeGuard` checks.
- `ScopeGuard` compiles the escape `[[]` into a regex that Python flags with a `FutureWarning`
  ("possible nested set"); escaping `[` inside character classes in `guard._glob_regex` removes it.
