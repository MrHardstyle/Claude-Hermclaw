# BUILD PLAN

Verbindlicher Fortschrittsplan für Hermclaw Next (generiert aus Bauplan §42, Status manuell gepflegt).

Legende: `[x]` erledigt mit Evidence · `[~]` erledigt, Live-Verifikation auf Zielhost blockiert (BLOCKER-001) · `[ ]` offen

## P00 – Initiale Research- und Inventurphase

- [x] 0.1 neues `hermclaw-next` Repo erstellen. — Evidence: Repo MrHardstyle/Claude-Hermclaw als hermclaw-next (DECISIONS D-001)
- [x] 0.2 Build-Control-Dateien anlegen. — Evidence: BUILD_PLAN/STATUS/BUGS/DECISIONS/RESEARCH_INDEX/TEST_MATRIX/RISK_REGISTER/CHANGELOG
- [x] 0.3 alte Architektur read-only inventarisieren. — Evidence: docs/architecture/legacy-inventory.md
- [~] 0.4 Host `.223` inventarisieren. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.5 Host `.222` inventarisieren. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.6 Host `.224` inventarisieren. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.7 Host `.225` inventarisieren. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.8 GitLab `.226` inventarisieren. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.9 Terra `.60` inventarisieren. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.10 Ports erfassen. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.11 Services erfassen. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.12 Hardware erfassen. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.13 SSH-Zugänge testen. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.14 WOL-Fähigkeit erfassen. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.15 Ollama-Version erfassen. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.16 vorhandene Modelle erfassen. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [~] 0.17 LiteLLM-Version/Config erfassen. — Evidence: scripts/inventory/hermclaw-inventory.sh + infra/ansible/playbooks/inventory.yml; Ausführung blockiert (BLOCKER-001)
- [x] 0.18 offizielle Modell-/API-Dokumentation recherchieren. — Evidence: docs/research/20261008-007..013 (Modelle/Ollama/LiteLLM)
- [x] 0.19 Systemdiagramm erzeugen. — Evidence: docs/architecture/system-diagram.md
- [x] 0.20 Risiko-/Abhängigkeitsregister erzeugen. — Evidence: RISK_REGISTER.md R-001..R-016

## P01 – Clean System Bootstrap

- [ ] 1.1 Debian-Zielversion festhalten. — Evidence: –
- [ ] 1.2 `hermclaw` User. — Evidence: –
- [ ] 1.3 Verzeichnisse. — Evidence: –
- [ ] 1.4 SSH Keys. — Evidence: –
- [ ] 1.5 Python. — Evidence: –
- [ ] 1.6 venv/tooling. — Evidence: –
- [ ] 1.7 PostgreSQL. — Evidence: –
- [ ] 1.8 pgvector. — Evidence: –
- [ ] 1.9 Git. — Evidence: –
- [ ] 1.10 Podman. — Evidence: –
- [ ] 1.11 Node/Vite Buildtooling auf `.223`. — Evidence: –
- [ ] 1.12 Nginx auf `.223`. — Evidence: –
- [ ] 1.13 systemd units Skeleton. — Evidence: –
- [ ] 1.14 Ansible inventory/roles. — Evidence: –
- [ ] 1.15 reproducible bootstrap test. — Evidence: –

## P02 – Backend Skeleton

- [x] 2.1 Python package. — Evidence: hermclaw/ (pyproject, editable install)
- [x] 2.2 settings. — Evidence: hermclaw/core/settings.py
- [x] 2.3 config loader. — Evidence: hermclaw/core/config.py + config/*.example.yaml, tests/unit/test_config_and_redaction.py
- [x] 2.4 structured logging. — Evidence: hermclaw/core/logging.py (JSON + Redaction)
- [x] 2.5 error model. — Evidence: hermclaw/core/errors.py
- [x] 2.6 health endpoint. — Evidence: GET /api/health (tests/integration/test_api_skeleton.py)
- [x] 2.7 test framework. — Evidence: pytest + pytest-asyncio + hypothesis, tests/conftest.py (Wegwerf-DB pro Session)
- [x] 2.8 lint/type checking. — Evidence: ruff + mypy --strict sauber (Makefile lint/typecheck)
- [~] 2.9 CI base. — Evidence: .gitlab-ci.yml (lint/test/ui); Lauf auf GitLab .226 blockiert (BLOCKER-001)
- [x] 2.10 version endpoint. — Evidence: GET /api/version

## P03 – Persistence

- [x] 3.1 DB models. — Evidence: hermclaw/persistence/models.py
- [x] 3.2 Alembic. — Evidence: alembic.ini, migrations/env.py (async), 0001_initial_schema
- [x] 3.3 all core tables. — Evidence: 39 Tabellen inkl. aller §10-Tabellen (test_all_required_tables_exist)
- [x] 3.4 indexes/constraints. — Evidence: FK/Check/Unique/partial unique (test_constraints_enforced)
- [x] 3.5 repositories. — Evidence: repositories table (persistence/models.Repository) + RepositoryRegistry (hermclaw/gitops/registry.py: register/get/resolve/list, protected branches, URL validation); CLI repo-add/repo-list; tests/integration/test_gitops_engine.py::test_registry_register_get_list_and_protection, ::test_registry_rejects_unsafe_urls, tests/integration/test_cli.py
- [x] 3.6 transaction boundary. — Evidence: session_scope() commit/rollback
- [x] 3.7 migration tests. — Evidence: test_migration_matches_models (Autogenerate-Diff leer)
- [x] 3.8 rollback tests. — Evidence: test_migration_downgrade_and_upgrade_roundtrip
- [x] 3.9 restart persistence test. — Evidence: test_restart_persistence

## P04 – Event Store

- [x] 4.1 append-only event contract. — Evidence: hermclaw/contracts/events.py EventEnvelope + EventType
- [x] 4.2 event service. — Evidence: hermclaw/events/store.py append_event (redacted, NOTIFY)
- [x] 4.3 sequence ordering. — Evidence: BIGINT IDENTITY sequence (test_append_orders_and_redacts)
- [x] 4.4 correlation IDs. — Evidence: correlation_id (Default = job_id)
- [x] 4.5 SSE endpoint. — Evidence: hermclaw/api/events.py stream_job_events (fastapi.sse) + X-Accel-Buffering
- [x] 4.6 reconnect/last-event-id. — Evidence: stream_events Replay ab Last-Event-ID (test_sse_stream_replay_then_live_and_reconnect)
- [x] 4.7 event retention. — Evidence: purge_events (test_retention_keeps_active_jobs)
- [x] 4.8 UI test client. — Evidence: ui/src/lib/sse.ts (EventStreamClient: Last-Event-ID/last_event_id resume, heartbeat watchdog, reconnect); ui/tests/unit/sse.test.ts; Playwright ui/tests/e2e/live-events.spec.ts (live events, reconnect with Last-Event-ID replay, half-open detection) against real uvicorn + PostgreSQL

## P05 – State Machines

- [x] 5.1 Job states. — Evidence: hermclaw/runtime/state_machines.py
- [x] 5.2 Step states. — Evidence: hermclaw/runtime/state_machines.py
- [x] 5.3 transition tables. — Evidence: hermclaw/runtime/state_machines.py
- [x] 5.4 invalid transitions. — Evidence: InvalidTransition (test_invalid_transition_raises)
- [x] 5.5 property tests. — Evidence: Hypothesis random walks (tests/unit/test_state_machines.py)
- [x] 5.6 recovery mapping. — Evidence: recovery_step_state/recovery_job_state

## P06 – Git Engine

- [x] 6.1 repository registry. — Evidence: hermclaw/gitops/registry.py RepositoryRegistry.register/get/get_by_name/resolve/list_repositories/set_protected_branches/sync_protected_branches/protected_patterns (repo row + default branch + policies.git.protected_branches globs); tests/integration/test_gitops_engine.py::test_registry_register_get
- [x] 6.2 clone/fetch. — Evidence: engine.GitEngine.sync_mirror (aliases clone/fetch), _init_mirror (atomic tmp+rename bare mirror), fetch --prune; tests/integration/test_gitops_engine.py::test_mirror_clone_then_fetch_and_resolve_base_sha, ::test_mirror_fetch_prunes_deleted_branches, ::test_unreachable_remote_is_recorded_as_failed; t
- [x] 6.3 base SHA. — Evidence: engine.GitEngine.resolve_base_sha(repo, branch, fetch=True) raising BaseBranchNotFound; tests/integration/test_gitops_engine.py::test_mirror_clone_then_fetch_and_resolve_base_sha, ::test_create_workspace_unknown_job_and_base_branch
- [x] 6.4 isolated workspaces. — Evidence: engine.GitEngine.create_workspace/_create_workspace_locked (settings.workspaces_dir/<job_id>/<repo>, full clone --no-hardlinks from mirror, Workspace row base_sha/branch/head_sha/status, idempotent, archive of vanished dirs, path-root checks, integrity check of .git before any git run); tests/integr
- [x] 6.5 job branches. — Evidence: naming.job_branch_name('<branch_prefix><job.id.hex[:8]>-<slug>') + validate_branch_name; engine.job_branch, resume from own remote job branch; tests/unit/test_gitops_units.py::test_slug_and_job_branch_name, ::test_invalid_branch_names; tests/integration/test_gitops_engine.py::test_create_workspace_r
- [x] 6.6 status/diff. — Evidence: engine.status/diff/changed_files + ops.read_status/read_diff (name-status, numstat, unified diff with byte/file limits, untracked via intent-to-add in a scratch index, -diff attributes for always_forbidden), reader.WorkspaceGitReader (GitReader protocol); tests/integration/test_gitops_engine.py::tes
- [x] 6.7 safe staging. — Evidence: engine.stage_allowed + scope_guard.StagingGuard (on hermclaw.scope.guard.ScopeGuard: target_paths/allowed_new_paths/forbidden/always_forbidden/allowed_operations incl. delete, symlink escape, embedded repos, index re-check); tests/integration/test_gitops_engine.py::test_stage_allowed_only_stages_sco
- [x] 6.8 Runtime commit. — Evidence: engine.commit_verified/_check_verification (passed run of same job/step, unused, created after workspace creation/last base update, covers staged paths, serialized per run), git_operations + EventType.GIT_COMMIT_CREATED; tests/integration/test_gitops_engine.py::test_commit_verified_happy_path, ::tes
- [~] 6.9 Runtime push. — Evidence: engine.push_job_branch (job branch only, to registered URL, force-with-lease, foreign-commit guard _may_replace/_known_remote_shas, EventType.GIT_PUSHED), create_merge_request + gitlab.GitLabClient (PRIVATE-TOKEN from secret ref, duplicate detection, EventType.MERGE_REQUEST_CREATED); tests/integrati
- [x] 6.10 protected branch tests. — Evidence: engine.assert_pushable + registry.protected_match (fnmatch globs from repo row, default branch, policies.git.protected_branches; refused locally before network, also at workspace creation and commit; only branch_prefix branches); tests/integration/test_gitops_push.py::test_push_refuses_protected_bra
- [x] 6.11 stale base detection. — Evidence: engine.check_base/ensure_base_current/_check_base_locked (mirror fetch, tracking-ref refresh, commits_behind, rewritten history, StaleBaseError details, refused base.check op), push require_current_base; tests/failure/test_gitops_failures.py::test_stale_base_detected_with_details, ::test_stale_base_
- [x] 6.12 conflict handling. — Evidence: engine.update_to_base (rebase --onto / --no-ff merge, autostash incl. untracked, abort + reset + stash restore on conflict, MergeConflictError with files/phase, pushed->committed after rewrite), recover(); tests/failure/test_gitops_failures.py::test_update_to_base_without_conflict[rebase|merge], ::t

## P07 – Worker Protocol

- [x] 7.1 Worker API schema. — Evidence: Request contracts are in hermclaw/contracts/worker.py. Response and registry schemas are in hermclaw/workers/schemas.py (HeartbeatAck, WorkerInfo/Detail, DaemonHealth, WorkspaceInfo, CommandStatus, Models*/Gpu*/Selftest*, ErrorResponse) plus worker_api_schemas(). Tests: tests/integration/test_worker
- [x] 7.2 heartbeat. — Evidence: Sender: worker/common/heartbeat.py HeartbeatSender (signed POST, ack-driven interval, backoff, final offline heartbeat). Endpoint: hermclaw/workers/api.py POST /api/workers/heartbeat (header precheck before the body is read). Ingest: WorkerRegistry.ingest_heartbeat updates the workers row, worker_ca
- [x] 7.3 capability registry. — Evidence: registry.py: _sync_capabilities upserts reported capabilities and deletes vanished ones (worker.state reason=capabilities_changed); register_from_config stores the declared capabilities from capabilities.yaml; select_worker(capability, kind, include_busy, exclude) requires ready, fresh, compatible, 
- [x] 7.4 health. — Evidence: Orchestrator: one worker_health row per heartbeat, get_worker(health_limit), prune_health, GET /api/workers/{id}. Daemons: GET /health (unauthenticated, checks plus degraded status), worker/common/system.py (/proc cpu/ram/load/uptime, shutil disk), worker/common/gpu.py (nvidia-smi CSV). Tests: test_
- [x] 7.5 worker auth. — Evidence: hermclaw/workers/auth.py: per-worker tokens via cred:/file:/env: refs, rotation (current + previous), HMAC-SHA256 over hermclaw-worker-v1|METHOD|path?query|ts|nonce|sha256(body), 120 s skew, constant-time compares, replay cache that only stores a nonce after the signature verifies, bearer header onl
- [~] 7.6 `.222` daemon. — Evidence: worker/execution/app.py: create_app with workspace upload (replace/merge, atomic swap), info, manifest, archive, delete-paths, delete, POST /v1/commands (idempotent per request_id, concurrency slots, draining, redaction, workspace RW locks), command status, container recovery, selftest. worker/execu
- [~] 7.7 `.224` daemon. — Evidence: worker/model/app.py and ollama.py: GET /v1/models (/api/ps + /api/tags), POST /v1/models/load (empty prompt, options.num_ctx, keep_alive, exclusive/keep), POST /v1/models/unload (keep_alive 0, then polls /api/ps), GET /v1/gpu (nvidia-smi CSV), POST /v1/selftest (counters only, no generated text). Th
- [x] 7.8 offline detection. — Evidence: WorkerRegistry.sweep_offline: heartbeat older than offline_after_missed x interval or a wake timeout leads to state offline, worker.state and worker.offline (SKIP LOCKED). A graceful final offline heartbeat now also emits worker.offline (reason=worker_shutdown). OfflineMonitor runs a background loop
- [x] 7.9 version compatibility. — Evidence: In ingest_heartbeat, a protocol_version that differs from WORKER_PROTOCOL_VERSION, or a kind that differs from the registration, forces state error and writes an event with the incompatibility. select_worker filters on protocol_version. The ack carries compatible and expected_protocol_version, and t

## P08 – Model Gateway

- [~] 8.1 LiteLLM adapter. — Evidence: hermclaw/models/gateway.py LiteLLMGateway (chat/structured/embed/call_with_fallback, model_invocations rows + model.invocation.started/finished events, planner.fallback.used). Tested against a REAL LiteLLM 1.104.2 proxy plus a fake Ollama in tests/integration/test_models_litellm_proxy.py: test_chat_
- [x] 8.2 model profiles. — Evidence: hermclaw/models/profiles.py: ProfileRegistry, check_architecture/assert_architecture, sync_profiles (INSERT ON CONFLICT, disables profiles that are no longer configured), get_profile_row(_by_role), profile_from_row. hermclaw/models/litellm_config.py generator + CLI. Tests: test_models_profiles.py::t
- [~] 8.3 Fast Qwen profile. — Evidence: ARCHITECTURE fast=qwen3:8b with ctx >= 16K enforced in profiles.py. Tests: test_models_profiles.py::test_architecture_errors; test_models_litellm_proxy.py::test_chat_request_fields_reach_ollama (fast-router: think=false, num_ctx 16384). Live check: test_models_live.py::test_live_fast_router_returns_
- [~] 8.4 Gemma Planner profile. — Evidence: planner=gemma4:26b, planner_fallback=gemma4:12b with fallback_for pointing at the planner, enforced in profiles.py. Technical fallback in gateway.call_with_fallback. Tests: test_models_gateway.py::test_fallback_on_load_error, test_fallback_after_repeated_timeout_only, test_no_fallback_for_non_techni
- [~] 8.5 Coder profile. — Evidence: coder=qwen3-coder:30b, ctx >= 32K, output 4K-8K enforced in profiles.py. Tests: test_models_profiles.py::test_architecture_missing_role_and_warnings; test_models_litellm_proxy.py::test_proxy_config_defaults_apply_without_passthrough (coder num_ctx 32768). Live: test_live_residency_loads_coder_with_c
- [~] 8.6 Heavy profile. — Evidence: heavy=qwen3.8:27b, ctx 24K-32K enforced in profiles.py. Test: test_models_litellm_proxy.py::test_request_level_think_overrides_proxy_default (heavy-review num_ctx 24576). Live inference on .224 blocked (BLOCKER-001).
- [~] 8.7 embedding profile. — Evidence: embedding=embeddinggemma via ollama/<tag>, embedding_dimensions required. LiteLLMGateway.embed: batching, index ordering, dimension check, no fallback. Tests: test_models_gateway.py::test_embed_batches_and_orders, test_embed_dimension_and_count_checks, test_embed_rejects_oversized_input, test_embed_
- [~] 8.8 health checks. — Evidence: hermclaw/models/health.py ModelHealthChecker: /health/liveliness, /health/readiness, /v1/models, Ollama /api/version /api/tags /api/ps; per-profile availability; never sends inference. Tests: test_models_litellm_proxy.py::test_health_against_real_proxy; test_models_residency.py::test_health_report_w
- [x] 8.9 context validation. — Evidence: hermclaw/models/tokens.py: 3.2 chars/token (D-008) plus per-message overhead; validate_context raises ValidationFailed(CONTEXT_OVERFLOW) before any HTTP call. The repair echo is dropped when it would overflow. Tests: test_models_profiles.py::test_estimate_tokens_is_conservative, test_message_estimat
- [~] 8.10 load/unload adapter. — Evidence: hermclaw/models/residency.py: ModelHostClient protocol, OllamaHostClient (/api/ps, /api/generate keep_alive, /api/embed for embedding models), WorkerModelHostClient adapter, ModelResidency.ensure_loaded/unload/unload_group/status (exclusive groups, num_ctx check, per-host lock, model.load.started/fi
- [x] 8.11 metrics. — Evidence: hermclaw/models/health.py invocation_metrics (per alias: calls, outcome counts, fallback/repair calls, tokens, reasoning chars, avg/p50/p95/p99/max latency via percentile_cont, error_rate, invalid_rate) and error_breakdown. Test: test_models_gateway_db.py::test_invocation_metrics on real PostgreSQL.

## P09 – Resource Manager

- [x] 9.1 lease table. — Evidence: The resource_leases/resource_requests tables from the P03 migration are used as the only state store (manager.py). Tests: tests/integration/test_resources_concurrency.py::test_unique_index_rejects_second_exclusive_lease (partial unique index proven), tests/integration/test_resources_manager.py::test
- [x] 9.2 acquisition. — Evidence: ResourceManager.acquire/try_acquire/_attempt_tx: advisory xact locks on a sorted lock set, unique-index backstop, budgets, exclusive and shared leases. Tests: test_resources_manager.py::test_exclusive_lease_blocks_until_release, ::test_shared_and_exclusive_interplay, ::test_wait_timeout_cancels_requ
- [x] 9.3 release. — Evidence: release/release_many (idempotent; NotFoundError for unknown ids; RESOURCE_RELEASED with reason/released_by/held_seconds), hold() releases with completed/preempted/error/cancelled. Tests: test_resources_manager.py::test_acquire_release_roundtrip_emits_events, ::test_release_unknown_lease_raises_not_f
- [x] 9.4 heartbeat/expiry. — Evidence: heartbeat (DB clock, owner check, capped at the preemption deadline, LeaseLost), status/should_yield, sweep_expired, expiry inside every acquire, run_maintenance, LeaseKeeper. Tests: test_resources_manager.py::test_heartbeat_extends_expiry_and_checks_owner, ::test_expired_lease_is_swept_with_event_a
- [x] 9.5 priority. — Evidence: PRIORITIES/OwnerKind/priority_for (Bauplan §4), queue in resource_requests ordered by priority, then FIFO created_at (clock_timestamp), then id; budget_queued rule across budget members; stale waiter requests expire. Tests: test_resources_policy.py::test_priorities_match_bauplan_section_4; test_reso
- [x] 9.6 safe preemption. — Evidence: request_preemption / acquire(preempt=True) / _preempt_for (minimal budget set) / _mark_preempting (expires_at capped at now+grace, RESOURCE_PREEMPT_REQUESTED) / withdraw_preemption plus auto-withdraw on abandon / forced expiry with reason preemption_grace_timeout; LeaseKeeper on_preempt callback. Te
- [x] 9.7 large-model exclusivity. — Evidence: acquire_model/hold_model (profile.resource_group/exclusive/memory_gb/priority), model_host_budgets(models) with capacity models.model_host_capacity_gb, validate_model_resources, worst_case_resident_gb. Tests: test_resources_preemption.py::test_large_model_exclusivity_and_model_budget, ::test_model_p
- [x] 9.8 video priority. — Evidence: acquire_gpu_for_media/hold_media/release_media: gpu-224 (+video-224) leases at 100/90, non-preemptible, taken in sorted order; concurrent exclusive drain leases on large-model-224/small-model-224 with preempt=True; partial rollback. Tests: test_resources_preemption.py::test_video_preempts_ai_model_l
- [x] 9.9 crash recovery. — Evidence: recover(holder_id): releases leases of earlier incarnations (reason holder_restarted), cancels leftover requests except live ones, sweeps stale leases; leases persist in the DB; a cancellation after commit cannot leak a lease; purge_finished_requests. Tests: test_resources_recovery.py::test_leases_s

## P10 – Wake-on-LAN

- [~] 10.1 config. — Evidence: hermclaw/wol/config.py: validate_host_wake_config / validate_wake_config / assert_wake_config check WOL semantics (unicast MAC, IPv4 broadcast, port, timeouts > 0, max_attempts >= 1), probe targets and idle_sleep_command. readiness._context raises ConfigError/WorkerNotFound before any stage runs. Te
- [~] 10.2 WOL send. — Evidence: hermclaw/wol/magic.py build_magic_packet/normalize_mac/send_magic_packet (asyncio datagram endpoint, allow_broadcast=True, raises WolError WOL_SEND_FAILED); readiness._wake/_send_once (retries bounded by max_attempts and the overall deadline, emits WORKER_WAKE_SENT). Tests: test_wol_magic.py::test_p
- [~] 10.3 ping wait. — Evidence: probes.Probes.ping (ping -n -c 1 -W, validated address, TCP fallback to SSH port where a refused connection counts as host alive); readiness._wait_ping with bounded re-sends inside ping_timeout_seconds. Tests: test_wol_probes.py::test_ping_uses_icmp_binary_with_safe_arguments, ::test_ping_without_bi
- [~] 10.4 SSH wait. — Evidence: probes.Probes.ssh_banner (RFC 4253 identification line, also after earlier lines); readiness._wait_ssh. Tests: test_wol_probes.py::test_ssh_banner_detected_even_after_pre_banner_lines, ::test_ssh_banner_wrong_protocol_and_silent_server; test_wol_failures.py::test_ssh_timeout; test_wol_readiness.py::
- [~] 10.5 worker API wait. — Evidence: readiness._wait_api: GET <worker_api>/health must return 2xx with status ok/degraded; a mismatching worker_id fails at once; redirects are not followed. Tests: test_wol_failures.py::test_worker_api_unhealthy, ::test_worker_api_answers_as_another_worker_fails_fast, ::test_worker_api_reporting_unknown
- [~] 10.6 Ollama wait. — Evidence: readiness._wait_services: every configured host service (e.g. ollama /api/version) is probed over HTTP or TCP within service_timeout_seconds and yields MODEL_SERVICE_TIMEOUT on failure; readiness._wait_capabilities checks registry freshness, compatibility, draining state and required capabilities, y
- [x] 10.7 failure states. — Evidence: errors.WakeFailureCode has exactly the six codes; STAGE_FAILURE maps each stage to its code; WakeController._fail writes a wake_events row (error_code), WORKER_WAKE_STAGE(failed) and WORKER_WAKE_FAILED, and sets the worker to error only when its state is not live; ReadyResult.raise_for_failure raise
- [x] 10.8 UI events. — Evidence: status.WorkerUiStatus/ui_status_for (STARTING/WAKING/READY/BUSY/ERROR/SLEEPING); every stage emits WORKER_WAKE_STAGE with ui_status plus WORKER_WAKE_SENT, WORKER_READY and WORKER_WAKE_FAILED; job STATUS lines; WakeController.ui_statuses()/worker_ui_status(). Tests: test_wol_readiness.py::test_full_w
- [~] 10.9 idle sleep hooks. — Evidence: idle.IdleSleepPolicy (evaluate/maybe_sleep/run_once/run_forever), RemoteCommandRunner Protocol run(host_id, command, timeout) -> (rc, out, err). Never sleeps a worker with active steps, attempts, leases or waiting requests, or one that reports active work. Uses a row lock plus a re-check (_recheck) 

## P11 – Repository Intelligence

- [x] 11.1 inventory. — Evidence: inventory.py build_inventory -> RepoInventory (persisted in repo_index_runs.inventory). Covers files (git ls-files tracked+untracked, .gitignore honoured), sizes, Docker, CI, migrations, config files, entry points, routes, README (redacted), git branch/head/status, and redacts credentials in manifes
- [x] 11.2 language detection. — Evidence: languages.py (extension, file name, shebang, binary sniffing, categories). Tests: tests/unit/test_repo_intelligence_inventory.py::test_language_detection and the shebang/binary assertions in ::test_inventory_of_a_multi_language_repository
- [x] 11.3 build/test discovery. — Evidence: inventory.py _manifests/_tests. Detects build systems (pyproject/setup.py/package.json/composer/Makefile/CMake/go.mod/Cargo/Maven/Gradle/...), package managers with lockfiles, test dirs, frameworks (pytest/unittest/jest/vitest/mocha/phpunit/pest/go-test/cargo/junit/rspec) and test scripts. Tests: te
- [x] 11.4 lexical search. — Evidence: lexical.py LexicalSearcher: rg --json with fixed/regex, globs, word, smart case, per-file and total limits, timeout, deterministic --sort path, symlink-safe explicit paths. Fallback is pure Python; regex runs in an isolated child interpreter (python -I) with a deadline. Also does filename search. Te
- [x] 11.5 symbol index. — Evidence: symbols.py persists code_symbols (repository_key, git_sha): functions, classes, methods, interfaces, traits, enums, constants, routes, tables/views (tables.py: CREATE TABLE/VIEW plus migration DSLs), imports, and module-level call rows; queries query_symbols, referencing_files, load_imports. Tests: 
- [x] 11.6 AST adapters. — Evidence: AST adapters: Python ast, plus tree-sitter (tree_sitter_language_pack.get_parser) for javascript/typescript/tsx/php, collecting definitions, call references and imports (incl. require/dynamic/export-from/PHP use and include). Recursion and syntax errors degrade to partial structure. Tests: tests/uni
- [x] 11.7 dependency relations. — Evidence: dependencies.py ModuleResolver: Python packages and relative imports, JS/TS relative specifiers with extension/index probing, PHP include literals, and PSR-4 from every workspace composer.json. Also provides DependencyGraph. Imports of untouched files are re-resolved during incremental runs. Tests: 
- [~] 11.8 EmbeddingGemma index. — Evidence: chunking.py: symbol-aligned chunks of at most ~1500 tokens, member split, overlapping windows, oversized module-level and doc segments windowed too, redacted embedding input. embeddings.py does batched embedding through the EmbeddingModel protocol, validates count/dim/NaN/zero vectors, and reuses ve
- [x] 11.9 pgvector store. — Evidence: embeddings.py stores code_chunks vector(768) with embedding_model via pgvector.sqlalchemy. semantic_search uses cosine_distance (<=>) ordered by distance, exact scan for small indexes, HNSW with hnsw.ef_search otherwise; models are never mixed. Tests against real PG16 + pgvector: tests/integration/t
- [x] 11.10 fusion ranking. — Evidence: ranking.py does Reciprocal Rank Fusion over lexical, symbol, structural (paths, routes, tables, plus query-matching definitions used by other files via JSONB @>), semantic (similarity floors), test_reference (tests that import, mention or are named after a file, scaled by preliminary relevance) and 
- [x] 11.11 targeted read. — Evidence: reader.py FileReader: exact line ranges, character budget cut at line boundaries, numbered view, binary and size refusal, no ../absolute/.git/secret paths, symlink escape refused. Service read and context_for give fair-share budget snippets, redacted. Tests: tests/unit/test_repo_intelligence_reader.
- [x] 11.12 incremental reindex by Git SHA. — Evidence: indexer.py RepoIndexer: noop/incremental/full by Git SHA using git diff --name-status --no-renames old..new. Only changed files are re-parsed, re-chunked and re-embedded; deleted files are removed; imports of untouched files are re-resolved. Falls back to full when the old SHA is unknown, INDEX_VERS

## P12 – Research Engine

- [x] 12.1 query planner. — Evidence: hermclaw/research/planner.py (QueryPlanner.plan: role fast, QueryPlan schema, capped at policies.research.max_queries; sanitize_queries; fallback_queries). Tests: tests/unit/test_research_planner_synth.py::test_planner_uses_fast_model_and_caps_queries, ::test_planner_falls_back_on_model_failure_or_u
- [~] 12.2 web search interface. — Evidence: hermclaw/research/search.py (SearchProvider protocol, SearxngSearchProvider GET /search?format=json, 403/HTML -> SEARCH_JSON_DISABLED, StaticSearchProvider, DisabledSearchProvider, canonical_url/dedupe/merge). Tests: tests/integration/test_research_fetch_search.py::test_searxng_json_parsing_against_
- [x] 12.3 HTTP/browser fetch. — Evidence: hermclaw/research/fetch.py HttpFetcher (SSRF guard for every hop, DNS pinning with Host/SNI, redirect limit, content-type allowlist, max bytes, timeouts, policy UA) + hermclaw/research/browser.py (RenderingFetcher/BrowserRenderer: headless Chromium --dump-dom behind GuardProxy that applies check_url
- [x] 12.4 source records. — Evidence: hermclaw/research/store.py (add_source, SourceRow, load_contract) + engine._read_one/_persist_source: read/failed/skipped rows in research_sources with type, scores, content_hash, excerpt, published_at. Tests: tests/integration/test_research_engine.py::test_full_pipeline_persists_scored_sources_link
- [x] 12.5 content extraction. — Evidence: hermclaw/research/extract.py (BeautifulSoup+lxml, drops nav/script/style/footer/aside/hidden/roles, prefers main/article, title, language, sha256 content hash of text; text/json docs). Tests: tests/unit/test_research_text_extract.py::test_html_extraction_drops_boilerplate_and_prefers_main, ::test_co
- [x] 12.6 claim extraction. — Evidence: hermclaw/research/claims.py ClaimExtractor (fast model, ClaimExtraction, fenced untrusted source, grounding check, heuristic sentence fallback). Tests: tests/unit/test_research_claims.py::test_model_claims_are_grounded_and_deduplicated, ::test_model_failure_and_ungrounded_output_fall_back_to_heurist
- [x] 12.7 source-to-claim links. — Evidence: claims.merge_claims (cross-source merge, noisy-OR confidence from authority*relevance*freshness) + store.add_claim (research_claim_sources links). Tests: tests/unit/test_research_claims.py::test_merge_claims_links_sources_and_combines_confidence; tests/integration/test_research_engine.py::test_same_
- [x] 12.8 freshness. — Evidence: extract.py dates from JSON-LD/meta/<time>, fetch Last-Modified, SearXNG publishedDate; sources.assess_freshness (fresh/aging/stale/unknown factors) used in confidence and contradiction tie-break. Tests: tests/unit/test_research_text_extract.py::test_meta_and_time_dates_and_multiple_articles, ::test_
- [x] 12.9 authority/relevance. — Evidence: sources.classify_source (primary_domains incl. path prefix and www., standards, docs.*/readthedocs/docs paths, github.com/<org>/<repo>, registries, vendor, secondary, forum), relevance_score (BM25-like), prior_score (primary first). Tests: tests/unit/test_research_sources.py::test_classify_source, :
- [x] 12.10 contradiction detection. — Evidence: hermclaw/research/contradictions.py (value/negation/antonym conflicts, same-source exclusion, union-find groups, preferred claim, optional LlmContradictionConfirmer hook). Tests: tests/unit/test_research_claims.py::test_contradictions_value_negation_antonym_and_same_source, ::test_grouping_is_transi
- [~] 12.11 Qwen synth. — Evidence: synth.py Synthesizer fast mode (role fast) with citation validation, repair, strip, deterministic fallback; persisted research_runs.synthesis; decision linkage. Tests: tests/unit/test_research_planner_synth.py::test_fast_synthesis_valid_first_try, ::test_uncited_text_after_repair_is_stripped, ::test
- [~] 12.12 Gemma deep synth. — Evidence: synth.choose_mode -> deep (role planner = Gemma) for deep flag/contradictions/>=6 sources/>=16 claims, fallback chain deep->fast->deterministic. Tests: tests/unit/test_research_planner_synth.py::test_deep_synthesis_for_contradictions_uses_planner_alias_and_repairs, ::test_deep_failure_falls_back_to_
- [x] 12.13 UI source stream. — Evidence: engine.py events research.started / research.query.started ('Research sucht: ...') / research.source.read ('Research liest: ...', failed+skipped too) / research.claim.created ('Quelle verwendet für: ...') / research.decision.linked / research.finished (sources_used with used_for_claims, contradictio

## P13 – Contracts

- [x] 13.1 JobContract. — Evidence: hermclaw/contracts/*.py
- [x] 13.2 PlanContract. — Evidence: hermclaw/contracts/*.py
- [x] 13.3 StepContract. — Evidence: hermclaw/contracts/*.py
- [x] 13.4 ScopeContract. — Evidence: hermclaw/contracts/*.py
- [x] 13.5 WorkerInput. — Evidence: hermclaw/contracts/*.py
- [x] 13.6 WorkerResult. — Evidence: hermclaw/contracts/*.py
- [x] 13.7 ToolCall. — Evidence: hermclaw/contracts/*.py
- [x] 13.8 VerificationContract. — Evidence: hermclaw/contracts/*.py
- [x] 13.9 ReviewContract. — Evidence: hermclaw/contracts/*.py
- [x] 13.10 ResearchContract. — Evidence: hermclaw/contracts/*.py
- [x] 13.11 ArtifactContract. — Evidence: hermclaw/contracts/*.py
- [x] 13.12 JSON schema export. — Evidence: python -m hermclaw.contracts.schema_export → docs/contracts/schemas/*.schema.json

## P14 – Gemma Planner

- [x] 14.1 planner prompt contract. — Evidence: hermclaw/planner/prompt.py: planner_system_prompt/_base_rules cover planner role, JSON only, no code, listed kinds and capabilities, acceptance with evidence types, grounded repo_hints and the new network rule; planner_user_payload has the Bauplan §15 keys with budgets, deterministic line-safe trunc
- [~] 14.2 structured output. — Evidence: hermclaw/planner/loop.py run_structured_loop calls ChatModel.chat(alias of role 'planner', json_schema=PlanContract schema, profile max_tokens/temperature/timeout). Tests: tests/unit/test_planner_loop.py::test_valid_first_answer_uses_schema_constrained_call, tests/integration/test_planner_repos.py::
- [x] 14.3 schema validation. — Evidence: hermclaw/planner/parsing.py extract_json_object/validate_schema/format_validation_errors produce located errors. Tests: tests/unit/test_planner_validation.py::test_extract_json_object_variants, ::test_validate_schema_reports_located_errors, ::test_validate_schema_dag_errors; tests/integration/test_p
- [x] 14.4 plan repair. — Evidence: loop.py: at most 2 repairs, exact redacted error list, only a parsed JSON object is echoed (redacted, canonical), truncation hint, then PlannerError PLANNER_INVALID_OUTPUT plus planner.failed. Tests: tests/unit/test_planner_loop.py::test_prose_or_reasoning_inside_a_broken_answer_is_never_echoed, ::t
- [x] 14.5 DAG validation. — Evidence: PlanContract rejects cycles, unknown deps and duplicate ids. validation.py semantic_errors adds max steps (including open research requests), duplicate work and unreachable info steps. Tests: tests/unit/test_planner_validation.py::test_validate_schema_dag_errors, ::test_duplicate_work_and_unreachabl
- [x] 14.6 dependency validation. — Evidence: validation.py checks: checking steps need dependencies, research must not depend on mutating steps, capability and step_kind_capability consistency, grounding (now from all inventory sections), catch-all rejection, network only via capability, diff glob hygiene. Tests: tests/unit/test_planner_valida
- [x] 14.7 risk assignment. — Evidence: enrich.py assign_risk/RiskPolicy: deploy/ssh/database are at least medium, never lowered, implement steps without tests touching at least 6 paths become high (glob/directory hints now count the known paths they match), overrides only stricter, unknown kinds rejected. Tests: tests/unit/test_planner_e
- [x] 14.8 acceptance generation. — Evidence: enrich.py generate_acceptance adds Diff (path-like hints, allow_empty false) and Test (detected command) when no substantive criterion exists, and always adds Scope and Security. Tests: tests/unit/test_planner_enrich.py::test_mutating_step_without_acceptance_gets_diff_test_scope_security, ::test_sub
- [x] 14.9 research request generation. — Evidence: enrich.py add_research_steps: uncovered research_needed entries become research steps that the first work steps depend on; no duplicates; preserved and reserved ids respected. Tests: tests/unit/test_planner_enrich.py::test_research_requests_become_research_steps_before_all_work, ::test_research_step
- [x] 14.10 planner tests across unrelated repos. — Evidence: Five unrelated fixtures (FastAPI, PHP, React/TS, YAML config, Linux admin) in tests/unit/test_planner_support.py. Tests: tests/integration/test_planner_repos.py::test_plan_created_for_unrelated_repositories[5 params] (DB rows plans/plan_versions/steps/deps and events), plus the repair, failure, fall

## P15 – Scope Engine

- [x] 15.1 planner hints intake. — Evidence: engine.py _StepSnapshot.of / ScopeEngine.create_scope / _generate (repo_hints, allowed_new_paths, forbidden_paths, acceptance, constraints) + canonical_path / strip_location_suffix / classify_hint; tests: test_scope_engine.py::test_paths_globs_directories_and_symbols_resolve, test_scope_failures.py:
- [x] 15.2 repo intelligence evidence. — Evidence: engine.py list_workspace_files (git ls-files --cached --others --exclude-standard + existence, symlink and symlinked-parent containment, core.fsmonitor=false), _resolve_hint, _resolve_by_repo (find_symbol/search with per-call timeout), select_confident_hits (score thresholds, relative floor, ambigui
- [x] 15.3 policy merge. — Evidence: engine.py merged_forbidden (always_forbidden + step forbidden, canonical, invalid recorded), _apply_policy (forbidden, unbounded creation globs, implausible paths excluded with evidence), caps -> unavailable never truncated; tests: test_scope_engine_units.py::test_merged_forbidden_normalises_dedupes
- [x] 15.4 scope generation. — Evidence: engine.py _generate: ScopeContract(source=planner_and_repo_intelligence, strict_target_paths=True, glob-escaped existing targets, allowed_new_paths incl. hinted missing paths for writing kinds, operations from new paths/targets/delete designation via AbsenceEvidence or non-negated delete constraints
- [x] 15.5 forbidden paths. — Evidence: engine.py merged forbidden_paths in contract + exclusion evidence; expansion.py assess_paths rejects forbidden; ScopeGuard enforces at audit; tests: test_scope_engine.py::test_forbidden_paths_are_excluded_with_evidence, test_scope_expansion.py::test_invalid_or_forbidden_paths_are_rejected / test_ste
- [x] 15.6 scope versioning. — Evidence: engine.py persist_scope_version (step row FOR UPDATE, version max+1, every non-superseded prior version -> superseded with evidence.superseded provenance, steps.current_scope_version), closed/superseded re-check under lock; tests: test_scope_engine.py::test_versioning_supersedes_previous_active, tes
- [x] 15.7 expansion request. — Evidence: expansion.py ScopeExpansionHandler.handle (validation, mechanical = test-of/test-imports/imported-by/same-dir+language via RepoContextProvider.read, semantic -> needs_replan, deletes always semantic, max 3 expansions, caps, closed-step rejection, literal bracket file names escaped, delete designatio
- [x] 15.8 unavailable handling. — Evidence: engine.py _generate unavailable branch (no_resolvable_scope / too_many_target_paths / too_many_new_paths): persisted unavailable row with deny-all contract + full evidence, SCOPE_UNAVAILABLE, decision.contract=None/runnable=False, guard_for deny-all, workspace untouched; tests: test_scope_engine.py:
- [x] 15.9 scope audit. — Evidence: audit.py ScopeAuditor.audit_changes/audit_status (ScopeGuard on active version, deny-all without scope, designated-delete restriction, unauditable entries fail closed, evidence.audits bounded, SCOPE_VIOLATION), derive_changes_from_status (orig_path / arrow form), unresolved_renames; tests: test_scop

## P16 – Context Builder

- [x] 16.1 context sections. — Evidence: sections.py SectionName (13 sections, Bauplan §18 order), SYSTEM/USER layout, DEFAULT_SYSTEM_CONTRACT, RESPONSE_PROTOCOL (single JSON action {tool,args,status,decision}), MANDATORY_SECTIONS; render.py renderers (goal, scope, constraints, acceptance for all 9 evidence types, repo facts, failure+corre
- [x] 16.2 token budgeting. — Evidence: tokens.py estimate_tokens (3.2 chars/token exact integer arithmetic, non-ASCII costed >=1 token, D-008); budget.py plan_budget (total = context - max_output - max(min margin, fraction)), framing reservation, SectionBudgets fixed shares (sum 1.00, validated), split_elastic redistribution to RELEVANT 
- [x] 16.3 relevance. — Evidence: builder.py relevance: context_for(title+goal+first error line), scope target heads (literal_path), failure file:line regions (traceback/path:line/pytest node ids, workspace-absolute paths relativised), ranking (failure 3.0 > target/acceptance 2.0 > search 1.0+norm > context norm, +1 inside targets),
- [x] 16.4 deduplication. — Evidence: snippets.py dedupe_snippets (identical keys folded, overlapping/adjacent ranges merged line-accurately with re-read of gaps, max_snippet_lines cap, contained non-exact dropped, duplicate content never twice), pack_snippets first-fit + head clipping with read_range hint, render_packed grouping. Tests
- [x] 16.5 error preservation. — Evidence: failure.py preserve_failure (verbatim if it fits; else first error line block + final summary lines always kept, head/tail alternating, exact '[… n chars omitted …]' markers, single huge line clipped, never above budget), first_error_line, truncate_middle; render.py render_failure (fenced verbatim f
- [x] 16.6 tool summary. — Evidence: history.py TurnRecord (no reasoning field, from_mapping ignores status/decision/reasoning), render_history (last N full digest lines, older turns as counts per tool + error codes + last failure + files changed, budget shrinking), strip_reasoning (<think>/<thinking>/<reasoning>/analysis channel). Tes
- [x] 16.7 current diff. — Evidence: diff.py split_diff/render_diff (per-file water-filling truncation with git_diff hint markers, listing of files that do not fit, exclusion of always_forbidden paths incl. renames); builder.py GitReader.diff(max_bytes=diff_max_bytes), redaction, fencing. Tests: test_context_builder_failure.py::test_di
- [x] 16.8 tests. — Evidence: builder.py RELEVANT TESTS: test files from acceptance test/command evidence (shlex parsing, ::node ids), failure references in test files, search(target stem / code-like goal identifiers) filtered by is_test_path, context_for test hits; separate budget with redistribution. Tests: test_context_builde
- [x] 16.9 context telemetry. — Evidence: report.py ContextReport (budget figures, per-section budget/estimate/chars/truncated/items/omitted_reason, dropped items with reasons, merged count, warnings, SHA-256 fingerprint), to_event_payload() (bounded, redacted, keys free of 'token' so the event-store redactor does not erase counters), recor

## P17 – Tool Engine

- [x] 17.1 list/read/find/search. — Evidence: engine.py _list_files/_read_file/_read_range/_find_text/_search_repo/_search_symbol + workspace.py WorkspaceFS. Tests in test_tools_engine_read_write.py: test_list_files_hides_secrets_and_git_and_honours_filters, test_read_file_and_range, test_read_output_budget_sets_truncated, test_path_traversal_a
- [x] 17.2 git read. — Evidence: engine.py _git_status/_git_diff go through the GitReader protocol; diff sections of protected files are left out; there are no git mutation tools. Tests: test_git_status_and_diff_are_read_only; test_broken_git_reader (failure)
- [x] 17.3 file writes. — Evidence: engine.py _write_file/_replace_text: ScopeGuard check with the operation judged against base_sha, atomic temp+fsync+rename, file mode kept, symlinked directories refused, redaction marker refused. Tests: test_write_file_create_modify_scope_and_events, test_write_tools_require_a_scope_contract, test_
- [x] 17.4 patch. — Evidence: patch.py parse_patch/targets (renames, deletes, -p0/-p1, symlinks refused) plus engine._apply_patch: every target checked against scope -> numstat cross-check -> git apply --check -> git apply (worktree only) inside a tracker audit. Tests: test_apply_patch_applies_in_scope_without_committing, test_a
- [x] 17.5 commands. — Evidence: classify.py (policy patterns, checks against several rewritten forms of the command so simple obfuscation fails, built-in git-mutation detection), engine._sandbox_run (cwd validation, timeout and network from ToolPermissions, command_runs rows, COMMAND_RUN), snapshot.WorkspaceTracker (reverts out-of
- [x] 17.6 tests. — Evidence: testparse.py parsers for pytest/unittest/jest/vitest/mocha/phpunit/go/cargo/generic plus summarise(); engine._run_test writes test_runs rows and TEST_STARTED/TEST_PASSED/TEST_FAILED. Tests: test_tools_testparse.py (16 recorded outputs + verdict tests), test_run_test_real_pytest_pass_and_fail (real p
- [x] 17.7 research request. — Evidence: engine._request_research -> ToolCallbacks.on_research, per-attempt limit, output clipped and redacted. Tests: test_request_research_forwards_and_limits, test_callback_failures_are_contained
- [x] 17.8 scope request. — Evidence: engine._request_scope_expansion: protected paths refused locally, forwarded to on_scope_expansion, a newer granted ScopeContract replaces the guard (an older one is ignored). Tests: test_request_scope_expansion_swaps_guard, test_callback_failures_are_contained, test_null_callbacks_refuse
- [x] 17.9 replan request. — Evidence: engine._request_replan -> on_replan, terminal; later calls get STEP_FINISHED. Tests: test_request_replan_is_terminal, test_callback_failures_are_contained (failed replan is not terminal)
- [x] 17.10 checkpoint. — Evidence: engine._checkpoint + recorder.save_checkpoint (row lock, redacted, step status untouched) + CHECKPOINT_CREATED. Tests: test_checkpoint_persists_on_step, test_checkpoint_for_unknown_step
- [x] 17.11 complete_step. — Evidence: engine._complete_step validates CompletionReport (unknown keys refused), normalises paths, puts reported vs. actual changed files in data, terminal. Tests: test_complete_step_validates_and_is_terminal, test_broken_git_reader
- [x] 17.12 block_step. — Evidence: engine._block_step validates BlockReport, terminal. Test: test_block_step_is_terminal
- [x] 17.13 policy enforcement. — Evidence: engine.execute: pre-checks (finished, allowed tools, turn budget, ARGS_INVALID with schema), tool_calls row (redacted, long values stored as head + sha256) with status succeeded/refused/failed/cancelled, TOOL_CALL_STARTED/FINISHED with duration, output budget with truncated flag, terminal flag, per-

## P18 – Execution Sandbox

- [~] 18.1 rootless Podman. — Evidence: worker/execution/sandbox.py PodmanSandbox (--userns=keep-id, require_rootless euid check, preflight() via podman info rootless flag; make_sandbox enforces rootless in production). Tests: test_podman_argv_has_every_isolation_flag, test_require_rootless, test_make_sandbox_engines, test_podman_info_ass
- [x] 18.2 images. — Evidence: resolve_image/allowed_images (policy.image, policy.images aliases, extra_images allowlist, strict reference regex), ContainerSandbox.ensure_images (inspect, pull, pull policy). Tests: test_image_allowlist_and_aliases, test_rejected_request_returns_error_result_without_running (unit); test_ensure_ima
- [x] 18.3 mounts. — Evidence: Exactly one bind mount (/workspace, :Z), --read-only, size-limited /tmp tmpfs and /dev/shm tmpfs, --read-only-tmpfs=false so /run and /var/tmp are not writable; workspace path validation. Tests: test_workspace_rw_rootfs_ro_tmp_rw, test_only_workspace_tmp_and_shm_writable (checks df sizes too), test_
- [x] 18.4 resource limits. — Evidence: --cpus/--memory/--memory-swap(=memory)/--pids-limit; requests may lower but not raise them (resolve_limits, minimums 0.01 CPUs / 6m). Tests: test_limits_may_be_lowered_not_raised, test_cpus_never_formatted_with_exponent (unit); test_pids_limit_enforced and test_memory_limit_enforced enforced for rea
- [x] 18.5 timeouts. — Evidence: _supervise + wait_exit: on timeout, podman kill -s KILL, client SIGKILL after a grace period, then rm -f; timed_out=True, exit_code=None; shielded cleanup on cancellation. LocalSandbox kills its whole process group. Tests: test_timeout_kills_and_removes_container, test_cancellation_kills_and_removes
- [x] 18.6 network-off default. — Evidence: --network=none unless req.network or policy network_default=allowed. Tests: test_network_modes (unit); test_network_off_by_default (real container: no routes, only lo, wget fails).
- [x] 18.7 allowed network mode. — Evidence: With the network capability: pasta (rootless and installed) or slirp4netns for podman, bridge for docker; allowed_network override. Tests: test_network_modes, test_pasta_auto_only_rootless (unit); test_network_allowed_mode_has_route (real slirp4netns: default route and one interface). The pasta path
- [x] 18.8 cleanup. — Evidence: --rm; kill+rm on timeout, cancellation and engine-created-but-not-started containers; stale same-request leftover replaced, foreign containers never touched; LocalSandbox kills background children after exit; recovery.prune_stale_workspaces, remove_containers. Tests: test_stale_leftover_with_same_na
- [x] 18.9 abandoned container recovery. — Evidence: recovery.recover_abandoned/recover_for_sandbox/list_managed_containers/run_periodic_recovery: find containers by managed label, keep tracked (evaluated after listing) and young ones, dry_run, report {scanned, removed, kept, errors}. Tests: test_crashed_worker_leftover_is_recovered (real: podman clie

## P19 – Main Coder

- [x] 19.1 worker loop. — Evidence: hermclaw/coder/loop.py CoderLoop.run; tests/integration/test_coder_loop.py::test_full_turn_sequence_completes_step
- [~] 19.2 Qwen3-Coder 30B. — Evidence: CoderSettings.alias=coder-main (LiteLLM -> qwen3-coder:30b); scripted model in tests, live run blocked by BLOCKER-001
- [~] 19.3 32K context. — Evidence: ContextBuilderConfig.from_config(role=coder) uses the coder profile window (32K); exact budget tests in tests/unit/test_context_builder_*; live window BLOCKER-001
- [x] 19.4 tool protocol. — Evidence: one CoderAction per turn via ChatModel.structured + ToolEngine.execute; test_out_of_scope_write_is_refused_and_reported_to_the_model, test_invalid_model_output_twice_fails_and_timeout_is_model_failure
- [x] 19.5 completion. — Evidence: terminal complete_step/block_step/request_replan; test_full_turn_sequence_completes_step, test_request_replan_and_correction_items_reach_prompt
- [x] 19.6 checkpoints. — Evidence: CoderResult.checkpoint/history_from_checkpoint + per-turn StepAttempt.history; test_cancel_and_checkpoint_resume
- [x] 19.7 result schema. — Evidence: CoderResult (outcome, turns, history digests, completion/block report); test_no_reasoning_or_secrets_in_history
- [x] 19.8 code task regression matrix. — Evidence: tests/e2e/test_e2e_code_tasks.py: 8 code task types (Python/FastAPI, SQLite migration, TSX/CSS, PHP, TypeScript, YAML-only, shell, replan) through CoderLoop + real verifier, run by scripts/dev/regression-matrix.sh; loop-level cases tests/integration/test_coder_loop.py (8)

## P20 – Stagnation Detection

- [x] 20.1 action fingerprints. — Evidence: hermclaw/stagnation/fingerprints.py action_fingerprint/action_label/tool_sequence/decision_label; detector signals action, tool_sequence, decision. Tests: test_stagnation_fingerprints.py::test_action_fingerprint_normalises_paths_whitespace_and_order, ::test_tool_sequence_and_decision_label; test_sta
- [x] 20.2 error fingerprints. — Evidence: fingerprints.py normalise_text/error_signature (key error lines, redacted)/extract_failing_tests (pytest, unittest, go, cargo, jest/vitest, mocha, phpunit)/failing_tests_fingerprint; detector signals error, failing_tests, changed_files. Tests: test_stagnation_fingerprints.py::test_two_runs_differing
- [x] 20.3 diff progress. — Evidence: detector.py workspace epochs: diff_hash of the full workspace diff (monitor diff_provider) or action-derived states; no_diff_progress streak. Tests: test_stagnation_detector.py::test_rewriting_identical_content_is_no_progress, ::test_oscillating_content_is_no_progress_in_fallback_mode, ::test_diff_m
- [x] 20.4 thresholds. — Evidence: detector.py level_for/validate_policy using policies.stagnation (2 warning / 3 diagnose / 4 stop). Tests: test_stagnation_detector.py::test_default_threshold_table, ::test_custom_threshold_table, ::test_identical_read_only_actions_follow_the_ladder (hypothesis), ::test_invalid_policy_is_rejected
- [x] 20.5 forced diagnose. — Evidence: actions.py diagnosis_message (asks only for the next action + decision label, no written reasoning), monitor injects it via StagnationDirective. Tests: test_stagnation_actions.py::test_forced_diagnosis_and_strategy_switch, ::test_diagnosis_names_the_previous_decision_label; test_stagnation_detector.
- [x] 20.6 strategy switch. — Evidence: actions.py in_loop_strategy (request_scope_expansion / block_external_failure / switch_to_research / reread_before_edit / switch_approach) + STOP_STRATEGY; persistence.record_events emits strategy.changed. Tests: test_stagnation_actions.py::test_forced_diagnosis_and_strategy_switch, ::test_research_
- [x] 20.7 replan. — Evidence: actions.py LADDER/choose_recommendation (same failing test after changes: heavy_review then replan; scope: replan), replan_hint (ReplanTrigger-compatible reason_code/evidence); persistence.prior_escalations feeds used rungs across attempts. Tests: test_stagnation_actions.py::test_stop_ladder_table, 
- [x] 20.8 stop conditions. — Evidence: detector stop at stop_after, diagnosis cap -> stop, sticky stop, replay protection; actions.decide stop -> research|heavy_review|replan|block; monitor ends the coder loop (outcome stagnated); state persisted in step_attempts.fingerprints['stagnation'] with stale-write guard. Tests: test_stagnation_d

## P21 – Deterministic Verifier

- [x] 21.1 scope. — Evidence: changes.collect_changes: GitReader.changed_files plus status renames; each op judged against the base tree with LocalGit.tree_files (create/modify/delete), falling back to porcelain codes. checks.scope_checks: ScopeGuard.audit and forbidden paths, which always apply. Policy checks: generated_check, 
- [x] 21.2 syntax. — Evidence: syntax.py: Python compile() in-process, strict JSON with a JSONC fallback, tolerant YAML safe loader, tomllib, local parse-only bash -n, batched php -l and node --check in the sandbox with nonce markers. TS/JSX are skipped (left to compile) and missing tools give a skip with a reason. Tests: test_sy
- [x] 21.3 compile. — Evidence: checks.compile_checks/_toolchains: tsconfig.json runs npx tsc --noEmit (skipped without node_modules/typescript), go.mod runs go build ./..., Cargo.toml runs cargo check [--offline]. Each runs only when the repo has the marker and a relevant file changed. Tests: test_compile_go_pass_and_fail, test_c
- [x] 21.4 lint. — Evidence: checks.lint_checks: policies.verifier.lint_commands[language], run only for languages that have changed files; {files} is replaced by shell-quoted paths; purpose=lint. Tests: test_lint_commands_run_only_for_changed_languages, test_lint_command_quotes_paths
- [x] 21.5 unit. — Evidence: evidence.run_test_evidence: framework-aware parsing via tools.testparse (pytest/unittest/npm->jest|vitest|mocha/phpunit/go/cargo/generic auto-detect), min_passed and a per-evidence timeout; unit category; test_runs and command_runs rows. Tests: test_passing_step_is_persisted_with_checks_events_and_c
- [x] 21.6 integration. — Evidence: evidence.categorise_test: integration/e2e and contract categories (check_type integration/contract), same evaluation path as unit. Tests: test_failing_and_insufficient_tests (the integration-subset criterion), test_evidence_helpers
- [x] 21.7 secrets. — Evidence: secrets.SecretScanner over added lines only (git diff -U0 base per file; untracked files are scanned whole; fallback to the GitReader diff, where ***REDACTED*** counts as a finding). Uses the hermclaw.core.redaction patterns, a prefixed-assignment rule, literals registered in DEFAULT_REDACTOR, priva
- [x] 21.8 conflicts. — Evidence: conflicts.scan_conflicts: <<<<<<< ||||||| ======= >>>>>>> at line start in added lines; a bare ======= is ignored in markup files unless real markers are present. Tests: test_conflict_markers_are_found_at_line_start, test_heading_underline_in_markup_is_not_a_conflict, test_conflict_markers_block
- [x] 21.9 presence evidence. — Evidence: evidence.presence: path count or regex match count over the glob (min_matches), with samples (path/line/snippet), ReDoS guard, per-file and total byte budgets. Tests: test_presence_and_absence_evidence, test_passing_step_is_persisted_with_checks_events_and_command_rows
- [x] 21.10 absence evidence. — Evidence: evidence.absence (first-class): the path must be absent (literal paths are found even when git-ignored; directory globs are supported) or the pattern must have 0 matches over the glob. Matching files and lines are reported. Tests: test_presence_and_absence_evidence (fail and fixed-pass cases), test_
- [x] 21.11 command evidence. — Evidence: evidence.command_evidence: expect_exit_code, optional stdout regex, timeout, network only when both the evidence and the step allow it; executor errors and hangs become an error check. Tests: test_command_evidence_exit_code_stdout_and_timeout, test_executor_outage_is_an_error_and_redacted, test_hang
- [x] 21.12 test evidence. — Evidence: checks.require_tests_check: an implement step in a repo with tests (languages.repo_has_tests) needs at least one test criterion that ran and passed; zero executed tests fails; other step kinds and repos without tests are skipped. Tests: test_implement_step_in_repo_with_tests_needs_test_evidence, tes
- [x] 21.13 diff evidence. — Evidence: evidence.diff_evidence: must_change, must_not_change, max_changed_files, allow_empty. Tests: test_diff_evidence, test_passing_step_is_persisted_with_checks_events_and_command_rows
- [x] 21.14 report. — Evidence: report.build_report/run_status (passed iff no blocking fail or error; a summary listing failures; report.failures used by correction). store.start_run/finish_run/abort_run write verification_runs and verification_checks (plus command_runs and test_runs) and emit VERIFIER_STARTED, VERIFIER_CHECK_FAIL

## P22 – Heavy Review

- [x] 22.1 review prompt. — Evidence: hermclaw/review/prompt.py (REVIEW_SYSTEM_PROMPT, build_review_prompt sized to the context window, shrink loop) + hermclaw/review/diff.py (split_diff, max-min fair per-file budget, withheld/generated/file-limit handling). Tests in tests/unit/test_review_prompt.py: test_prompt_contains_all_sections_in
- [~] 22.2 Qwen3.8 profile. — Evidence: HeavyReviewer.profile() resolves config models.by_role('heavy') (alias heavy-review, qwen3.8:27b, think false, 24K context) and calls ChatModel.structured(alias, ..., ReviewDraft, max_tokens/temperature from the profile, per-call timeout = min(profile, policy), overall asyncio.timeout(policies.revie
- [x] 22.3 structured findings. — Evidence: severity.ReviewDraft has the same JSON schema as ReviewContract (title, properties, required) and parses tolerantly; review_runs and review_findings rows plus review.started / review.finding.created / review.finished events are written in reviewer._start/_finish. Tests: test_review_severity.py::test
- [x] 22.4 severity. — Evidence: severity.normalise_severity/normalise_verdict map synonyms; unknown severities become major (fail-closed). normalise_findings canonicalises paths (splits path:line, removes absolute or traversing paths), dedupes, and orders blocker > major > minor; reasoning markup is stripped and texts redacted. Te
- [x] 22.5 major/blocker invariant. — Evidence: invariant.apply_review_invariants enforces: a major/blocker finding means no pass, and a failed verifier means no pass. The model's verdict is persisted as raw_verdict, the effective one as verdict, plus invariant_override. Every error is fail-closed: status 'error', verdict fix_required. Tests: tes
- [x] 22.6 correction request. — Evidence: correction.build_correction_request returns a CorrectionRequest with: verifier_failures (check type, name, status, message, evidence excerpt, path), review_findings (blocker/major first, deduplicated among themselves and against verifier facts, keeping the suggested fix), required_changes, constrain

## P23 – Correction Pipeline

- [x] 23.1 verifier failure correction. — Evidence: ImplementStepHandler._correction (verifier source); tests/integration/test_coder_handler.py::test_verifier_failure_triggers_correction_with_evidence
- [x] 23.2 review correction. — Evidence: review fix_required/major via enforce_review_invariant -> correction; test_review_invariant_major_finding_forces_correction
- [x] 23.3 bounded attempts. — Evidence: steps.correction_count/attempt_count vs policies.correction; test_exhausted_corrections_escalate_to_replan
- [x] 23.4 evidence passed forward. — Evidence: step_attempts.correction_input items -> CorrectionItem in coder prompt; test_verifier_failure_triggers_correction_with_evidence
- [x] 23.5 regression rerun. — Evidence: every attempt fully re-verified; finalize RegressionCheck after base update (test_runtime_driver::test_base_moved_during_job_is_rebased_and_regression_rerun)
- [x] 23.6 escalation. — Evidence: blocked repeated_verifier_failure -> Gemma replan; test_exhausted_corrections_escalate_to_replan

## P24 – Replanning

- [x] 24.1 failure package. — Evidence: failure_package.py build_failure_package collects goal, current plan, completed steps with summaries, failed step with attempts, verifier failed checks, test output tails, failing commands, scope decision, review findings, open steps and research evidence; redacted and bounded. Test: tests/integrati
- [~] 24.2 Gemma replan. — Evidence: replanner.py: the planner alias with the ReplanContract schema, the same 2-repair budget, the rerun_reason rule, merged-DAG and semantic validation, enrichment and the no-blind-repeat rule. Tests: tests/integration/test_planner_replan.py::test_replan_preserves_completed_steps_and_supersedes_the_rest
- [x] 24.3 plan versioning. — Evidence: replanner._persist writes the next plan_versions row (source replanner|fallback, reason, validation history) and updates plans.current_version, jobs.current_plan_version, replan_count += 1 and the max_replans limit; PLAN_CHANGED on stale state. Tests: tests/integration/test_planner_replan.py::test_r
- [x] 24.4 completed-step preservation. — Evidence: Completed rows are kept. A rerun needs rerun_reason: the old row is superseded but stays completed, and a new pending row is created. Other steps are superseded: open or in-flight ones are cancelled through the state machine, failed ones keep failed. Tests: tests/integration/test_planner_replan.py::
- [x] 24.5 new dependencies. — Evidence: materialize_steps maps dependencies to kept completed rows, or to the new row for a re-run key. Tests: tests/integration/test_planner_replan.py::test_replan_preserves_completed_steps_and_supersedes_the_rest (StepDependency rows), ::test_completed_step_needs_rerun_reason (dependants point at the new 
- [x] 24.6 scope refresh. — Evidence: New and changed steps have current_scope_version NULL; active scope_contracts of superseded steps (including a re-run completed step) are set to superseded with a reason. Tests: tests/integration/test_planner_replan.py::test_replan_preserves_completed_steps_and_supersedes_the_rest, ::test_completed_

## P25 – Scheduler

- [x] 25.1 DAG scheduler. — Evidence: hermclaw/scheduler/scheduler.py; tests/integration/test_scheduler.py::test_dag_runs_in_dependency_order_and_finalizes
- [x] 25.2 ready steps. — Evidence: hermclaw/scheduler/scheduler.py; tests/integration/test_scheduler.py::test_dag_runs_in_dependency_order_and_finalizes
- [x] 25.3 capabilities. — Evidence: capability registry (config/capabilities.yaml: step_kind_capability + worker_kind/resources) validated by the planner; workers report capabilities in signed heartbeats and are selected per capability (WorkerRegistry.select_worker via runtime/services.WorkerPool) – tests/integration/test_runtime_dispatch.py::test_ready_worker_is_selected_and_runs_the_command_remotely
- [x] 25.4 resource leases. — Evidence: ImplementStepHandler._model: coder lease during the loop (preemptible -> checkpoint), heavy lease during review; MediaStepHandler hold_media; test_coder_handler.py::test_video_preemption_checkpoints_the_coder_and_the_step_resumes
- [x] 25.5 worker dispatch. — Evidence: runtime/services.ExecutionPool + WorkerPool: remote sandbox on the selected execution worker (signed client, workspace sync), Wake-on-LAN before dispatch, drained/offline workers skipped, WorkerUnreachable when none – tests/integration/test_runtime_dispatch.py (2 tests), tests/integration/test_runtime_services.py::test_remote_execution_without_a_ready_worker_is_reported_not_run_locally
- [x] 25.6 retries. — Evidence: Scheduler._apply_outcome retry/backoff; test_retry_with_backoff_then_success, test_backoff_delays_redispatch
- [x] 25.7 timeouts. — Evidence: SchedulerSettings.step_timeout_seconds; test_step_timeout_is_retryable_failure
- [x] 25.8 parallel steps. — Evidence: parallel read-only steps, serialised mutating steps; test_mutating_steps_are_serialised_per_job, test_priority_and_concurrency_limit
- [x] 25.9 dependency failure. — Evidence: _promote_steps DEPENDENCY_FAILED + replan; test_permanent_failure_blocks_dependents_then_replans

## P26 – SSH/Admin Tools

- [x] 26.1 host registry. — Evidence: hermclaw/sshtools/registry.py checks entries at startup: option-like addresses, user, port, key_ref, and known_hosts paths with whitespace, % or ~ are rejected. tests/unit/test_sshtools_registry_keys.py::test_registry_from_example_config, ::test_unknown_and_unlisted_hosts_are_policy_violations, ::te
- [x] 26.2 SSH keys. — Evidence: hermclaw/sshtools/keys.py: file: keys checked for mode 0600/0400 and owner; cred: keys written to a 0600 key-<pid>-* file in a private 0700 runtime dir and deleted after the run; keys left by dead processes purged; known_hosts mandatory with an ssh-keygen -F entry check; generate_keypair; authorized
- [x] 26.3 command policy. — Evidence: hermclaw/sshtools/policy.py classifies read|mutate|destructive|forbidden, allows sudo only for exact allow_sudo_commands entries, denies chaining (SSH_COMPOUND_NOT_ALLOWED) unless allow-listed, forbids onward hops, and returns Verdict.approval_required with approval_token(host, command, step). tests
- [x] 26.4 read commands. — Evidence: SshTools.ssh_read (read class only, otherwise SSH_NOT_READ_ONLY), ssh_service_status (systemctl is-active + status --no-pager; reports no-systemd hosts as SSH_SERVICE_UNAVAILABLE), SshOpsProvider. tests/integration/test_sshtools_sshd.py::test_ssh_read_runs_and_is_audited, ::test_ssh_read_refuses_non
- [x] 26.5 mutation commands. — Evidence: SshTools.ssh_command and ssh_upload (atomic mktemp + sha256 + chmod + mv, approval bound to path/mode/sha256); hermclaw/sshtools/approvals.py keeps jobs.metadata['ssh_approvals'], each approval single use under FOR UPDATE and consumed in the same transaction as the command_runs 'started' row. tests/
- [x] 26.6 audit. — Evidence: tools.py writes command_runs rows (target='ssh', classification, exit_code, timed_out, redacted excerpts, duration, worker_id=host) and EventType.SSH_COMMAND events for every call, refusals included; a failed audit write is flagged as audit_incomplete. tests/integration/test_sshtools_sshd.py::test_s
- [x] 26.7 timeout. — Evidence: client.py SshRunner runs ssh in its own process group with SIGTERM then SIGKILL on timeout or cancellation, ConnectTimeout, output caps and redaction, and satisfies the RemoteCommandRunner protocol. tests/integration/test_sshtools_sshd.py::test_runner_timeout_kills_the_ssh_client, ::test_runner_canc
- [~] 26.8 sandbox vs host separation. — Evidence: Implemented and tested locally: separation.py (assert_ssh_allowed for 'ssh'/'deploy', sandbox_isolation_issues, scrub_ssh_environment) and scripts/ops/hermclaw-ssh-gate (POSIX sh, SSH_ORIGINAL_COMMAND, whole-line ERE allow-list, shell: opt-in for metacharacters, syslog; tested with dash, bash and bu

## P27 – DB Tools

- [ ] 27.1 introspection. — Evidence: –
- [ ] 27.2 readonly query. — Evidence: –
- [ ] 27.3 migrations. — Evidence: –
- [ ] 27.4 sandbox apply. — Evidence: –
- [ ] 27.5 backup. — Evidence: –
- [ ] 27.6 restore test. — Evidence: –
- [ ] 27.7 policy. — Evidence: –

## P28 – Container/Proxy Tools

- [ ] 28.1 inspect. — Evidence: –
- [ ] 28.2 compose validate. — Evidence: –
- [ ] 28.3 sandbox startup. — Evidence: –
- [ ] 28.4 port collision. — Evidence: –
- [ ] 28.5 Nginx test. — Evidence: –
- [ ] 28.6 rollback artifact. — Evidence: –

## P29 – Media

- [x] 29.1 image step. — Evidence: worker/media (comfyui backend) + hermclaw/media/handler.py; tests/integration/test_media_worker.py::test_comfyui_workflow_with_overrides_and_rejection, test_media_handler.py
- [x] 29.2 video step. — Evidence: ffmpeg backend + MediaStepHandler; test_media_handler.py::test_video_step_registers_artifacts_and_releases_gpu, test_media_worker.py::test_video_transcode_thumbnail_probe_concat_unloads_models_first
- [x] 29.3 GPU leases. — Evidence: ResourceManager.hold_media (gpu-224 + video-224, priority video 100/image 90); test_media_handler.py::test_video_preempts_running_ai_lease
- [x] 29.4 safe AI drain. — Evidence: drain leases on AI model groups + preemption request -> AI holder yields (checkpoint); test_video_preempts_running_ai_lease
- [x] 29.5 model unload. — Evidence: media service unloads all Ollama models before GPU work, residency lock; test_video_transcode_thumbnail_probe_concat_unloads_models_first
- [x] 29.6 artifacts. — Evidence: sha256-verified download into artifacts_dir + artifacts rows (kind image/video); test_video_step_registers_artifacts_and_releases_gpu
- [x] 29.7 resume AI. — Evidence: lease released after the job; scheduler resumes checkpointed AI steps (scheduler checkpoint resume tests); test_video_step_registers_artifacts_and_releases_gpu asserts no active leases

## P30 – API

- [x] 30.1 job API. — Evidence: hermclaw/api/jobs.py POST/GET /api/jobs, GET /api/jobs/{id} (tests/integration/test_api.py)
- [x] 30.2 controls. — Evidence: cancel/pause/resume/retry/replan (test_job_lifecycle_controls)
- [x] 30.3 workers. — Evidence: GET /api/workers
- [x] 30.4 models. — Evidence: GET /api/models (+stats aus model_invocations)
- [x] 30.5 resources. — Evidence: GET /api/resources
- [x] 30.6 artifacts. — Evidence: GET /api/jobs/{id}/artifacts, /api/artifacts/{id}/download (confined, test_artifact_download_confined)
- [x] 30.7 events. — Evidence: GET /api/jobs/{id}/events + SSE /events/stream (test_sse_stream_over_real_http)
- [x] 30.8 research. — Evidence: GET /api/jobs/{id}/research
- [x] 30.9 auth. — Evidence: Bearer-Token + Scopes read/control/admin, api_tokens (test_auth_required_and_scopes)

## P31 – UI

- [x] 31.1 layout. — Evidence: ui/src/components/Layout.tsx, App.tsx, lib/router.tsx, pages/Login.tsx; E2E auth.spec.ts ('ungültiges Token wird abgewiesen…', 'abgelaufenes Token führt zurück zur Anmeldung', 'Skip-Link springt zum Hauptinhalt'), pages.spec.ts 'Hauptnavigation markiert die aktive Seite…', job-detail.spec.ts 'Kopf, 
- [x] 31.2 dashboard. — Evidence: ui/src/pages/Dashboard.tsx, lib/aggregate.ts; E2E dashboard.spec.ts 'zeigt Jobs, Warteschlange, Worker, Modelle, GPU, Fehler, Research und Bugs' and 'Live-Statuszeilen kommen über den globalen SSE-Stream'
- [x] 31.3 jobs. — Evidence: ui/src/pages/Jobs.tsx, components/JobCreateForm.tsx, lib/jobForm.ts; E2E jobs.spec.ts 'Liste zeigt die Jobs und filtert nach Status', 'Formular validiert Eingaben und legt einen Job über die API an', 'Serverfehler werden angezeigt'; unit helpers.test.ts
- [x] 31.4 DAG. — Evidence: ui/src/lib/dag.ts (longest-path layers, barycenter ordering, cycle/missing-dependency tolerance), components/DagView.tsx, pages/job/PlanTab.tsx; unit dag.test.ts (6 tests); E2E job-detail.spec.ts 'Plan-Tab zeigt den DAG mit Status-Farben und Planversionen'
- [x] 31.5 live events. — Evidence: ui/src/lib/sse.ts (ReconnectingEventStream: backoff, Last-Event-ID cursor, de-dup, half-open watchdog), lib/useEvents.ts (REST history + SSE + 500-sequence reconcile window), pages/job/EventsTab.tsx, StepsTab.tsx + components/StepDetailView.tsx; unit sse.test.ts (11); E2E live-events.spec.ts ('neue 
- [x] 31.6 diff. — Evidence: ui/src/lib/diff.ts (parseUnifiedDiff, isDiffTruncated), components/DiffViewer.tsx, pages/job/EvidenceTabs.tsx ChangesTab; unit diff.test.ts (11 incl. new isDiffTruncated tests); E2E job-detail.spec.ts 'Änderungen: Diff-Viewer mit Dateiköpfen und +/- Färbung' and new 'Änderungen: gekürzter Diff wird 
- [x] 31.7 tests. — Evidence: ui/src/pages/job/EvidenceTabs.tsx TestsTab, components/Evidence.tsx TestRunsTable, lib/aggregate.ts collectTests; E2E job-detail.spec.ts 'Tests-, Verifier- und Review-Tab aggregieren über alle Schritte'
- [x] 31.8 verifier. — Evidence: ui/src/pages/job/EvidenceTabs.tsx VerifierTab, components/Evidence.tsx VerificationRunView, lib/aggregate.ts collectVerifications; E2E job-detail.spec.ts 'Tests-, Verifier- und Review-Tab…' + 'Schritt-Detail…'
- [x] 31.9 review. — Evidence: ui/src/pages/job/EvidenceTabs.tsx ReviewTab, components/Evidence.tsx ReviewRunView, lib/aggregate.ts collectReviews; E2E job-detail.spec.ts 'Tests-, Verifier- und Review-Tab…'
- [x] 31.10 sources. — Evidence: ui/src/pages/job/ResearchTab.tsx (runs, sources sorted by authority/relevance, claims with [n] links to sources, contradictions; only http(s) URLs linked via safeExternalUrl, rel=noopener noreferrer); E2E job-detail.spec.ts 'Research-Quellen: Läufe, Quellen mit Autorität/Relevanz, Aussagen, Widerspr
- [x] 31.11 workers. — Evidence: ui/src/pages/Workers.tsx (state badges STARTING/WAKING/READY/BUSY/ERROR/SLEEPING/OFFLINE/DRAINING, detail with GPU + loaded models, drain); E2E workers.spec.ts 'Liste mit Zustands-Badges, Detail mit GPU und geladenen Modellen', 'Drain mit Bestätigung setzt den Worker auf DRAINING und wieder zurück'
- [x] 31.12 resources. — Evidence: ui/src/pages/Resources.tsx, pages/job/MiscTabs.tsx (job resources tab); E2E pages.spec.ts 'aktive Leases und wartende Anfragen mit Job-Verweisen', job-detail.spec.ts 'Repository, Ressourcen, Artefakte, Medien, Git, Fehler'
- [x] 31.13 media. — Evidence: ui/src/pages/Media.tsx, components/MediaGallery.tsx, components/Artifacts.tsx (authenticated blob preview for image/video, download); E2E pages.spec.ts 'Job wählen zeigt Bild-Vorschau über die Artefakt-Download-URL und Medien-Events'
- [x] 31.14 errors. — Evidence: ui/src/pages/job/MiscTabs.tsx Errors tab (error/warning events + job error), Git tab, Artifacts tab; E2E job-detail.spec.ts 'Repository, Ressourcen, Artefakte, Medien, Git, Fehler' and 'Fehler-Tab eines fehlgeschlagenen Jobs zeigt den Job-Fehler'
- [x] 31.15 controls. — Evidence: ui/src/components/JobControls.tsx, ConfirmDialog.tsx, lib/controls.ts (rules verified to match hermclaw/api/jobs.py::_control and state_machines: retry only from failed/blocked); E2E controls.spec.ts 'Pausieren und Fortsetzen…', 'Neuplanung mit Begründung', 'Abbrechen eines wartenden Jobs…; danach s

## P32 – Deployment

- [ ] 32.1 `.225` runtime systemd. — Evidence: –
- [ ] 32.2 `.223` UI. — Evidence: –
- [ ] 32.3 `.222` worker. — Evidence: –
- [ ] 32.4 `.224` worker. — Evidence: –
- [ ] 32.5 LiteLLM. — Evidence: –
- [ ] 32.6 Ollama models. — Evidence: –
- [ ] 32.7 Nginx. — Evidence: –
- [ ] 32.8 secrets. — Evidence: –
- [ ] 32.9 health checks. — Evidence: –
- [ ] 32.10 Ansible idempotency. — Evidence: –

## P33 – Backup/Restore

- [ ] 33.1 PostgreSQL backup. — Evidence: –
- [ ] 33.2 config backup. — Evidence: –
- [ ] 33.3 artifacts. — Evidence: –
- [ ] 33.4 `.60` target. — Evidence: –
- [ ] 33.5 restore environment. — Evidence: –
- [ ] 33.6 restore verification. — Evidence: –

## P34 – Recovery

- [x] 34.1 kill runtime during job. — Evidence: tests/failure/test_recovery_process_kill.py::test_kill_during_coder_step_restart_recovers_and_finishes (real SchedulerProcess child SIGKILLed while the coder works on S001), ::test_kill_during_planning_is_reentrant
- [x] 34.2 restart. — Evidence: restart with the same stable instance id scheduler@<instance> (runtime/services.resource_holder_id): startup_recovery reclaims own step leases immediately; restarted process exits 0 on SIGTERM – tests/failure/test_recovery_process_kill.py::test_kill_during_coder_step_restart_recovers_and_finishes
- [x] 34.3 reconstruct. — Evidence: Scheduler.startup_recovery: lost attempt -> status lost / WORKER_LOST, step ready (no checkpoint) or checkpointed, prepare-phase jobs re-entered with job.recovered event – tests/failure/test_recovery_process_kill.py (both tests); tests/integration/test_scheduler.py::test_crash_recovery_requeues_in_flight_steps
- [x] 34.4 leased worker recovery. — Evidence: orchestrator side: step leases of the dead incarnation reclaimed on restart (tests/failure/test_recovery_process_kill.py); execution worker side: worker/execution recover_for_sandbox on daemon start – tests/integration/test_workers_execution_daemon.py::test_startup_recovery_removes_leftovers, ::test_container_recovery_with_real_podman
- [x] 34.5 model load recovery. — Evidence: coder model lease (large-model budget) of the killed process released by ResourceManager.recover() at SchedulerProcess start – tests/failure/test_recovery_process_kill.py asserts held lease of scheduler@<instance> before the kill and no active/preempting lease after; tests/failure/test_resources_recovery.py::test_leases_survive_crash_and_recover_releases_previous_incarnation
- [x] 34.6 workspace recovery. — Evidence: same workspace reused after the crash (uncommitted edit preserved, no re-clone), integrity-checked by GitEngine – tests/failure/test_recovery_process_kill.py::test_kill_during_coder_step_restart_recovers_and_finishes
- [x] 34.7 Git state recovery. — Evidence: exactly one verified commit on the job branch after kill+restart (no duplicate/partial commit), pushed – tests/failure/test_recovery_process_kill.py (_branch_commits == 1 in both scenarios); tests/failure/test_gitops_failures.py::test_interrupted_rebase_is_detected_and_recovered

## P35 – Failure Injection

- [x] 35.1 Planner timeout — Evidence: tests/failure/test_runtime_failures.py::test_planner_timeout_fails_preparation_with_a_timeout_code; gateway fallback gemma4:12b tests/integration/test_planner_repos.py::test_gateway_fallback_is_recorded
- [x] 35.2 invalid Planner JSON — Evidence: tests/integration/test_planner_repos.py::test_schema_invalid_answer_is_repaired, ::test_failure_after_two_repairs_raises_planner_error; tests/integration/test_runtime_driver.py::test_invalid_planner_output_fails_preparation
- [x] 35.3 Coder timeout — Evidence: tests/integration/test_coder_loop.py::test_invalid_model_output_twice_fails_and_timeout_is_model_failure; scheduler step timeout tests/integration/test_scheduler.py::test_step_timeout_is_retryable_failure
- [x] 35.4 Worker offline — Evidence: tests/failure/test_workers_failures.py::test_killed_daemon_is_detected_by_offline_sweep; tests/integration/test_runtime_dispatch.py (offline worker woken, drained skipped); tests/integration/test_runtime_services.py::test_remote_execution_without_a_ready_worker_is_reported_not_run_locally
- [x] 35.5 Ollama unavailable — Evidence: tests/failure/test_models_failures.py::test_unreachable_litellm_tries_fallback_then_fails, ::test_ollama_error_bodies_are_redacted; tests/failure/test_wol_failures.py::test_model_service_error_status
- [x] 35.6 GitLab unavailable — Evidence: tests/failure/test_gitops_failures.py::test_gitlab_down_raises_gitlab_error; tests/integration/test_gitops_push.py::test_merge_request_failure_does_not_fail_push
- [x] 35.7 DB reconnect — Evidence: tests/failure/test_runtime_failures.py::test_db_connections_killed_mid_job_are_reestablished_and_the_job_finishes (pg_terminate_backend of every backend during planning/running, pool_pre_ping); tests/failure/test_repo_intelligence_failures.py::test_database_down_degrades_to_live_retrieval
- [x] 35.8 process crash — Evidence: tests/failure/test_recovery_process_kill.py (SIGKILL of the scheduler process, 2 scenarios); tests/failure/test_workers_failures.py::test_killed_daemon_is_detected_by_offline_sweep
- [x] 35.9 full disk warning — Evidence: hermclaw/runtime/health.py DiskMonitor (system.disk events, admission pause while critical): tests/failure/test_runtime_failures.py::test_disk_monitor_warns_once_per_level_change_and_reports_recovery, ::test_critical_disk_pauses_job_admission_until_space_returns; worker heartbeat disk_free_mb
- [x] 35.10 scope violation — Evidence: tests/failure/test_scope_failures.py (8 tests); tests/integration/test_coder_loop.py::test_out_of_scope_write_is_refused_and_reported_to_the_model; gitops StagingGuard tests
- [x] 35.11 secret found — Evidence: tests/unit/test_verifier_secrets.py; tests/failure/test_models_failures.py::test_echoed_secrets_never_persisted; tests/failure/test_context_builder_failures.py::test_secrets_are_redacted_everywhere_in_the_prompt
- [x] 35.12 merge conflict — Evidence: tests/failure/test_gitops_failures.py::test_update_to_base_conflict_aborts_cleanly, ::test_update_to_base_autostash_conflict_restores_everything; tests/integration/test_runtime_driver.py::test_conflicting_base_change_fails_with_merge_conflict
- [x] 35.13 stale base SHA — Evidence: tests/failure/test_gitops_failures.py::test_stale_base_detected_with_details, ::test_push_with_require_current_base_refuses_stale; tests/integration/test_runtime_driver.py::test_base_moved_during_job_is_rebased_and_regression_rerun
- [x] 35.14 verifier crash — Evidence: tests/failure/test_verifier_failures.py::test_verifier_crash_is_persisted_as_error, ::test_crashing_check_group_does_not_abort_the_run
- [x] 35.15 reviewer timeout — Evidence: tests/failure/test_review_failures.py::test_review_timeout_fails_closed, ::test_gateway_timeout_fails_closed
- [x] 35.16 WOL timeout — Evidence: tests/failure/test_wol_failures.py::test_ping_timeout_with_bounded_resends, ::test_ssh_timeout, ::test_overall_deadline_caps_every_stage
- [x] 35.17 research source unavailable — Evidence: tests/integration/test_research_step.py::test_research_without_sources_retries_then_blocks_for_the_replanner; tests/integration/test_research_engine.py::test_failed_blocked_unsupported_and_tiny_sources_are_recorded_and_run_continues; tests/failure/test_research_failures.py::test_unexpected_fetcher_and_search_exceptions_are_isolated
- [x] 35.18 network failure — Evidence: tests/failure/test_workers_failures.py::test_heartbeat_sender_backs_off_when_orchestrator_unreachable; tests/failure/test_wol_failures.py::test_socket_errors_are_retried_up_to_max_attempts; tests/integration/test_research_engine.py::test_no_usable_source_marks_run_failed
- [x] 35.19 video preemption — Evidence: tests/integration/test_media_handler.py::test_video_preempts_running_ai_lease; tests/integration/test_coder_handler.py::test_video_preemption_checkpoints_the_coder_and_the_step_resumes; tests/integration/test_resources_preemption.py

## P36 – Security Hardening

- [x] 36.1 secret scan. — Evidence: tests/security/test_security_checks.py::test_repository_contains_no_secrets (verifier SecretScanner over every tracked non-test file; found BUG-041); job diffs: tests/unit/test_verifier_secrets.py, verifier secret check in every verification run
- [x] 36.2 permissions. — Evidence: API scopes read/control/admin: tests/integration/test_api.py::test_auth_required_and_scopes; tests/security/test_security_checks.py::test_worker_api_views_need_admin_scope_and_heartbeats_a_signature, ::test_secret_references_policy_in_production (0400 secret files, no env:/literal: in production), ::test_api_errors_never_echo_tokens
- [x] 36.3 worker auth. — Evidence: HMAC-signed worker requests (method/path/timestamp/nonce/body hash, skew window, replay cache, rotation): tests/unit/test_workers_auth.py (12 tests), tests/integration/test_workers_api.py::test_heartbeat_auth_failures, ::test_unauthenticated_heartbeat_rejected_before_body_is_read, tests/failure/test_workers_failures.py::test_orchestrator_rejects_heartbeats_with_rotated_out_token; tests/security/test_security_checks.py (main app)
- [ ] 36.4 SSH restrictions. — Evidence: –
- [x] 36.5 command policy. — Evidence: policies.commands read/mutate/destructive/forbidden: tests/integration/test_tools_engine_exec.py::test_forbidden_and_destructive_commands_are_refused, ::test_destructive_commands_need_explicit_capability; tests/unit/test_tools_units.py
- [x] 36.6 sandbox escape checks. — Evidence: rootless podman sandbox: tests/integration/test_sandbox_podman.py::test_workspace_rw_rootfs_ro_tmp_rw, ::test_only_workspace_tmp_and_shm_writable, ::test_capabilities_dropped_and_no_new_privileges, ::test_pids_limit_enforced, ::test_memory_limit_enforced, ::test_secret_values_never_on_engine_command_line; workspace sync refuses .git/escapes: tests/integration/test_tools_remote_executor.py; symlink/embedded repo staging guards: tests/integration/test_gitops_engine.py::test_stage_refuses_escaping_symlink_and_odd_names
- [x] 36.7 network policy. — Evidence: sandbox network off by default / explicit per step: tests/integration/test_sandbox_podman.py::test_network_off_by_default, ::test_network_allowed_mode_has_route; research SSRF guard (private ranges, NAT64, redirects): tests/failure/test_research_failures.py, tests/integration/test_research_fetch_search.py
- [x] 36.8 dependency audit. — Evidence: docs/security/dependency-audit.md (2026-10-13: runtime venv 98 packages, LiteLLM venv 115 packages – no known vulnerabilities, pip-audit); reproducible: scripts/dev/dependency-audit.sh (incl. npm audit for ui/)

## P37 – Generalization E2E

- [x] 37.1 Python FastAPI Feature. — Evidence: tests/e2e/test_e2e_code_tasks.py::test_37_1_python_fastapi_feature (full runtime: triage, plan, scope, coder tools, real pytest acceptance, heavy review, commit, push, report)
- [x] 37.2 SQLite Schema Migration. — Evidence: tests/e2e/test_e2e_code_tasks.py::test_37_2_sqlite_schema_migration (new migration + test, 001 untouched via diff evidence)
- [ ] 37.3 PostgreSQL Migration. — Evidence: –
- [ ] 37.4 Docker Compose Service. — Evidence: –
- [x] 37.5 React/CSS Navigation. — Evidence: tests/e2e/test_e2e_code_tasks.py::test_37_5_react_css_navigation (TSX via tsc + node --test, CSS presence)
- [x] 37.6 PHP Backend Form. — Evidence: tests/e2e/test_e2e_code_tasks.py::test_37_6_php_backend_form (php -l + php test script)
- [x] 37.7 TypeScript Utility. — Evidence: tests/e2e/test_e2e_code_tasks.py::test_37_7_typescript_utility (tsc + node --test; found BUG-042)
- [x] 37.8 YAML-only Config. — Evidence: tests/e2e/test_e2e_code_tasks.py::test_37_8_yaml_only_config (YAML JSON-schema evidence, max_changed_files 1)
- [x] 37.9 Shell/Linux Admin. — Evidence: tests/e2e/test_e2e_code_tasks.py::test_37_9_shell_linux_admin (bash -n + behavioural shell test)
- [x] 37.10 Research-only Task. — Evidence: tests/e2e/test_e2e_platform.py::test_37_10_research_only_task (job without repo, real ResearchEngine, sources in final report)
- [x] 37.11 Image Job. — Evidence: tests/e2e/test_e2e_platform.py::test_37_11_image_job (real media daemon + ffmpeg thumbnail, PNG artifact, leases released)
- [x] 37.12 Video Job. — Evidence: tests/e2e/test_e2e_platform.py::test_37_12_video_job (real media daemon + ffmpeg VP9 transcode, verified artifact)
- [x] 37.13 Wake-on-LAN Job. — Evidence: tests/e2e/test_e2e_platform.py::test_37_13_wake_on_lan_job_runs_on_the_woken_remote_worker (sleeping execution worker woken before dispatch, commands on the remote daemon copy, .git never synced); live magic packet: tests/integration/test_wol_live.py (BLOCKER-001)
- [x] 37.14 Worker failure/recovery. — Evidence: tests/e2e/test_e2e_platform.py::test_37_14_worker_restart_and_connection_loss_are_recovered (daemon restarted mid-job with empty state, transient connection reset retried, workspace re-synced); process kill: tests/failure/test_recovery_process_kill.py
- [x] 37.15 planner replan after failure. — Evidence: tests/e2e/test_e2e_code_tasks.py::test_37_15_planner_replan_after_failure (coder request_replan -> Gemma replan -> new steps succeed, replan_count 1)

## P38 – Performance / Load

- [x] 38.1 concurrent jobs. — Evidence: tests/load/test_load.py::test_38_1_concurrent_jobs (6 full jobs, concurrency 4, all succeeded, wall 12.6 s) – docs/operations/performance-baseline.md
- [x] 38.2 event volume. — Evidence: tests/load/test_load.py::test_38_2_event_volume (20 000 events: append ~570/s, replay ~29 000/s) – docs/operations/performance-baseline.md
- [x] 38.3 DB load. — Evidence: tests/load/test_load.py::test_38_3_db_load_many_steps_skip_locked (125 steps, tick p95 227 ms, exactly-once dispatch) – docs/operations/performance-baseline.md
- [x] 38.4 context build timings. — Evidence: tests/load/test_load.py::test_38_4_context_build_timings (1 501 files: inventory 3.5 s cold, build ~0.85 s) – docs/operations/performance-baseline.md
- [x] 38.5 model load/unload. — Evidence: tests/load/test_load.py::test_38_5_model_load_unload_and_lease_latency (lease p50 22 ms, hand-over ~0.27 s; real model load time live-only, BLOCKER-001) – docs/operations/performance-baseline.md
- [x] 38.6 worker wake. — Evidence: tests/load/test_load.py::test_38_6_worker_wake_latency (0.41 s with 0.3 s simulated boot; real boot live-only) – docs/operations/performance-baseline.md
- [x] 38.7 video preemption. — Evidence: tests/load/test_load.py::test_38_7_video_preemption_latency (GPU grant 0.34 s after request, never shared) – docs/operations/performance-baseline.md
- [x] 38.8 memory usage. — Evidence: tests/load/test_load.py::test_38_8_scheduler_process_memory (peak RSS 104 MB for a full job) – docs/operations/performance-baseline.md

## P39 – FULL BUG BASH / HARDENING

- [ ] 39.1 `BUGS.md` vollständig reviewen. — Evidence: –
- [ ] 39.2 jeden offenen Bug reproduzieren. — Evidence: –
- [ ] 39.3 stale Bugs schließen mit Beleg. — Evidence: –
- [ ] 39.4 P0 fixen. — Evidence: –
- [ ] 39.5 P1 fixen. — Evidence: –
- [ ] 39.6 P2 fixen. — Evidence: –
- [ ] 39.7 P3 soweit reproduzierbar und sinnvoll fixen. — Evidence: –
- [ ] 39.8 für jeden Fix Regressiontest. — Evidence: –
- [ ] 39.9 komplette Testmatrix neu. — Evidence: –
- [ ] 39.10 Failure Tests neu. — Evidence: –
- [ ] 39.11 Recovery neu. — Evidence: –
- [ ] 39.12 Security neu. — Evidence: –
- [ ] 39.13 E2E neu. — Evidence: –

## P40 – SOAK TEST

- [~] 40.1 System vollständig starten. — Evidence: local: API + scheduler + execution worker processes started by scripts/soak/run_soak.py (docs/operations/soak-local.md); target hosts pending (BLOCKER-001)
- [~] 40.2 mehrere reale Jobs. — Evidence: 118 jobs via HTTP API, 118 succeeded (real git/verifier/review/commit/push, scripted model answers) – docs/operations/soak-local.md; real models on .224 pending (BLOCKER-001)
- [~] 40.3 24h/geeigneter Dauerbetrieb soweit Umgebung erlaubt. — Evidence: 60 min continuous local soak with fault injection – docs/operations/soak-local.md; 24 h on the target hosts after deployment (BLOCKER-001)
- [x] 40.4 Ressourcen prüfen. — Evidence: leases, in-flight steps and RSS sampled every 60 s, all back to zero between jobs – docs/operations/soak-local.md
- [x] 40.5 Leaks. — Evidence: RSS growth first→last quarter API +0.5 %, scheduler +3.7 %, worker +2.7 % over 118 jobs – docs/operations/soak-local.md
- [x] 40.6 stuck leases. — Evidence: 0 active/preempting leases and 0 waiting requests after drain – docs/operations/soak-local.md
- [x] 40.7 worker reconnect. — Evidence: execution worker SIGKILLed: offline after 51.5 s, ready 2.2 s after restart – docs/operations/soak-local.md
- [x] 40.8 event ordering. — Evidence: 12 589 events: no duplicate sequence, no per-job ordering violation – docs/operations/soak-local.md
- [x] 40.9 DB integrity. — Evidence: no unvalidated constraints, no orphans, no open attempts of terminal jobs, report for every succeeded job – docs/operations/soak-local.md

## P41 – RELEASE CANDIDATE

- [ ] 41.1 Version. — Evidence: –
- [ ] 41.2 Changelog. — Evidence: –
- [ ] 41.3 Architektur-Doku. — Evidence: –
- [ ] 41.4 Admin Runbook. — Evidence: –
- [ ] 41.5 Disaster Recovery. — Evidence: –
- [ ] 41.6 Upgrade Path. — Evidence: –
- [ ] 41.7 known limitations. — Evidence: –
- [ ] 41.8 all tests final. — Evidence: –
- [ ] 41.9 release tag. — Evidence: –

## P42 – CUTOVER PLAN

- [~] 42.1 altes Hermclaw read-only. — Evidence: docs/runbooks/cutover.md §1: old WebUI read-only (limit_except), patch watcher stopped/disabled (not removed), GitLab project archived; execution on the hosts pending (BLOCKER-001)
- [~] 42.2 keine Daten löschen. — Evidence: docs/runbooks/cutover.md §1/§5: no rm/DROP/branch deletion during cutover, legacy snapshot to .60; execution pending (BLOCKER-001)
- [~] 42.3 neues System produktiv. — Evidence: docs/runbooks/cutover.md §0/§2: go/no-go checklist + single nginx site switch on .223 (own webroot/units/DB/branches); execution pending (BLOCKER-001)
- [x] 42.4 Rollback dokumentieren. — Evidence: docs/runbooks/cutover.md §4: complete rollback commands (nginx site back, watcher re-enabled, project un-archived, new runtime stopped; no data loss)
- [x] 42.5 Monitoring. — Evidence: GET /api/metrics (hermclaw/api/metrics.py, tests/integration/test_api_metrics.py) + alert suggestions, liveness, health – docs/runbooks/cutover.md §3
- [~] 42.6 erst nach stabiler Laufzeit Altkomponenten stilllegen. — Evidence: docs/runbooks/cutover.md §5: decommission criteria (14 days without P0/P1, 20 jobs, 2 restore tests) and order; execution after stable operation

