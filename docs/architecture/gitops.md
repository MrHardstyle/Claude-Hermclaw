# Git Engine (P06)

`hermclaw/gitops/` – the runtime-controlled Git engine (Bauplan §27, §2.5, §35 "Git SSH"; steps 6.1–6.12).

Only the runtime mutates repositories. Models get the read-only `WorkspaceGitReader` (status, diff,
changed files, log, base file content); everything that writes goes through `GitEngine` and leaves one
`git_operations` row **and** one event per operation – successful, failed or refused.

## Modules

| Module | Content |
|---|---|
| `engine.py` | `GitEngine` (all writes), `DiffLimits`, `EngineTimeouts`, `build_commit_message`, `ssh_options_from_config` |
| `registry.py` | `RepositoryRegistry` – `repositories` rows, protected-branch policy, GitLab sync |
| `runner.py` | `GitRunner` – hardened async `git` subprocess runner, `GitSshOptions` |
| `ops.py` | path-level git helpers (status, diff, rev-parse, integrity check …) – no DB, no policy |
| `reader.py` | `WorkspaceGitReader` – implements `hermclaw.core.interfaces.GitReader` for LLM tools |
| `scope_guard.py` | `StagingGuard` – staging decisions on top of `hermclaw.scope.guard.ScopeGuard` |
| `gitlab.py` | `GitLabClient` – GitLab REST v4 (merge requests, protected branches) via httpx |
| `audit.py` | `OpRecord`, `write_audit` – `git_operations` row + event in one transaction |
| `naming.py`, `urls.py`, `parsing.py`, `locks.py`, `_fs.py` | branch names, URL validation/redaction, porcelain parsers, locks, fs helpers |
| `_secrets.py` | `read_secret(ref)` / `secret_file_path(ref)` for `env:NAME`, `file:/path`, `cred:name` (until the shared SecretStore replaces it) |
| `errors.py` | engine error classes (stable codes) |

## Public interfaces

```python
GitEngine(sessionmaker, *, settings: Settings, policies: PoliciesConfig, runner: GitRunner | None = None,
          gitlab: GitLabClient | None = None, diff_limits: DiffLimits | None = None, timeouts: EngineTimeouts | None = None)
GitEngine.from_config(config: HermclawConfig, settings: Settings, sessionmaker, *, gitlab=GitLabClient | "auto" | None) -> GitEngine
engine.registry: RepositoryRegistry
engine.reader() -> WorkspaceGitReader

# 6.2 / 6.3
async sync_mirror(repo: RepoRef, *, job_id=None) -> Path          # aliases: clone, fetch
async resolve_base_sha(repo: RepoRef, branch: str | None = None, *, fetch=True, job_id=None) -> str
# 6.4 / 6.5
job_branch(job_id, title_or_slug) -> str                          # "<branch_prefix><job-short-id>-<slug>"
async create_workspace(job_id, repo: RepoRef, *, base_branch=None, slug=None, step_id=None, fetch=True) -> WorkspaceInfo
async get_workspace(workspace_id) -> WorkspaceInfo ; async list_workspaces(job_id) -> list[WorkspaceInfo]
async handle(ws) -> WorkspaceHandle                               # shared handle for tools/executors
# 6.6
async status(ws) -> WorkspaceStatus
async diff(ws, *, paths=None, include_patch=True, include_untracked=True, max_patch_bytes=None, max_files=None, context_lines=None) -> DiffResult
async changed_files(ws) -> list[str]
# 6.7 / 6.8
async stage_allowed(ws, scope: ScopeContract, *, step_id=None) -> StageResult          # .staged / .refused
async commit_verified(ws, message: str, verification_run_id: UUID, *, step_id=None, scope: ScopeContract | None = None) -> CommitResult
# 6.9 / 6.10
async push_job_branch(ws, *, create_merge_request: bool | None = None, title=None, description=None, require_current_base=False) -> PushResult
async create_merge_request(ws, *, title=None, description=None) -> MergeRequestInfo
assert_pushable(repo: RepositoryInfo, branch: str, *, base_branch: str | None = None) -> None
# 6.11 / 6.12
async check_base(ws, *, fetch=True) -> BaseStatus
async ensure_base_current(ws, *, fetch=True) -> BaseStatus                             # raises StaleBaseError
async update_to_base(ws, *, strategy: "rebase" | "merge" = "rebase", fetch=True) -> UpdateResult  # raises MergeConflictError
async recover(ws) -> RecoveryResult                                                    # crash recovery
# cleanup
async cleanup(ws, *, force=False) -> WorkspaceInfo ; async cleanup_job(job_id, *, force=False) -> list[WorkspaceInfo]

RepositoryRegistry(sessionmaker, *, git_policy: GitPolicy)
  async register(name, url, *, default_branch="main", provider="gitlab", gitlab_project_id=None,
                 protected_branches=(), metadata=None, update=False) -> RepositoryInfo
  async get(repo_id) / get_by_name(name) / resolve(ref: RepositoryInfo | UUID | str) -> RepositoryInfo
  async list_repositories() -> list[RepositoryInfo]
  async set_protected_branches(ref, patterns, *, source="manual") ; async sync_protected_branches(ref, gitlab)
  protected_patterns(repo) -> list[str] ; protected_match(repo, branch) -> str | None ; is_protected(repo, branch) -> bool

WorkspaceGitReader(runner, *, workspaces_root=None, max_patch_bytes=200_000, forbidden_globs=())
  async status(handle) / diff(handle, paths=None, *, max_bytes=200_000) / changed_files(handle)
  async log(handle, *, max_count=20) / show_base_file(handle, file_path, *, max_bytes=200_000)

GitLabClient(base_url, *, token=None, token_ref=None, timeout=30.0, retries=2, backoff_seconds=0.5, verify=True, transport=None)
  async create_merge_request(project, *, source_branch, target_branch, title, description="", remove_source_branch=False, labels=None)
  async find_open_merge_request(project, *, source_branch, target_branch=None) ; async list_protected_branches(project)
  async get_project(project) ; async delete_branch(project, branch) ; async aclose()
```

`ws` (`WorkspaceRef`) is a `WorkspaceInfo`, a `WorkspaceHandle` or a workspace UUID; the DB row is always
re-read (inside the workspace lock for writes). Result types (`types.py`) are frozen Pydantic models.

## Layout on the orchestrator (.225)

```text
<settings.repos_cache_dir>/<repo>.git          bare mirror cache (+refs/heads/*, +refs/tags/*, fetch --prune)
<settings.workspaces_dir>/<job_id>/<repo>/     isolated full clone (--no-hardlinks) on the job branch
<settings.workspaces_dir>/.locks/              flock files (cross-process); per-key asyncio locks in-process
```

Directory names come from `safe_dir_name()` (hash suffix when the repository name had to be changed).

## Flow and audit (operation → `git_operations.operation` → event)

| Step | Operation | Event on success |
|---|---|---|
| 6.1 | `RepositoryRegistry.register/set_protected_branches` | `git.operation` (`repository.register/update/protected_branches`) |
| 6.2 | mirror clone / fetch → `mirror.clone` / `mirror.fetch` | `git.operation` |
| 6.4/6.5 | `create_workspace` → `workspace.create` (`archive` for vanished dirs) | `git.operation` |
| 6.7 | `stage_allowed` → `stage` (+ `scope.violation` event if anything was refused) | `git.operation` |
| 6.8 | `commit_verified` → `commit` | `git.commit.created` |
| 6.9 | `push_job_branch` → `push` | `git.pushed` |
| 6.9 | merge request → `merge_request.create` / `merge_request.reuse` | `git.merge_request.created` / `git.operation` |
| 6.11 | `ensure_base_current` → `base.check` (refused when stale), tracking-ref refresh `workspace.fetch_base` | `git.operation` |
| 6.12 | `update_to_base` → `base.rebase` / `base.merge` | `git.operation` |
| – | `recover` → `workspace.recover`; `cleanup` → `workspace.cleanup`; integrity → `workspace.integrity` / `workspace.attributes` | `git.operation` |

Failures are recorded with status `failed`, policy refusals (protected branch, not verified, nothing to
commit/push, push rejected, stale base, workspace state, tampering, scope) with status `refused`; the event is
then `git.operation` with severity `warning`/`error`, `error_code` and redacted `error_details`. Successful
writes that touch the DB (commit, push, stage, base update, cleanup, workspace creation) write the row, the
event and the state change in **one** transaction.

## Rules

- **Job branch (6.5):** `<policies.git.branch_prefix><job.id.hex[:8]>-<slug(job.title|slug)>`, validated with
  `git check-ref-format` rules. Deterministic per job, so a retried job resumes from its remote job branch if
  that still contains the base commit; otherwise it starts fresh from the base.
- **Protected branches (6.10):** union of the repo's default branch, `repositories.protected_branches` and
  `policies.git.protected_branches` (fnmatch globs, `refs/heads/` optional). Refused locally before any
  network access, also at workspace creation and commit. Only `branch_prefix*` branches are pushable, never the
  base branch. `sync_protected_branches` merges GitLab's rules into the row (local rules are never weakened).
- **Safe staging (6.7):** the index is reset to `HEAD`, then each changed path (porcelain, every untracked file,
  ignored files excluded) is decided by `StagingGuard`: invalid path → `.git` segment → embedded repository →
  `policies.scope.always_forbidden` → `scope.forbidden_paths` → `allowed_operations` → `ScopeGuard.decide`
  (modify/delete need `target_paths`, create needs `allowed_new_paths`). Symlinks pointing outside the
  workspace are refused. Only allowed paths are added (NUL pathspec file, literal pathspecs); the resulting index
  is re-checked and reset if it ever contains anything else. Refused paths stay untouched in the work tree.
- **Runtime commit (6.8):** requires a `verification_runs` row of the workspace's job (and `step_id`, if given)
  with `passed=true, status=passed`, not yet used by an `ok` commit, created **after** the workspace was
  created or last rebased/merged onto a new base, and – if it lists `changed_files` – covering every staged path.
  Commits citing the same run are serialised (one run backs one commit). The index is re-checked against
  `always_forbidden` (or the full scope if passed). Message: redacted, control chars removed, subject ≤ 200,
  trailers `Hermclaw-Job/-Step/-Verification`; identity from `policies.git.author_name/author_email`;
  `--no-verify`, no signing, hooks disabled.
- **Runtime push (6.9):** pushes `refs/heads/<job branch>` only, to the **registered repository URL** (never a
  workspace-configured remote), `--force-with-lease=<ref>:<remote sha seen just before>`. A non-fast-forward
  update is allowed only if that remote SHA was pushed by the runtime for this job or seen at workspace creation;
  commits someone else added to the job branch are never overwritten (`PUSH_REJECTED`, reason
  `foreign_commits`). `require_current_base=True` refuses a stale base first. MR creation (policy
  `create_merge_request` or argument) never fails the push; errors land in `PushResult.merge_request_error`.
- **Stale base (6.11):** the mirror is fetched, the workspace tracking ref refreshed, and `base_sha` compared with
  the upstream branch tip (`commits_behind`, `rewritten` when the old base is no longer an ancestor).
- **Conflicts (6.12):** `update_to_base` stashes uncommitted work (incl. untracked), runs `rebase --onto <new>
  <old base>` or a `--no-ff` merge, re-applies the stash. On any conflict the rebase/merge is aborted, the
  workspace reset to its previous HEAD, the stash re-applied, and `MergeConflictError(details.files, phase)` is
  raised – the workspace is never left conflicted. A pushed workspace whose head changed becomes `committed`
  again; verification must be re-run before the next commit (enforced by the freshness rule).
- **Cleanup:** removes the directory and marks the row `cleaned` (idempotent); refuses unpushed verified commits
  (`committed`) unless `force=True`.

## Security hardening

- `GitRunner`: argv only (no shell), `GIT_TERMINAL_PROMPT=0`, empty askpass, `LC_ALL=C`, `GIT_CONFIG_NOSYSTEM`,
  `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_LITERAL_PATHSPECS=1`, `GIT_ALLOW_PROTOCOL=file:ssh:https:http`, `-c
  core.autocrlf=false core.hooksPath=/dev/null core.fsmonitor=false credential.helper= protocol.ext.allow=never
  commit.gpgSign=false …`, `GIT_SSH_COMMAND=ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -i <key>
  -o IdentitiesOnly=yes -o UserKnownHostsFile=<known_hosts>`, per-command timeout (process group killed),
  bounded output, redacted error details.
- **Workspace integrity:** the sandbox can write the workspace (D-005 sync), so a modified `.git` could make the
  runtime's own git execute commands on the orchestrator (filter/merge/diff drivers, `remote.*.uploadpack`,
  `include.path`, `url.*.insteadOf`, `core.worktree` …). Before running git in a workspace the engine and the
  reader verify: `.git` is a real directory of that work tree, no symlinks in `.git/` (or `info/attributes`), no
  `alternates`/`commondir`/`config.worktree`, and `.git/config` contains only the allow-listed keys
  (`ops.ALLOWED_LOCAL_CONFIG`). Otherwise `WORKSPACE_TAMPERED` (refused + event); cleanup then removes the
  directory without running git status. `$GIT_DIR/info/attributes` marks `always_forbidden` files `-diff`
  (content never in diffs); the engine rewrites it if it differs, the reader refuses if it is missing lines.
- Workspaces are cloned with `--no-hardlinks` (no shared object inodes with the mirror/other jobs).
- Remote URLs: only ssh / scp-like / https / http / file / absolute paths; no `transport::`, no leading `-`, no
  embedded credentials. Branch names validated; paths always after `--` or via NUL pathspec files.
- Secrets: tokens/keys only via references; resolved values are registered with `DEFAULT_REDACTOR`. Commit
  messages, patches, audit details, events and GitLab payloads are redacted; URLs are stripped of userinfo.
  GitLab client: `PRIVATE-TOKEN` header, `follow_redirects=False`, bounded retries (429/5xx), duplicate MR
  detection (lookup before POST, re-lookup on 409).

## Configuration used

| Key | Use |
|---|---|
| `settings.repos_cache_dir` / `settings.workspaces_dir` (`HERMCLAW_DATA_DIR`) | mirror cache, workspaces, lock dir |
| `settings.gitlab_token_ref` (default `cred:gitlab-token`) | GitLab API token |
| `policies.git.branch_prefix`, `protected_branches`, `author_name`, `author_email`, `create_merge_request` | job branches, protection, identity, MR default |
| `policies.scope.always_forbidden` | staging refusal, `-diff` attributes, reader `show_base_file` refusal |
| `hosts[role=gitlab].ssh.key_ref` / `.known_hosts` | SSH transport (dedicated deploy key, pinned host key) |
| `hosts[role=gitlab].labels.api_url` (default `http://<address>`) | GitLab API base URL |

## Failure behaviour (error codes)

`PROTECTED_BRANCH` (403), `NOT_JOB_BRANCH` (403), `COMMIT_NOT_VERIFIED` (409), `NOTHING_TO_COMMIT` /
`NOTHING_TO_PUSH` (409), `PUSH_REJECTED` (409; remote refusal or foreign commits), `STALE_BASE_SHA`,
`MERGE_CONFLICT` (details `files`, `phase`), `BASE_BRANCH_NOT_FOUND` (404), `WORKSPACE_NOT_FOUND` (404),
`WORKSPACE_STATE` (409: cleaned/archived, missing dir, wrong branch, rebase in progress → `recover()`),
`WORKSPACE_TAMPERED` / `WORKSPACE_PATH_VIOLATION` (403), `SCOPE_VIOLATION`, `INVALID_REMOTE_URL` (422),
`GIT_COMMAND_FAILED`, `GIT_TIMEOUT`, `GIT_LOCK_TIMEOUT` (503), `GITLAB_ERROR`, `SECRET_REF_INVALID`.
Temporary clones are removed on any failure (workspace creation and mirror init are atomic renames); an
interrupted rebase/merge found later is reported as `WORKSPACE_STATE` and cleaned up by `recover()`.

## Operating on the real hosts

1. On `.225`: dedicated deploy key (`/etc/hermclaw/ssh/id_ed25519`, mode 0600, owner `hermclaw`), GitLab host
   key pinned in `/etc/hermclaw/ssh/known_hosts` (`ssh-keyscan 192.168.178.226` once, verify the fingerprint),
   API token as systemd credential `gitlab-token` (`LoadCredentialEncrypted=`).
2. On GitLab `.226`: protect `main`/`master`/`release/*`; give the deploy key / project access token push rights
   only on `hermclaw/*`.
3. Register the repository (`RepositoryRegistry.register(name, "git@192.168.178.226:group/project.git",
   provider="gitlab", gitlab_project_id="group/project")`) and run `sync_protected_branches`.
4. Live test (BLOCKER-001: skipped here): `HERMCLAW_LIVE_GITLAB_REPO_URL=… HERMCLAW_LIVE_GITLAB_API=…
   HERMCLAW_LIVE_GITLAB_PROJECT=… .venv/bin/pytest -m live tests/integration/test_gitops_live.py` – pushes a
   throw-away job branch, opens and re-finds an MR, checks local `main` refusal, deletes the branch.

## Tests

- `tests/unit/test_gitops_units.py` – naming, URL validation/redaction, porcelain/numstat/push parsers, staging
  guard decisions, commit messages, secret refs, hardened runner env, ext transport blocked, locks, GitLab client.
- `tests/unit/test_gitops_review.py` – lock bookkeeping, GitLab pagination, config allow-list / integrity checks.
- `tests/integration/test_gitops_engine.py` – registry, mirror clone/fetch/prune, base SHA, workspaces, job
  branches, status/diff (untracked, limits, forbidden content), reader protocol, safe staging, commit
  verification rules, cleanup (real git + PostgreSQL, `file://` bare remotes).
- `tests/integration/test_gitops_push.py` – push, protected-branch refusal (row/policy globs), remote rejection,
  force-with-lease, merge requests incl. duplicate detection against a local fake GitLab HTTP server.
- `tests/integration/test_gitops_hardening.py` – tampered `.git` (filter driver never executed), foreign
  commits on the job branch, verification freshness, concurrent commit with one run, lock re-validation,
  `--no-hardlinks`, rebased pushed workspace, redacted scope events, public `create_merge_request`.
- `tests/failure/test_gitops_failures.py` – stale base (incl. rewritten history), rebase/merge conflicts with
  clean abort, autostash conflicts, interrupted rebase recovery, timeouts, GitLab down, atomic creation.
- `tests/integration/test_gitops_live.py` – `@pytest.mark.live` against GitLab `.226`.

## Known limitations

- `verification_runs` has no workspace/tree hash column; coverage is checked by job/step, freshness and
  `changed_files`. A content hash would make the binding exact (shared schema change, not done here).
- Registering `file://`/local-path repositories is allowed (tests, local mirrors); registration is an operator
  action and should be restricted at the API layer in production.
- `create_workspace` returns an existing active workspace of the job/repository even if another `base_branch` is
  requested.
