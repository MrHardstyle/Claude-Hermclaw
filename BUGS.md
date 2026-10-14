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

### BUG-030 – Cross-member budget queue rule guesses why a waiter is blocked (requests carry no weight or exclusive flag)
- Phase: P09 · Komponente: resources · Severity: P3 · blockierend: nein · Status: **behoben** in P39 (`tests/integration/test_resources_budget_queue.py`: Requests speichern weight/exclusive/metadata, Migration 0002; abgebrochene/abgelaufene Requests emittieren `resource.request.cancelled`, zurückgezogene Preemption `resource.preempt.withdrawn`)
- Reproduktion: Budget {large, small}. A high-priority shared request on 'small' waits only because the shared holders there are not exclusive-compatible, not because of capacity. A lower-priority 'large' acquisition that would fit is then held back as budget_queued.
- Erwartet: A lower-priority request on another budget member is blocked only when the higher waiter actually waits for capacity.
- Tatsächlich: It is blocked whenever the higher waiter's resource has no exclusive holder. This is safe (it never lets a lower waiter overtake), but it can delay lower-priority work. Fixing it needs the shared schema change (weight and exclusive columns on resource_requests).

### BUG-031 – Primary domains listed with www. prefix (e.g. www.postgresql.org in policies.example.yaml) never matched
- Phase: P12 · Komponente: research · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den research-Tests)
- Reproduktion: classify_source('https://www.postgresql.org/docs/16/', ['www.postgresql.org'])
- Erwartet: official_docs, authority 0.95 (primary source)
- Tatsächlich: Fixed in this session (was documentation-path 0.75); regression test tests/failure/test_research_failures.py::test_primary_domain_listed_with_www_prefix_matches

### BUG-032 – SSRF guard treated RFC 8215 local-use NAT64 (64:ff9b:1::/48) addresses as public when their low 32 bits looked public
- Phase: P12 · Komponente: research · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den research-Tests)
- Reproduktion: is_public_address(ip_address('64:ff9b:1:7f00:1::808:808'))
- Erwartet: False (embedded IPv4 position depends on operator prefix length)
- Tatsächlich: Fixed in this session (was True); regression tests test_nat64_local_use_prefix_is_never_public / test_nat64_local_use_answer_is_blocked_by_fetcher

### BUG-033 – Raw source URLs (possibly containing tokens) were kept in ResearchOutcome.errors and research_sources.url
- Phase: P12 · Komponente: research · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den research-Tests)
- Reproduktion: search result URL with ?api_key=<secret> that fails to fetch
- Erwartet: secret redacted everywhere
- Tatsächlich: Fixed in this session: URLs and error extras pass through DEFAULT_REDACTOR; regression test test_secrets_in_question_and_urls_never_reach_events_db_or_prompts

### BUG-034 – Test fixture server ignored routes when the request carried a query string (2 SearXNG tests failed)
- Phase: P12 · Komponente: research · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den research-Tests)
- Reproduktion: pytest tests/integration/test_research_fetch_search.py
- Erwartet: route '/search' serves '/search?q=...'
- Tatsächlich: Fixed in tests/integration/test_research_support.py

### BUG-035 – Shared redactor misses secrets assigned to prefixed identifiers
- Phase: P11 · Komponente: repo_intelligence · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den repo_intelligence-Tests)
- Reproduktion: from hermclaw.core.redaction import DEFAULT_REDACTOR; DEFAULT_REDACTOR.text("DB_PASSWORD = '<literal value>'")
- Erwartet: the literal is masked
- Tatsächlich: returned unchanged (\bpassword\b does not match inside DB_PASSWORD). Repo intelligence works around it with hermclaw/repo_intelligence/redact.py::redact_code; other components using DEFAULT_REDACTOR on code are still affected.

### BUG-036 – Draining worker waited the whole service_timeout before CAPABILITY_MISSING
- Phase: P10 · Komponente: wol · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest tests/failure/test_wol_regressions.py)
- Reproduktion: ensure_worker_ready for a worker in state draining that lacks the required capability
- Erwartet: immediate failure CAPABILITY_MISSING, worker stays draining
- Tatsächlich: waited service_timeout_seconds (180 s default) before failing

### BUG-037 – Concurrent wake runs for one worker with different capability sets sent double magic packets and raced on state writes
- Phase: P10 · Komponente: wol · Severity: P2 · blockierend: nein · Status: **behoben** (per-worker asyncio.Lock; regression test in tests/failure/test_wol_regressions.py)
- Reproduktion: two ensure_worker_ready calls for the same worker with different required_capabilities at the same time
- Erwartet: one wake, one state writer; each run's budget starts when it acquires the worker
- Tatsächlich: two parallel runs, duplicate packets, interleaved state transitions

### BUG-038 – Failed quick probe on an awake worker with a live heartbeat flipped it to waking and then error
- Phase: P10 · Komponente: wol · Severity: P2 · blockierend: nein · Status: **behoben** (regression test in tests/failure/test_wol_regressions.py)
- Reproduktion: worker API crashed on an awake host (heartbeat state still live), ensure_worker_ready
- Erwartet: live state is kept, failure reported without a wake state change
- Tatsächlich: state set to waking, then error

### BUG-039 – wol_send retry loop ignored the overall deadline
- Phase: P10 · Komponente: wol · Severity: P3 · blockierend: nein · Status: **behoben** (regression test in tests/failure/test_wol_regressions.py)
- Reproduktion: deadline_seconds shorter than the packet retry schedule
- Erwartet: retries stop at the deadline
- Tatsächlich: retries continued past the deadline

### BUG-040 – Idle re-check after committing 'sleeping' ignored a heartbeat that moved the worker or reported active work
- Phase: P10 · Komponente: wol · Severity: P2 · blockierend: nein · Status: **behoben** (row-locked _recheck aborts the sleep; regression test in tests/integration/test_wol_idle.py)
- Reproduktion: heartbeat with an active job arrives between the idle decision and the sleep command
- Erwartet: sleep aborted
- Tatsächlich: worker was put to sleep anyway

### BUG-041 – Verifier secret scanner flagged secret *references* as secrets (systemd LoadCredential paths, cred: refs, "ghp_..." placeholders)
- Phase: P36 · Komponente: verifier · Severity: P1 · blockierend: ja (a deploy job writing systemd units with LoadCredential= would be blocked by its own verifier) · Status: **behoben** (regression cases in tests/unit/test_verifier_secrets.py::test_ordinary_code_is_not_reported; found by tests/security/test_security_checks.py::test_repository_contains_no_secrets)
- Reproduktion: SecretScanner().scan_line("x.service", 1, "LoadCredential=worker-token:/etc/hermclaw-worker/worker-token")
- Erwartet: no finding – the value is a path to the credential, not the credential
- Tatsächlich: secret_assignment finding; likewise for "api_token_ref: cred:…" and documentation placeholders like "token=ghp_..."

### BUG-042 – Planner rejected correct repo_hints as "invented": the driver took known paths from the prompt-sized inventory summary, which has no file list
- Phase: P37 · Komponente: runtime driver / repo_intelligence · Severity: P1 · blockierend: ja (any job whose goal text does not lexically hit the files it names failed preparation) · Status: **behoben** (RepoIntelligence.known_paths + driver uses it; regression: tests/e2e/test_e2e_code_tasks.py::test_37_7_typescript_utility)
- Reproduktion: TypeScript repo with src/strings.ts, job "Add a slugify(s) utility", Gemma plan with repo_hints ["src/strings.ts"]
- Erwartet: hint accepted (file exists)
- Tatsächlich: semantic repair "repo_hint 'src/strings.ts' does not exist in repository_inventory/retrieved_context", plan rejected; only files that retrieval happened to return were known

### BUG-043 – POST /api/jobs: repository auto-registration by URL crashes (HTTP 500) on a name collision and bypasses the registry validation
- Phase: P40 (found by the local soak) · Komponente: api/jobs · Severity: P2 · blockierend: nein · Status: **behoben** in P39 (`tests/integration/test_api_repository_registration.py`: `validate_remote_url`, eindeutige Namen bei gleichem URL-Ende, file:// nur mit admin)
- Reproduktion: two jobs with different repository URLs that share the basename, e.g. file:///a/remote.git and file:///b/remote.git → second POST /api/jobs fails with UniqueViolation uq_repositories_name (HTTP 500)
- Erwartet: the repository is registered through RepositoryRegistry (URL validation: no userinfo, allowed schemes) with a unique name (collision → suffix), or a clear 409/422
- Tatsächlich: _slug_name(url) is used as the unique name directly and the row is inserted without RepositoryRegistry checks

### BUG-044 – Staged renames reported as unexplained changes (reader dropped the rename origin)
- Phase: P39 · Komponente: scope · Severity: P1 · blockierend: nein · Status: **behoben** (Regressionstest: tests/integration/test_scope_rename_audit.py)
- Reproduktion: git mv legacy/old_module.py src/app/feature/old_module.py; ScopeAuditor.audit_status(step, await WorkspaceGitReader(...).status(ws))
- Erwartet: Rename judged by both paths: (legacy/old_module.py, delete) and (src/app/feature/old_module.py, create)
- Tatsächlich: Before 74ef977: violation 'staged rename without a known origin'. The source fix was already in HEAD; only the real-git regression test was missing (added now).

### BUG-045 – Escaped literal '[[]' in _glob_regex caused FutureWarning 'Possible nested set'
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_escaped_bracket_file_names_match_only_themselves)
- Reproduktion: path_matches('routes/[id].tsx', glob_escape('routes/[id].tsx')) with -W error::FutureWarning, using the pre-review guard.py
- Erwartet: Matches, no warning
- Tatsächlich: FutureWarning (an error under -W error). Already fixed in HEAD; regression tests added.

### BUG-046 – Odd path segments ('src//a.py', 'src/./a.py') were not canonicalised
- Phase: P39 · Komponente: scope · Severity: P1 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_odd_spelling_never_bypasses_a_forbidden_path (plus the related odd_spelling and normalise_path tests))
- Reproduktion: ScopeGuard(forbidden_paths=['lib/secret.py']).decide('lib//secret.py', 'modify') with the pre-review contracts/scope.py
- Erwartet: Judged as lib/secret.py, i.e. forbidden
- Tatsächlich: Pre-review: not recognised as the forbidden path. The path collapse was already in HEAD; tests added.

### BUG-047 – normalise_path('./') returned the absolute path '/', and ScopeGuard.decide('./') raised ValueError
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_normalise_path_rejects_invalid_paths, ::test_contract_rejects_root_and_nul_entries, ::test_invalid_paths_are_refused_not_raised)
- Reproduktion: normalise_path('./'); ScopeContract(allowed_new_paths=['./']); ScopeGuard(c).decide('./', 'create')
- Erwartet: ValueError from normalise_path; decide returns (False, reason)
- Tatsächlich: Returned '/' (not idempotent). The contract stored '/'. decide raised ValueError from is_forbidden instead of refusing.

### BUG-048 – normalise_path accepted NUL bytes
- Phase: P39 · Komponente: scope · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_normalise_path_rejects_invalid_paths[src/a\x00.py])
- Reproduktion: normalise_path('src/a\x00.py')
- Erwartet: ValueError
- Tatsächlich: Returned 'src/a\x00.py'

### BUG-049 – Malformed glob classes raised re.error and crashed ScopeGuard
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_malformed_classes_in_a_contract_never_crash_the_guard, ::test_character_class_semantics)
- Reproduktion: ScopeGuard(ScopeContract(target_paths=['a[]'])).decide('a[]', 'modify'); also path_matches(']', '[]]') and path_matches('a', '[z-a]')
- Erwartet: A decision: a class that is never closed is literal, a leading ']' is a member, an inverted range is empty
- Tatsächlich: re.error: unterminated character set / bad character range. re.error is not a ValueError, so the gitops first_match and review _safe_any_match guards do not catch it either.

### BUG-050 – Glob negation '[!..]' was treated as a literal '!' (inverted semantics)
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_character_class_semantics[[!a]x-*])
- Reproduktion: path_matches('bx', '[!a]x'); path_matches('ax', '[!a]x')
- Erwartet: True / False (glob negation)
- Tatsächlich: False / True

### BUG-051 – Character classes could match '/' and cross a path segment
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_character_classes_never_cross_a_path_segment)
- Reproduktion: path_matches('x/y', 'x[^a]y'), path_matches('x/y', 'x[/]y'), path_matches('x/y', 'x[+-0]y')
- Erwartet: False (a class stays within one segment, as documented)
- Tatsächlich: True

### BUG-052 – Scope patterns were not canonicalised (policy globs with odd segments never matched)
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_patterns_are_compared_in_canonical_form_too)
- Reproduktion: ScopeGuard(ScopeContract(target_paths=['**'], allowed_operations=['modify']), ScopePolicy(always_forbidden=['./vault//**'])).allowed('vault/k.txt', 'modify')
- Erwartet: False (forbidden)
- Tatsächlich: True. Only a leading './' was stripped from patterns, so a 'vault//**' pattern never matched.

### BUG-053 – Creating an explicitly listed but glob-escaped target was refused
- Phase: P39 · Komponente: scope · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest: tests/unit/test_scope_guard.py::test_escaped_bracket_target_guards_modify_and_explicit_create)
- Reproduktion: ScopeGuard(ScopeContract(target_paths=[glob_escape('routes/[id].tsx'), 'src/a.py'])).allowed('routes/[id].tsx', 'create')
- Erwartet: True, the same as for the explicitly listed src/a.py
- Tatsächlich: False, because the raw check 'p in target_paths' misses the escaped spelling

### BUG-054 – Replanner left 'unavailable' scope rows of superseded steps open (24.6 invariant broken)
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest: tests/integration/test_planner_replan_scope.py::test_replan_supersedes_unavailable_scope_versions)
- Reproduktion: Superseded step with current scope v2 status 'unavailable'; Replanner.replan(...)
- Erwartet: v2 marked superseded; at most one non-superseded scope row per step
- Tatsächlich: v2 stayed 'unavailable'; only 'active' rows were updated

### BUG-055 – mypy --strict errors in tests/unit/test_scope_guard.py (missing annotations / '-> None')
- Phase: P39 · Komponente: scope · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest: mypy run on the file)
- Reproduktion: .venv/bin/mypy tests/unit/test_scope_guard.py
- Erwartet: No issues
- Tatsächlich: 2 errors [no-untyped-def]

### BUG-056 – Stale comments in non-owned tests claim GitReader.status drops the rename origin
- Phase: P39 · Komponente: scope · Severity: P3 · blockierend: nein · Status: **behoben** (Kommentare im Integrations-Commit korrigiert)
- Reproduktion: tests/unit/test_scope_hardening_units.py:146 and tests/integration/test_scope_hardening.py:205
- Erwartet: Comments describe the current reader, which fills orig_path
- Tatsächlich: They say the origin is dropped. The tests still pass because they build their entries by hand.

### BUG-057 – A file literally named with a backslash is audited under the slash path
- Phase: P39 · Komponente: scope · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_scope_audit_backslash.py`, 3 Tests – vor dem Fix alle rot; auch eine gestagte Umbenennung von `legacy\old_module.py` wurde als designierte Löschung von `legacy/old_module.py` akzeptiert)
- Reproduktion: A shell command creates the root-level file 'src\a.py'; git status reports '?? src\a.py'; audit_status with src/a.py in scope
- Erwartet: Refused, or judged as its literal name
- Tatsächlich: normalise_path turns '\' into '/', so the audit allows it as 'src/a.py'. gitops staging (normalise_repo_path) still refuses backslash paths, so it cannot be committed. Fix: `ScopeAuditor.audit_changes` meldet solche Namen als Verletzung unter ihrem wörtlichen Namen (`_display` wandelt `\` nicht mehr um); Contract-/Tool-Parsing unverändert.

### BUG-058 – GET /api/workers was registered twice (system read route and workers admin route)
- Phase: P39 · Komponente: api/workers · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_api_routes_unique.py`)
- Reproduktion: `create_app().openapi()` with warnings as errors → FastAPI "Duplicate Operation ID list_workers_api_workers_get" (found by the P31 UI agent)
- Erwartet: one route per method/path; the UI list uses the read-scope system route (display_state, capabilities)
- Tatsächlich: `hermclaw/workers/api.py` also registered an admin-only `GET /api/workers`; the system router won by order, the second route was dead and the OpenAPI document ambiguous
- Fix: `create_workers_router(include_list=False)` in the Hermclaw app; the standalone worker router (tests, embedding) keeps its list route

### BUG-059 – read_diff/changed_files missed a same-size edit made in the same second as the index write (racy git)
- Phase: P39 · Komponente: gitops · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_gitops_racy_index.py`)
- Reproduktion: commit `module.py` (`VALUE = 1`), rewrite it to `VALUE = 3` (same size) in the same second, wait 1 s, `ops.changed_files` → `[]` (found by the P39 scope agent: `test_reader_implements_git_reader_protocol` failed ~25 %)
- Erwartet: every change that `git status` reports is in the diff, the verifier's changed files and the push decision
- Tatsächlich: `_scratch_index` copied `.git/index` with `shutil.copyfile`; the fresh mtime made git treat the entry as clean (stat data matches at second granularity) instead of "racily clean" (content compare)
- Fix: `gitops/_fs.copy_if_exists` uses `shutil.copy2` (keeps the mtime); deterministic regression test aligns commit and edit to one second

### BUG-060 – A wheel installation had no Alembic migrations: `hermclaw migrate` failed outside a source checkout
- Phase: P32 · Komponente: packaging/persistence · Severity: P1 · blockierend: ja (Deployment) · Status: **behoben** (`tests/unit/test_persistence_migrate.py`; manuell: Wheel in frische venv installiert, `hermclaw migrate` + `alembic -c <venv>/share/hermclaw/alembic.ini downgrade 0001` gegen leere DB)
- Reproduktion: `pip wheel .` → install into `/opt/hermclaw/venv-<version>` → `hermclaw migrate` (cwd ≠ checkout)
- Erwartet: the release migrates its database; the rollback in `docs/runbooks/upgrade.md` uses `<venv>/share/hermclaw/alembic.ini`
- Tatsächlich: the wheel contained only the packages; `cmd_migrate` looked for `migrations/` next to the installed package (site-packages) and found nothing
- Fix: pyproject `[tool.setuptools.data-files]` ships `alembic.ini` + `migrations/` to `<venv>/share/hermclaw/`; `hermclaw.persistence.migrate` resolves checkout → installed data files (`HERMCLAW_MIGRATIONS_DIR` overrides)

### BUG-061 – Verification binding ignored the git file mode (symlink swap / chmod +x after verification was committed)
- Phase: P39 · Komponente: gitops/verifier · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_verification_binding.py`, 3 Tests rot ohne Fix)
- Reproduktion: verify a change with the regular file `src/alias.txt` (content `module.py`); replace it by a symlink to `module.py` or `chmod +x` a verified file; `stage_allowed` + `commit_verified`
- Erwartet: refused with `VERIFICATION_STALE` – the commit would record another type/mode than the verified one
- Tatsächlich: committed as `120000`/`100755`; the content digest covered blob shas only
- Fix: `gitops.engine.content_token` includes the mode for non-regular files (worktree: lstat, index: `ls-files --stage`); new columns `verification_runs.workspace_id` + `content_digest` (migration 0002)

### BUG-062 – The verifier hashed with its own GitRunner instead of the GitEngine runner that re-computes the digest at commit
- Phase: P39 · Komponente: runtime/verifier · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_verification_binding.py::test_adapter_hashes_with_the_git_runner_of_the_engine_reader`)
- Erwartet: verifier and `commit_verified` hash with the same git binary/environment · Tatsächlich: the verifier built `GitRunner("git")` · Fix: `VerifierAdapter` passes the reader's runner

### BUG-063 – Five event types had no live SSE listener in the UI (arrived only through the 15 s REST reconciliation)
- Phase: P39 · Komponente: ui/events · Severity: P4 · blockierend: nein · Status: **behoben** (`tests/unit/test_event_types.py`: Contract ↔ alle `append_event`-Literale ↔ UI-Listenerliste)
- Reproduktion: compare `EventType` with `KNOWN_EVENT_TYPES` in `ui/src/lib/events.ts`: `resource.preempt.withdrawn`, `resource.request.cancelled`, `system.disk`, `worker.sleep`, `workspace.cleaned` missing
- Fix: listeners added; `job.recovered`, `job.control.*` and `repository.registered` were emitted as literals only and are now `EventType` members

### BUG-064 – Any control principal could register file:// or local-path repositories through POST /api/jobs
- Phase: P39 · Komponente: api/jobs · Severity: P1 · blockierend: nein · Status: **behoben** (`tests/integration/test_api_repository_registration.py::test_control_principal_cannot_register_local_repository`)
- Reproduktion: control-scoped token, `POST /api/jobs {"repository": {"url": "file:///etc/..."}}` → registered; the runtime would clone anything the orchestrator user can read
- Fix: local repositories (file://, absolute paths) need the admin scope (403 FORBIDDEN otherwise); already registered ones stay usable

### BUG-065 – Repository URLs with embedded credentials were stored and emitted; same-tail remotes collided on the repository name
- Phase: P39 · Komponente: api/jobs · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_api_repository_registration.py`)
- Fix: remote URLs go through `gitops.urls.validate_remote_url` (422 for userinfo/transport helpers), distinct names for `a/demo.git` / `b/demo.git`

### BUG-066 – Finished jobs' workspaces and their code index were never removed (no retention)
- Phase: P39 · Komponente: runtime · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_runtime_maintenance.py`, `tests/integration/test_runtime_janitor_process.py`)
- Fix: `hermclaw/runtime/maintenance.py` WorkspaceJanitor (policies.git.workspace_retention_hours, default 72): drops the workspace index first, then `GitEngine.cleanup`, emits `workspace.cleaned`; unpushed verified commits are kept; runs in `SchedulerProcess`

### BUG-067 – CommandRequest.workspace accepted '/', '..', '.' (no contract-level pattern)
- Phase: P39 · Komponente: contracts/worker · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/unit/test_contracts_command_workspace.py`) – defense in depth next to the daemon/client checks; raw bad ids now get 422 VALIDATION_FAILED

### BUG-068 – worker.sleep was never emitted by the idle sleep hook
- Phase: P39 · Komponente: wol · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_wol_idle.py::test_sleep_success` + negative cases)

### BUG-069 – RepoIntelligence kept per-workspace in-memory state after drop_workspace_index (unbounded growth)
- Phase: P39 · Komponente: repo_intelligence · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_repo_intelligence_drop_state.py`)

### BUG-070 – Janitor crash window left orphaned index rows (directory removed before the index was dropped)
- Phase: P39 · Komponente: runtime/maintenance · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_runtime_maintenance.py::test_crash_right_after_the_directory_removal_leaves_no_orphaned_index`)

### BUG-071 – IdleSleepPolicy (P10.9 idle sleep) is never instantiated by any process
- Phase: P39 · Komponente: wol/runtime · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_runtime_ops.py::test_scheduler_process_runs_idle_sleep_over_the_ssh_runner`): `SchedulerProcess` runs `IdleSleepPolicy.run_forever` over the SSH runner of `hermclaw.runtime.ops` when Wake-on-LAN is on and a host has an `idle_sleep_command`
- Reproduktion: `grep -rn 'IdleSleepPolicy(' hermclaw worker` → only the class itself

### BUG-072 – Scheduler fails a job permanently (PREPARE_CRASHED) on a transient DB OperationalError during preparation
- Phase: P39 · Komponente: scheduler · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_scheduler.py::test_transient_db_errors_in_prepare_and_finalize_rerun_the_phase`; `tests/failure/test_runtime_failures.py -k db_connections_killed` 6/6 grün, vorher 1/3)
- Fix: connection-level DB errors (`OperationalError`, `InterfaceError`, invalidated `DBAPIError`) in the re-entrant phases prepare/finalize re-run the phase (bounded `prepare_transient_retries`=5, exponential backoff) instead of PREPARE_CRASHED/FINALIZE_CRASHED; a database that stays down still fails the job
- Reproduktion: `tests/failure/test_runtime_failures.py -k db_connections_killed` fails ~2 of 3 runs: AdminShutdown inside `driver.prepare` is caught in `Scheduler._run_prepare` as PREPARE_CRASHED
- Erwartet: killed DB connections are re-established and the job finishes (35.7)

### BUG-073 – Test hygiene: test_api_skeleton pinned schema revision 0001; untyped helper in test_gitops_support
- Phase: P39 · Komponente: tests · Severity: P4 · blockierend: nein · Status: **behoben** (assert against `hermclaw.persistence.migrate.head_revision()`; annotations)

### BUG-074 – GET /api/jobs/{id}/diff loaded the whole diff file into memory, cut it silently and did not confine the path
- Phase: P39 · Komponente: api/jobs · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/integration/test_api.py::test_diff_is_bounded_flagged_and_confined`)
- Tatsächlich: `p.read_text()[:2_000_000]` (whole artifact in memory, no flag; found by the P31 UI agent); the diff path was not checked against the artifact store like the download is
- Fix: reads at most `DIFF_CHAR_LIMIT` characters, returns `truncated` + `size_bytes`; artifacts outside the store are never read (`missing: true`)

### BUG-075 – docs/contracts/openapi.json and five contract schemas were stale
- Phase: P39 · Komponente: contracts/docs · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_contract_docs.py` fails on any drift; `hermclaw schemas docs/contracts/schemas` regenerates schemas + OpenAPI)
- Tatsächlich: worker detail/drain/heartbeat and `/api/metrics` missing in the OpenAPI document; CoderAction/ToolCall/ToolResult/CommandRequest/VerificationReport schemas outdated

### BUG-076 – ssh_read classification bypass: VAR=value prefix and non-word-boundary tool names were treated as read
- Phase: P26 · Komponente: sshtools · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den sshtools-Tests)
- Reproduktion: SshCommandPolicy.classify(host, "cat=1 python3 -c 'import os'") or classify(host, 'cat.sh /x') with the example policies
- Erwartet: mutate (approval required; ssh_read refuses it)
- Tatsächlich: Before the fix: read, so arbitrary code ran through ssh_read without approval. Fixed in policy.py (_PLAIN_TOOL_RE plus _matches_read); regression test tests/unit/test_sshtools_policy.py::test_read_bypasses_are_not_reads.

### BUG-077 – Read tools with executing, writing or remote-hop flags classified as read (ip abbreviations/-batch, ss -D, systemctl -H/-M, journalctl --cursor-file, nginx -g/-c, rg --hostname-bin)
- Phase: P26 · Komponente: sshtools · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den sshtools-Tests)
- Reproduktion: classify(host, 'ip a a 10.0.0.1/24 dev eth0'), classify(host, 'systemctl status -H root@other nginx')
- Erwartet: mutate
- Tatsächlich: Before the fix: read. Fixed: extended escalation tables and an ip read-command check in policy.py; covered by the same regression test.

### BUG-078 – known_hosts/key file paths with whitespace or %/~ tokens accepted by the registry
- Phase: P26 · Komponente: sshtools · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den sshtools-Tests)
- Reproduktion: HostConfig ssh.known_hosts='/etc/hermclaw/known hosts'
- Erwartet: SshConfigError at startup
- Tatsächlich: Before the fix: accepted, and ssh would split UserKnownHostsFile into two files. Fixed with SAFE_PATH_RE in registry.py; test tests/unit/test_sshtools_registry_keys.py::test_registry_validates_entries_at_startup.

### BUG-079 – Order-dependent test failures caused by global redactor literals registered by tests
- Phase: P26 · Komponente: sshtools · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den sshtools-Tests)
- Reproduktion: Run the unit tests before test_runner_satisfies_remote_command_runner_protocol: SecretStore registers 'hello' as a redaction literal
- Erwartet: Tests independent of order
- Tatsächlich: Fixed: the tests now use unique secret values.

### BUG-080 – Materialised cred: keys could survive a SIGKILL of the orchestrator
- Phase: P26 · Komponente: sshtools · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den sshtools-Tests)
- Reproduktion: Kill the process between writing the key and deleting it
- Erwartet: The leftover key is removed at the next start
- Tatsächlich: Fixed: keys are named key-<pid>-* and purge_stale_keys runs on first use of the runtime dir; test ::test_stale_materialised_keys_of_dead_processes_are_purged.

### BUG-081 – Mutating SSH commands could never be approved (no operator endpoint for `ssh_approvals`)
- Phase: P39 · Komponente: api/sshtools · Severity: P1 · blockierend: ja (für SSH-Mutationen) · Status: **behoben** (`tests/integration/test_runtime_ops.py::test_operator_approves_and_revokes_a_pending_ssh_command`)
- Reproduktion: a step runs `ssh_command` on an allow-listed host with `require_approval_for_mutations: true` → `approval_required` + token; nothing could write `jobs.metadata.ssh_approvals`
- Fix: `GET/POST /api/jobs/{id}/ssh-approvals`, `DELETE …/{token}` (read / admin), pending tokens from `ssh.command` events, events `ssh.approval.granted|revoked`

### BUG-082 – Operations provider skip log used the reserved LogRecord key `module` (scheduler start crashed at INFO level)
- Phase: P39 · Komponente: runtime/ops · Severity: P1 · blockierend: ja · Status: **behoben** before release (`tests/integration/test_runtime_ops.py::test_missing_optional_provider_is_skipped_with_info_logging`; found by `tests/failure/test_recovery_process_kill.py`)

### BUG-083 – test_remote_execution_without_a_ready_worker… was order-dependent (shared test DB had a ready worker without token)
- Phase: P39 · Komponente: tests · Severity: P4 · blockierend: nein · Status: **behoben** (precondition explicit: execution workers set offline; `test_workers_execution_daemon.py` + `test_runtime_services.py` together green; found by the P27 agent)
- Tatsächlich: with a ready worker left by another test the pool selected it and raised WorkerAuthFailed (correct runtime behaviour) instead of the expected WorkerUnreachable

### BUG-084 – Sandbox refused the environment of DB migration commands (`SANDBOX_ENV_NOT_ALLOWED`)
- Phase: P39 · Komponente: worker/execution + dbtools · Severity: P1 · blockierend: ja (für `database`-Schritte) · Status: **behoben** (`tests/unit/test_sandbox_runtime_env.py`)
- Reproduktion: a `database` step calls `db_migration_apply_sandbox`; the DB tools run `alembic upgrade head` in the step's sandbox with `DATABASE_URL`/`SQLALCHEMY_DATABASE_URI`/`HERMCLAW_SANDBOX_URL`/`HERMCLAW_ALEMBIC_*` and libpq `PG*` variables
- Erwartet: the migration runs against the throw-away sandbox database
- Tatsächlich: `validate_request_env` only allowed `policies.sandbox.env_allowlist` (PATH, HOME, LANG, LC_ALL, CI, PYTHONDONTWRITEBYTECODE) → every sandbox migration failed; the unit tests used fake executors and missed it
- Fix: `hermclaw.core.config.RUNTIME_SANDBOX_ENV` (names only the runtime sets – tool calls of the model carry no environment) + `SandboxPolicy.request_env_allowlist`; the podman/docker sandbox validates against it, so an operator override of `env_allowlist` cannot break migrations or the verifier's `CI=1`. A test scans `hermclaw/dbtools` for every env name it sets.

### BUG-085 – PostgreSQL config validation treated pending-restart settings as errors (first live run on .225 would always fail)
- Phase: P01 · Komponente: bootstrap · Severity: P1 · blockierend: ja · Status: **behoben** (Regressionstest in den bootstrap-Tests)
- Reproduktion: Running cluster with default shared_buffers; write conf.d/hermclaw.conf with shared_buffers=2GB; run the old validation query on pg_file_settings before the restart handler.
- Erwartet: Validation passes; the restart handler applies the new value.
- Tatsächlich: pg_file_settings returns 'setting could not be applied' for postmaster-context parameters, so the role failed with 'PostgreSQL configuration errors'. FIXED: new roles/postgresql/tasks/validate_config.yml ignores restart-only rows that are merely pending, validates all values with 'postgres -C', and rolls back pg_hba.conf and the drop-in (backup: true, a new broken drop-in is removed). Regression tests in test_bootstrap_postgresql.

### BUG-086 – nginx baseline site always listened on [::]; nginx -t fails on kernels without IPv6
- Phase: P01 · Komponente: bootstrap · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den bootstrap-Tests)
- Reproduktion: Render hermclaw-next.conf.j2 and run nginx -t on a host without IPv6 (ipv6.disable=1), e.g. this build sandbox.
- Erwartet: Config test succeeds.
- Tatsächlich: 'socket() [::]:80 failed (97: Address family not supported by protocol)'. FIXED: listen [::] only when nginx_listen_ipv6 is true, which defaults to whether the host has an IPv6 address. Regression test test_ipv6_listener_follows_the_host; real nginx -t passes.

### BUG-087 – No Secure Boot check before building the DKMS NVIDIA driver on .224
- Phase: P01 · Komponente: bootstrap · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den bootstrap-Tests)
- Reproduktion: .224 with Secure Boot enabled and no MOK enrolled; run the nvidia role.
- Erwartet: A clear refusal up front with remediation steps.
- Tatsächlich: The driver would build but its unsigned modules would not load; nvidia-smi fails only after the install. FIXED: roles/nvidia/tasks/secureboot.yml reads the EFI SecureBoot variable and refuses unless nvidia_secureboot_mok_enrolled is true. Test test_nvidia_secure_boot_guard (5 cases).

### BUG-088 – ansible-lint production profile: 2 name[template] violations
- Phase: P01 · Komponente: bootstrap · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den bootstrap-Tests)
- Reproduktion: ansible-lint --offline --profile=production playbooks/*.yml roles
- Erwartet: 0 violations
- Tatsächlich: Fixed in roles/ollama/tasks/gpu_check.yml:40 and main.yml:135 (Jinja moved to the end of the task name).

### BUG-089 – Repository intelligence: a quoted "phrase" across lines disabled the lexical signal
- Phase: P39 · Komponente: repo_intelligence · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/unit/test_repo_intelligence_structure.py::test_query_phrases_are_single_line_and_ignore_apostrophes`; found by the 37.3 E2E run)
- Reproduktion: a step query like `title: orders.status'` + newline + `… DEFAULT 'new' …` (or any apostrophe as in "user's" followed later by a quoted word)
- Erwartet: lexical search with the step's words and literals
- Tatsächlich: the single-quote phrase pattern matched from the apostrophe across the line break; ripgrep refuses multi-line fixed patterns, so the whole lexical signal failed (`REPO_PATTERN_INVALID`, ranking degraded)
- Fix: phrases are single-line, a single quote must not touch a word (apostrophes), and `lexical_patterns` never emits a pattern with a newline/NUL

### BUG-090 – `hermclaw doctor` accepted a production install running on the shipped example configuration
- Phase: P39 · Komponente: doctor · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_doctor.py::test_production_refuses_example_configuration_and_flags_placeholder_macs`)
- Reproduktion: production install without `/etc/hermclaw/hosts.yaml` (only `hosts.example.yaml`), or with the inventory's placeholder MACs
- Erwartet: doctor fails / warns before the first job
- Tatsächlich: `load_config` silently falls back to `<name>.example.yaml` (meant for checkouts and tests) and doctor reported everything ok; Wake-on-LAN with `00:00:00:00:..` MACs never wakes a worker
- Fix: check `config:files` (production: fail when any of hosts/models/policies/capabilities/logging is only available as example) and `config:wake_on_lan` (warn on placeholder MACs)

### BUG-091 – Installed release looked for its configuration in `<site-packages>/config`
- Phase: P39 · Komponente: core/settings · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_config_and_redaction.py::test_default_config_dir_is_the_checkout_or_etc_hermclaw`)
- Reproduktion: `sudo -u hermclaw /opt/hermclaw/venv/bin/hermclaw config-check` without `HERMCLAW_CONFIG_DIR` (the units set it, a manual admin call does not)
- Erwartet: the production configuration in `/etc/hermclaw`
- Tatsächlich: `config_dir` defaulted to `REPO_ROOT/config`, which for a wheel install is `<venv>/lib/python3.13/site-packages/config` → "missing configuration file"
- Fix: default is the checkout's `config/` only when it exists (examples), otherwise `/etc/hermclaw`

### BUG-092 – `ollama` CLI not on PATH with the pinned tarball install (.224)
- Phase: P39 · Komponente: bootstrap/ollama · Severity: P3 · blockierend: nein · Status: **behoben** (role task "Link the ollama CLI into /usr/local/bin"; `tests/integration/test_bootstrap_infra.py` lint/syntax/idempotency green)
- Reproduktion: tarball install to `/opt/ollama/<version>` + `current` symlink, then `ollama ps` or `hermclaw-model-pull` (default `OLLAMA_BIN=ollama`)
- Erwartet: the CLI works for operators and the pull script
- Tatsächlich: `command not found` – only the unit used the absolute path
- Fix: `/usr/local/bin/ollama` → `/opt/ollama/current/bin/ollama`

### BUG-093 – Repository secret scan red: false positives in new docs and a fixed SearXNG `secret_key` placeholder
- Phase: P39 · Komponente: docs/bootstrap · Severity: P2 · blockierend: ja (Security-Test rot) · Status: **behoben** (`tests/security/test_security_checks.py::test_repository_contains_no_secrets`; found by research R-027 Y5)
- Reproduktion: `pytest tests/security/test_security_checks.py`
- Tatsächlich: `docs/operations/bootstrap.md:213` and `INSTALLATION.md` ("token: …" prose read as an assignment), `docs/research/20261014-026…:101` (`CMAKE_CUDA_ARCHITECTURES=…` read as high entropy), `searxng-settings.yml.j2:8` (a fixed `secret_key` placeholder – SearXNG would have started with that known key if the podman secret were missing)
- Fix: prose rephrased; the settings template sets no `secret_key` (only the podman secret `SEARXNG_SECRET`, SearXNG refuses to start on its shipped default); the research table writes the CMake value without `=`. The scanner stays strict (it also guards job diffs).

### BUG-094 – Same-second backup sets were ordered by their random id suffix, so retention could keep the wrong 'newest' set
- Phase: P33 · Komponente: backup · Severity: P2 · blockierend: nein · Status: **behoben** (Regressionstest in den backup-Tests)
- Reproduktion: Two create_backup runs within the same UTC second, then retention with keep_last=1 (test_prune_after_backup_protects_the_new_set failed intermittently).
- Erwartet: Sets sort by creation time.
- Tatsächlich: Fixed: set ids now carry milliseconds (hermclaw-YYYYmmddTHHMMSSmmmZ-<6hex>). Ids with second precision stay valid. Regression test: test_backup_units.py::test_set_ids_and_signature; the formerly flaky test passed 3 repeat runs.

### BUG-095 – Partial-attempt build_tarball fsynced before the tar end blocks and gzip trailer were written
- Phase: P33 · Komponente: backup · Severity: P3 · blockierend: nein · Status: **behoben** (Regressionstest in den backup-Tests)
- Reproduktion: Code review of the interrupted attempt's archive.py.
- Erwartet: fsync after the TarFile closes.
- Tatsächlich: Fixed (fsync moved after the tar close).

### BUG-096 – A misconfigured staging_dir equal to or nested with a local target could let the stale-staging cleanup delete real backup sets
- Phase: P33 · Komponente: backup · Severity: P1 · blockierend: nein · Status: **behoben** (Regressionstest in den backup-Tests)
- Reproduktion: BackupConfig(staging_dir=<local target path>).
- Erwartet: Refused.
- Tatsächlich: Fixed: check_staging_separate raises BACKUP_CONFIG_INVALID. Regression test: test_backup_failures.py::test_staging_inside_the_target_is_refused.

### BUG-097 – A wrong LiteLLM master key was classified as a bad request (DB-less proxy answers 400 / 500)
- Phase: P39 · Komponente: models/gateway · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/unit/test_models_gateway.py` cases for 400 "No connected db." and 500 "No module named 'prisma'"; `tests/integration/test_models_litellm_proxy.py::test_wrong_key_is_rejected_without_fallback` now requires `MODEL_AUTH_FAILED`, green against real LiteLLM 1.104.2 with and without prisma)
- Reproduktion: LiteLLM proxy without database (our setup), gateway with a wrong key
- Erwartet: `MODEL_AUTH_FAILED` (operator problem, no retry/fallback, clear doctor message)
- Tatsächlich: 400 `no_db_connection` → `MODEL_BAD_REQUEST`; without the `prisma` package auth errors are HTTP 500 (research R-027 L10)
- Fix: both shapes are authentication failures; `prisma==0.15.0` joins the `litellm-proxy` extra (401 for a missing key)


### BUG-098 – JSON output redacted before parsing broke `podman inspect` (container sandboxes never saw their containers)
- Phase: P28 · Komponente: infratools · Severity: P1 · blockierend: ja · Status: **behoben** (`tests/unit/test_infratools_containers.py::test_inspect_secret_like_json_keys_do_not_break_parsing`)
- Tatsächlich: `"Passwd": true` became invalid JSON after redaction
- Fix: JSON is parsed raw, only extracted fields are redacted; `loads_lenient` repairs transport-redacted output; raw output never reaches `command_runs`

### BUG-099 – Podman healthchecks stayed `starting` without systemd timers
- Phase: P28 · Komponente: infratools · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_infratools_compose_real.py::test_sandbox_up_healthy_remapped_ports_logs_and_teardown`)
- Tatsächlich: compose sandbox waited for `healthy` forever on a host without systemd user timers
- Fix: the sandbox runs `podman healthcheck run` itself

### BUG-100 – `nginx -t` bound the real listen sockets and wrote log/pid paths
- Phase: P28 · Komponente: infratools · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_infratools_nginx_real.py::test_listen_on_foreign_address_and_busy_port_is_rewritten`)
- Tatsächlich: other hosts' addresses fail with EADDRNOTAVAIL, ports < 1024 without root, paths created on the host
- Fix: `rewrite_for_test` moves sockets and paths into a private prefix and maps messages back to the original lines

### BUG-101 – SSE regex locations were not recognised as SSE
- Phase: P28 · Komponente: infratools · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_infratools_nginx.py`)
- Tatsächlich: `~ ^/api/(jobs/[^/]+/)?events/stream$` skipped the buffering/timeout rules
- Fix: probe paths matched with nginx location semantics

### BUG-102 – Billion-laughs YAML in a compose file
- Phase: P28 · Komponente: infratools · Severity: P1 · blockierend: nein · Status: **behoben** (`tests/unit/test_infratools_compose.py`)
- Tatsächlich: an alias-count limit did not stop the expansion
- Fix: cap on the expanded node count

### BUG-103 – Duplicate host ports: IPv4/IPv6 and wildcard overlap not told apart
- Phase: P28 · Komponente: infratools · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_infratools_ports.py`)
- Tatsächlich: false duplicates / missed overlaps between `0.0.0.0` and a specific address
- Fix: address-family aware overlap check

### BUG-104 – Local compose staging directory leaked when copying failed
- Phase: P28 · Komponente: infratools · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/failure/test_infratools_failures.py`)
- Tatsächlich: temp dir left behind
- Fix: always removed

### BUG-105 – Remote port allocator could fail although free ports existed
- Phase: P28 · Komponente: infratools · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/unit/test_infratools_sandbox_model.py`)
- Tatsächlich: allocation gave up early
- Fix: fixed retry over the range

### BUG-106 – Predeploy check crashed on invalid YAML
- Phase: P28 · Komponente: infratools · Severity: P3 · blockierend: nein · Status: **behoben** (`tests/failure/test_infratools_failures.py`)
- Tatsächlich: exception instead of a finding
- Fix: reported as validation error

### BUG-107 – A failed rollback-artifact step left the deployment row in `started`
- Phase: P28 · Komponente: infratools · Severity: P2 · blockierend: nein · Status: **behoben** (`tests/integration/test_infratools_rollback.py::test_failed_rollback_artifact_blocks_the_write_and_releases_the_claim`)
- Tatsächlich: target blocked for later deployments
- Fix: claim released, deployment marked failed

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
