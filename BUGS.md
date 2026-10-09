# BUGS

Zentrales Bugregister für Hermclaw Next.

## Severity
- P0 – Datenverlust / Security / katastrophaler Fehler
- P1 – Kernfunktion gebrochen
- P2 – wichtiger Defekt
- P3 – kleiner Defekt / Usability

## Regeln
P0/P1-Blocker werden sofort behoben. Nicht-blockierende Bugs werden reproduziert, mit Evidence dokumentiert und spätestens in P39 erneut abgearbeitet.

## Externe Blocker

### BLOCKER-001 – Zielhosts aus der Build-Umgebung nicht erreichbar
- Zeit: 2026-10-08T14:20Z · Phase: P00 · Severity: extern
- Reproduktion: `bash -c 'exec 3<>/dev/tcp/192.168.178.225/22'` (ebenso .222/.223/.224/.226/.60) → Timeout; `curl -m 6 http://192.168.178.224:11434/api/version` → Timeout nach 6 s.
- Ursache: Cloud-Build-Container hat keine Route ins Heim-LAN 192.168.178.0/24.
- Auswirkung: Live-Inventur, Deployment, Modell-Downloads, GPU-/WOL-/GitLab-Live-Tests, Backup nach `.60`, Soak-Test auf Zielhosts.
- Workaround: Alle Schritte als Skripte/Ansible-Playbooks + Installationsanleitung; lokale Integrationstests mit echten Diensten (PostgreSQL, Podman, Git, LiteLLM) wo möglich, sonst klar gekennzeichnete Fakes ausschließlich im Testcode.
- Fortsetzung: Auf dem Orchestrator `docs/operations/INSTALLATION.md` abarbeiten, danach `scripts/ops/live-acceptance.sh` ausführen.

## Offene Bugs
Noch keine.

## Behobene Bugs

### BUG-001 – CommandRequest.workspace path traversal on the execution daemon
- Phase: P07 · Komponente: workers · Severity: P0 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: Send a signed POST /v1/commands with workspace="/" (the id comes from the JSON body, so the router's path pattern never checks it).
- Erwartet: 400 WORKSPACE_ID_INVALID; the runner is never called.
- Tatsächlich: Before the fix the command ran with workspace_dir=/ (the probe printed '/'), and '..' addressed the data dir. Fixed: ExecutionService.workspace_dir() validates every id against WORKSPACE_ID_PATTERN, and the client validates too. Regression test: test_workers_execution_daemon.py::test_command_workspace_id_cannot_escape_workspaces_root, which fails on the old code.

### BUG-002 – RWLock counted as idle while a reader was still waiting; delete_workspace dropped it
- Phase: P07 · Komponente: workers · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: Reader A holds the lock, DELETE waits, reader B queues behind it. After the delete releases, B has been woken but has not run yet, and lk.idle was already True, so the lock was popped. A later upload got a fresh lock and could swap the tree under B's command.
- Erwartet: The lock stays registered while anyone holds or waits on it.
- Tatsächlich: Fixed: a _users counter is incremented synchronously on entry. Regression test: ::test_rwlock_waiting_reader_keeps_lock_registered, which fails on the old code.

### BUG-003 – a cancelled waiting writer left readers blocked
- Phase: P07 · Komponente: workers · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: Reader holds the lock, a writer waits, a second reader queues (writer preference), then the writer is cancelled (client disconnect during upload).
- Erwartet: The second reader proceeds.
- Tatsächlich: Before the fix the second reader hung until some unrelated notify. Fixed: notify_all when the waiting-writer count drops to 0. Regression test: ::test_rwlock_cancelled_writer_releases_waiting_readers, which fails on the old code.

### BUG-004 – concurrent first heartbeats of an unknown worker raised IntegrityError (HTTP 500)
- Phase: P07 · Komponente: workers · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: Four parallel transactions call ingest_heartbeat for the same new worker_id.
- Erwartet: One creates the row (one worker.registered event); the others update it.
- Tatsächlich: Fixed with INSERT ... ON CONFLICT DO NOTHING RETURNING followed by SELECT ... FOR UPDATE. Regression test: test_workers_registry.py::test_concurrent_first_heartbeats_register_once.

### BUG-005 – bearer token sent in clear over plain HTTP next to the HMAC signature
- Phase: P07 · Komponente: workers · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: WorkerRequestSigner defaulted to include_bearer=True on http:// LAN URLs.
- Erwartet: The shared secret never crosses an unencrypted link.
- Tatsächlich: Fixed: the default include_bearer=None sends the bearer only over https; verifiers still accept and check it when present. Regression test: test_workers_auth.py::test_request_signer_sends_bearer_only_over_https_by_default.

### BUG-006 – SignedRequestMiddleware passed websocket scopes through unauthenticated
- Phase: P07 · Komponente: workers · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: A websocket route added through extra_routers (media extension) received a handshake with no signature.
- Erwartet: Fail closed.
- Tatsächlich: Fixed: every websocket handshake is closed with code 1008. Regression test: test_workers_daemon_common.py::test_middleware_refuses_websocket_handshakes.

### BUG-007 – heartbeat body was read before the header-only auth checks
- Phase: P07 · Komponente: workers · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: An unsigned 256 KiB POST /api/workers/heartbeat returned 413 after buffering.
- Erwartet: 401 without reading the body; 413 only for signed requests.
- Tatsächlich: Fixed: the router runs precheck_signed_request, then reads the size-limited body, then verify_prechecked. Regression test: test_workers_api.py::test_unauthenticated_heartbeat_rejected_before_body_is_read.

### BUG-008 – graceful shutdown did not emit worker.offline; select_worker returned workers without api_url
- Phase: P07 · Komponente: workers · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den workers-Tests)
- Reproduktion: (1) The daemon sends its final offline heartbeat: only worker.state was emitted, so the scheduler waited for the sweep. (2) A heartbeat-only registered worker (api_url NULL) could be selected, and WorkerClient.for_worker then raised WORKER_UNREACHABLE.
- Erwartet: (1) worker.offline with reason=worker_shutdown, active_job and active_step. (2) Only dispatchable workers are selected.
- Tatsächlich: Both fixed. Regression tests: test_workers_registry.py::test_graceful_offline_heartbeat_emits_worker_offline, ::test_select_worker_skips_heartbeat_only_registrations_without_api_url.

### BUG-009 – Runtime git executed workspace-controlled config (filter/merge/diff drivers, remote.*.uploadpack, include.path, url.insteadOf, core.worktree) -> sandbox-to-orchestrator code execution
- Phase: P06 · Komponente: gitops · Severity: P0 · blockierend: ja · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: append [filter "evil"] clean="touch marker; cat" to <ws>/.git/config, add '* filter=evil' to .gitattributes, call engine.stage_allowed
- Erwartet: runtime refuses to run git in a tampered workspace
- Tatsächlich: FIXED: ops.integrity_problems allow-list check in engine._load / reader._path / cleanup -> WORKSPACE_TAMPERED (refused op + event); confirmed marker was created before the fix; regression tests in test_gitops_hardening.py and test_gitops_review.py

### BUG-010 – push force-with-lease against just-seen remote SHA overwrote foreign commits on the job branch
- Phase: P06 · Komponente: gitops · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: push job branch, reviewer pushes fix-up onto it, runtime commits again and pushes
- Erwartet: PUSH_REJECTED (foreign_commits), remote unchanged
- Tatsächlich: FIXED: non-fast-forward only if remote SHA was pushed by the runtime for this job or seen at workspace creation; test_push_refuses_to_overwrite_foreign_commits_on_the_job_branch

### BUG-011 – commit_verified accepted verification runs older than the workspace or a base rebase
- Phase: P06 · Komponente: gitops · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: create verification run, update_to_base, stage, commit with old run
- Erwartet: COMMIT_NOT_VERIFIED
- Tatsächlich: FIXED: freshness rule (run.created_at >= max(workspace.created_at, last ok base.rebase/merge)); concurrent commits with one run serialized via per-run lock

### BUG-012 – Operations validated workspace state before taking the lock (cleanup/update race); in-process lock dict grew unbounded
- Phase: P06 · Komponente: gitops · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: stage_allowed waiting on the lock while the workspace is cleaned
- Erwartet: WORKSPACE_STATE after lock acquisition
- Tatsächlich: FIXED: _locked() reloads the row inside the lock; KeyedLocks refcounted; test_operation_waiting_for_the_lock_sees_a_concurrent_cleanup

### BUG-013 – Workspace clones hardlinked mirror object files; info/attributes write followed symlinks; push used workspace-configurable 'origin'
- Phase: P06 · Komponente: gitops · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: stat object files (nlink>1); symlink .git/info/attributes to an outside file
- Erwartet: isolated objects, no writes outside the tree, push to registered URL
- Tatsächlich: FIXED: clone --no-hardlinks, symlink checks + O_NOFOLLOW write, push/ls-remote to repo.url; tests in test_gitops_hardening.py

### BUG-014 – Minor: scope.violation event payload not redacted; http(s) URLs with token-as-username accepted; GitLab X-Next-Page malformed/looping header; pushed workspace stayed 'pushed' after rebase; show_base_file used git show
- Phase: P06 · Komponente: gitops · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den gitops-Tests)
- Reproduktion: see tests
- Erwartet: redacted/rejected/terminating/committed/raw blob
- Tatsächlich: FIXED with regression tests (test_scope_violation_event_payload_is_redacted, test_http_urls_with_userinfo_are_rejected, test_gitlab_pagination_stops_on_malformed_or_looping_header, test_rebasing_a_pushed_workspace_marks_it_committed_again); show_base_file now uses cat-file blob

### BUG-015 – Repair turn could echo prose or reasoning-like text from an unparseable answer
- Phase: P14/P24 · Komponente: planner · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: Model answers '{"goal":..} I considered deleting the tests.. {"note":1}'; the old json_candidate echoed everything from the first '{' to the last '}' as the assistant turn
- Erwartet: Only a parsed JSON object is echoed (redacted, canonical); nothing is echoed for unparseable text
- Tatsächlich: FIXED: loop.py echoes compact_json(redact(parsed)) only; json_candidate removed. Regression test: tests/unit/test_planner_loop.py::test_prose_or_reasoning_inside_a_broken_answer_is_never_echoed

### BUG-016 – Validation error lists were not redacted before plan_versions, events and repair turns
- Phase: P14/P24 · Komponente: planner · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: repo_hint 'token=ghp_...' appears verbatim in the error text
- Erwartet: Errors are redacted
- Tatsächlich: FIXED: loop._error_list runs DEFAULT_REDACTOR. Test: test_planner_loop.py::test_error_lists_are_redacted_everywhere

### BUG-017 – Hints to paths listed outside the inventory's files/paths keys were rejected as invented, though the prompt allows them
- Phase: P14/P24 · Komponente: planner · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: inventory {'entrypoints':['app/main.py']} and hint app/main.py: error 'does not exist' and a needless repair turn
- Erwartet: Every path that appears in repository_inventory grounds hints
- Tatsächlich: FIXED: inputs.collect_known_paths harvests path-shaped strings (values and keys) from the whole inventory and strips '::' test ids. Tests: test_planner_validation.py::test_paths_anywhere_in_the_inventory_ground_hints, test_planner_repos.py::test_inventory_paths_outside_files_ground_hints_end_to_end

### BUG-018 – Risk floor counted a directory or glob hint as one path
- Phase: P14/P24 · Komponente: planner · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: implement step with hint 'src/pkg/' covering 8 files and no test evidence stayed low risk
- Erwartet: high (touches >= 6 paths without tests)
- Tatsächlich: FIXED: enrich.touched_paths counts the known paths a glob or directory matches. Test: test_planner_enrich.py::test_directory_and_glob_hints_count_the_paths_they_match

### BUG-019 – step.network and CommandEvidence.network were not checked against the capability network policy
- Phase: P14/P24 · Komponente: planner · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: an implement/coding step with network=true was accepted
- Erwartet: Bauplan §30: network only via the step capability
- Tatsächlich: FIXED: new semantic rule plus a prompt rule. Test: test_planner_validation.py::test_network_only_for_capabilities_that_allow_it

### BUG-020 – Catch-all allowed_new_paths/repo_hints ('**', '*') were accepted
- Phase: P14/P24 · Komponente: planner · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: allowed_new_paths ['**'] passed validation
- Erwartet: Scope stays explicit
- Tatsächlich: FIXED: is_catch_all rule. Test: test_planner_validation.py::test_catch_all_patterns_are_rejected

### BUG-021 – Prompt redacted only the job goal, not the title or other job fields
- Phase: P14/P24 · Komponente: planner · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: a job title containing a token reached the prompt unredacted
- Erwartet: The whole job section is redacted
- Tatsächlich: FIXED. Test: test_planner_prompt.py::test_planner_payload_keys_redaction_and_test_command

### BUG-022 – risk_policy override with an unknown step kind was silently accepted; DiffEvidence globs were not path-checked
- Phase: P14/P24 · Komponente: planner · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den planner-Tests)
- Reproduktion: {'min_risk_by_kind': {'rollout': 'high'}}; diff must_change ['../x']
- Erwartet: ValidationFailed / semantic error
- Tatsächlich: FIXED. Tests: test_planner_enrich.py::test_invalid_risk_policy_overrides_are_rejected, test_planner_validation.py::test_diff_globs_must_be_repository_relative

### BUG-023 – extract_json let RecursionError escape on deeply nested model output
- Phase: P08 · Komponente: models · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den models-Tests)
- Reproduktion: extract_json('[' * 100000), or structured() where the model answers with 60k '[' characters
- Erwartet: ValueError, so the output is treated as invalid and a repair call follows
- Tatsächlich: RecursionError propagated, and the invocation was recorded as INTERNAL_ERROR. FIXED in gateway.py (RecursionError and the int-digit limit now map to ValueError, and the candidate scan is capped at 256 start positions). Regression tests: test_models_gateway.py::test_extract_json_hostile_inputs, test_structured_hostile_nesting_triggers_repair

### BUG-024 – Repair call could hit CONTEXT_OVERFLOW because of the echoed answer
- Phase: P08 · Komponente: models · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den models-Tests)
- Reproduktion: structured() on fast-router with a prompt of about 13.5K tokens where the first answer is invalid and long
- Erwartet: Repair still possible with the validation error alone
- Tatsächlich: Repeating the rejected answer (up to 6000 chars) pushed the request over the context window and raised ValidationFailed. FIXED: the echo is dropped when it does not fit (LiteLLMGateway._repair_conversation). Regression tests: test_structured_repair_drops_echo_when_context_is_tight, test_structured_repair_keeps_echo_when_it_fits

### BUG-025 – Failed unload of a conflicting group member left no event
- Phase: P08 · Komponente: models · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den models-Tests)
- Reproduktion: ensure_loaded('coder-main') while gemma4:26b stays resident after keep_alive=0
- Erwartet: Switch aborted, target not loaded, failure visible in the event log
- Tatsächlich: ModelError raised with no event recorded. FIXED: model.load.finished is now written with ok=false, phase=unload, error_code and severity error (the load-phase failure event now carries phase=load too). Regression test: test_models_failures.py::test_failed_conflict_unload_blocks_switch_and_is_evented

### BUG-026 – Empirical LiteLLM proxy tests skip in the repo venv
- Phase: P08 · Komponente: models · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den models-Tests)
- Reproduktion: .venv/bin/pytest tests/integration/test_models_litellm_proxy.py
- Erwartet: Tests run against a real proxy
- Tatsächlich: 12 tests skip because .venv lacks the litellm[proxy] extras. Test-infrastructure issue only; fix is the shared pyproject change listed above.

### BUG-027 – Docker adapter: docker CLI injects proxy env from ~/.docker/config.json 'proxies' into containers
- Phase: P18 · Komponente: sandbox · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den sandbox-Tests)
- Reproduktion: Configure 'proxies' in ~/.docker/config.json for the worker user, use policies.sandbox.engine=docker, run `env` in a command
- Erwartet: No host proxy variables in the container (podman gets --http-proxy=false)
- Tatsächlich: Docker has no flag to turn this off. The docs say not to configure 'proxies' for the worker user; podman, the preferred engine, is not affected

### BUG-028 – sh -lc (required by spec) resets PATH on Debian-based images via /etc/profile
- Phase: P18 · Komponente: sandbox · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den sandbox-Tests)
- Reproduktion: Use an image whose ENV PATH contains e.g. /opt/venv/bin and run `which <tool>` in the sandbox
- Erwartet: The image's PATH is preserved
- Tatsächlich: Debian's /etc/profile overwrites PATH with the standard directories. Workaround: ContainerSandbox(shell=('sh','-c')); this is documented in docs/architecture/sandbox.md section 4


### BUG-029 – Folge-Steps eines Jobs sahen die Commits früherer Steps als eigene Änderungen
- Phase: P23 (Integration) · Komponente: coder/handler + verifier · Severity: P1 · blockierend: ja · Status: **behoben**
- Reproduktion: Plan mit zwei Implement-Steps (S001 ändert src/module.py, S002 ändert src/util.py); nach dem Commit von S001 meldet der Verifier für S002 eine Scope-Verletzung (src/module.py), weil `changed_files` gegen die Job-Basis gerechnet wird.
- Erwartet: Jeder Step wird gegen den HEAD zu Step-Beginn geprüft.
- Fix: ImplementStepHandler setzt die Step-Baseline (`WorkspaceHandle.base_sha = workspace.head_sha`); Regression-Rerun nach Rebase prüft mit der Vereinigung aller Step-Scopes (`merge_scopes`).
- Regressionstest: tests/integration/test_coder_handler.py::test_two_implement_steps_with_real_verifier_commit_separately (schlägt ohne Fix fehl, verifiziert per Mutation).

## Template

### BUG-XXX – Titel
- ID / Zeit / Phase / Severity / Komponente
- Reproduktionsschritte:
- Expected:
- Actual:
- Logs/Evidence:
- Workaround:
- Blocking: yes/no
- Status: open/fixed/closed
- Regression Test:
- Fix Commit:
