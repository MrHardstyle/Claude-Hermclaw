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
- [ ] 3.5 repositories. — Evidence: –
- [x] 3.6 transaction boundary. — Evidence: session_scope() commit/rollback
- [x] 3.7 migration tests. — Evidence: test_migration_matches_models (Autogenerate-Diff leer)
- [x] 3.8 rollback tests. — Evidence: test_migration_downgrade_and_upgrade_roundtrip
- [x] 3.9 restart persistence test. — Evidence: test_restart_persistence

## P04 – Event Store

- [x] 4.1 append-only event contract. — Evidence: hermclaw/contracts/events.py EventEnvelope + EventType
- [x] 4.2 event service. — Evidence: hermclaw/events/store.py append_event (redacted, NOTIFY)
- [x] 4.3 sequence ordering. — Evidence: BIGINT IDENTITY sequence (test_append_orders_and_redacts)
- [x] 4.4 correlation IDs. — Evidence: correlation_id (Default = job_id)
- [ ] 4.5 SSE endpoint. — Evidence: –
- [x] 4.6 reconnect/last-event-id. — Evidence: stream_events Replay ab Last-Event-ID (test_sse_stream_replay_then_live_and_reconnect)
- [x] 4.7 event retention. — Evidence: purge_events (test_retention_keeps_active_jobs)
- [ ] 4.8 UI test client. — Evidence: –

## P05 – State Machines

- [x] 5.1 Job states. — Evidence: hermclaw/runtime/state_machines.py
- [x] 5.2 Step states. — Evidence: hermclaw/runtime/state_machines.py
- [x] 5.3 transition tables. — Evidence: hermclaw/runtime/state_machines.py
- [x] 5.4 invalid transitions. — Evidence: InvalidTransition (test_invalid_transition_raises)
- [x] 5.5 property tests. — Evidence: Hypothesis random walks (tests/unit/test_state_machines.py)
- [x] 5.6 recovery mapping. — Evidence: recovery_step_state/recovery_job_state

## P06 – Git Engine

- [ ] 6.1 repository registry. — Evidence: –
- [ ] 6.2 clone/fetch. — Evidence: –
- [ ] 6.3 base SHA. — Evidence: –
- [ ] 6.4 isolated workspaces. — Evidence: –
- [ ] 6.5 job branches. — Evidence: –
- [ ] 6.6 status/diff. — Evidence: –
- [ ] 6.7 safe staging. — Evidence: –
- [ ] 6.8 Runtime commit. — Evidence: –
- [ ] 6.9 Runtime push. — Evidence: –
- [ ] 6.10 protected branch tests. — Evidence: –
- [ ] 6.11 stale base detection. — Evidence: –
- [ ] 6.12 conflict handling. — Evidence: –

## P07 – Worker Protocol

- [ ] 7.1 Worker API schema. — Evidence: –
- [ ] 7.2 heartbeat. — Evidence: –
- [ ] 7.3 capability registry. — Evidence: –
- [ ] 7.4 health. — Evidence: –
- [ ] 7.5 worker auth. — Evidence: –
- [ ] 7.6 `.222` daemon. — Evidence: –
- [ ] 7.7 `.224` daemon. — Evidence: –
- [ ] 7.8 offline detection. — Evidence: –
- [ ] 7.9 version compatibility. — Evidence: –

## P08 – Model Gateway

- [ ] 8.1 LiteLLM adapter. — Evidence: –
- [ ] 8.2 model profiles. — Evidence: –
- [ ] 8.3 Fast Qwen profile. — Evidence: –
- [ ] 8.4 Gemma Planner profile. — Evidence: –
- [ ] 8.5 Coder profile. — Evidence: –
- [ ] 8.6 Heavy profile. — Evidence: –
- [ ] 8.7 embedding profile. — Evidence: –
- [ ] 8.8 health checks. — Evidence: –
- [ ] 8.9 context validation. — Evidence: –
- [ ] 8.10 load/unload adapter. — Evidence: –
- [ ] 8.11 metrics. — Evidence: –

## P09 – Resource Manager

- [ ] 9.1 lease table. — Evidence: –
- [ ] 9.2 acquisition. — Evidence: –
- [ ] 9.3 release. — Evidence: –
- [ ] 9.4 heartbeat/expiry. — Evidence: –
- [ ] 9.5 priority. — Evidence: –
- [ ] 9.6 safe preemption. — Evidence: –
- [ ] 9.7 large-model exclusivity. — Evidence: –
- [ ] 9.8 video priority. — Evidence: –
- [ ] 9.9 crash recovery. — Evidence: –

## P10 – Wake-on-LAN

- [ ] 10.1 config. — Evidence: –
- [ ] 10.2 WOL send. — Evidence: –
- [ ] 10.3 ping wait. — Evidence: –
- [ ] 10.4 SSH wait. — Evidence: –
- [ ] 10.5 worker API wait. — Evidence: –
- [ ] 10.6 Ollama wait. — Evidence: –
- [ ] 10.7 failure states. — Evidence: –
- [ ] 10.8 UI events. — Evidence: –
- [ ] 10.9 idle sleep hooks. — Evidence: –

## P11 – Repository Intelligence

- [ ] 11.1 inventory. — Evidence: –
- [ ] 11.2 language detection. — Evidence: –
- [ ] 11.3 build/test discovery. — Evidence: –
- [ ] 11.4 lexical search. — Evidence: –
- [ ] 11.5 symbol index. — Evidence: –
- [ ] 11.6 AST adapters. — Evidence: –
- [ ] 11.7 dependency relations. — Evidence: –
- [ ] 11.8 EmbeddingGemma index. — Evidence: –
- [ ] 11.9 pgvector store. — Evidence: –
- [ ] 11.10 fusion ranking. — Evidence: –
- [ ] 11.11 targeted read. — Evidence: –
- [ ] 11.12 incremental reindex by Git SHA. — Evidence: –

## P12 – Research Engine

- [ ] 12.1 query planner. — Evidence: –
- [ ] 12.2 web search interface. — Evidence: –
- [ ] 12.3 HTTP/browser fetch. — Evidence: –
- [ ] 12.4 source records. — Evidence: –
- [ ] 12.5 content extraction. — Evidence: –
- [ ] 12.6 claim extraction. — Evidence: –
- [ ] 12.7 source-to-claim links. — Evidence: –
- [ ] 12.8 freshness. — Evidence: –
- [ ] 12.9 authority/relevance. — Evidence: –
- [ ] 12.10 contradiction detection. — Evidence: –
- [ ] 12.11 Qwen synth. — Evidence: –
- [ ] 12.12 Gemma deep synth. — Evidence: –
- [ ] 12.13 UI source stream. — Evidence: –

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

- [ ] 14.1 planner prompt contract. — Evidence: –
- [ ] 14.2 structured output. — Evidence: –
- [ ] 14.3 schema validation. — Evidence: –
- [ ] 14.4 plan repair. — Evidence: –
- [ ] 14.5 DAG validation. — Evidence: –
- [ ] 14.6 dependency validation. — Evidence: –
- [ ] 14.7 risk assignment. — Evidence: –
- [ ] 14.8 acceptance generation. — Evidence: –
- [ ] 14.9 research request generation. — Evidence: –
- [ ] 14.10 planner tests across unrelated repos. — Evidence: –

## P15 – Scope Engine

- [ ] 15.1 planner hints intake. — Evidence: –
- [ ] 15.2 repo intelligence evidence. — Evidence: –
- [ ] 15.3 policy merge. — Evidence: –
- [ ] 15.4 scope generation. — Evidence: –
- [ ] 15.5 forbidden paths. — Evidence: –
- [ ] 15.6 scope versioning. — Evidence: –
- [ ] 15.7 expansion request. — Evidence: –
- [ ] 15.8 unavailable handling. — Evidence: –
- [ ] 15.9 scope audit. — Evidence: –

## P16 – Context Builder

- [ ] 16.1 context sections. — Evidence: –
- [ ] 16.2 token budgeting. — Evidence: –
- [ ] 16.3 relevance. — Evidence: –
- [ ] 16.4 deduplication. — Evidence: –
- [ ] 16.5 error preservation. — Evidence: –
- [ ] 16.6 tool summary. — Evidence: –
- [ ] 16.7 current diff. — Evidence: –
- [ ] 16.8 tests. — Evidence: –
- [ ] 16.9 context telemetry. — Evidence: –

## P17 – Tool Engine

- [ ] 17.1 list/read/find/search. — Evidence: –
- [ ] 17.2 git read. — Evidence: –
- [ ] 17.3 file writes. — Evidence: –
- [ ] 17.4 patch. — Evidence: –
- [ ] 17.5 commands. — Evidence: –
- [ ] 17.6 tests. — Evidence: –
- [ ] 17.7 research request. — Evidence: –
- [ ] 17.8 scope request. — Evidence: –
- [ ] 17.9 replan request. — Evidence: –
- [ ] 17.10 checkpoint. — Evidence: –
- [ ] 17.11 complete_step. — Evidence: –
- [ ] 17.12 block_step. — Evidence: –
- [ ] 17.13 policy enforcement. — Evidence: –

## P18 – Execution Sandbox

- [ ] 18.1 rootless Podman. — Evidence: –
- [ ] 18.2 images. — Evidence: –
- [ ] 18.3 mounts. — Evidence: –
- [ ] 18.4 resource limits. — Evidence: –
- [ ] 18.5 timeouts. — Evidence: –
- [ ] 18.6 network-off default. — Evidence: –
- [ ] 18.7 allowed network mode. — Evidence: –
- [ ] 18.8 cleanup. — Evidence: –
- [ ] 18.9 abandoned container recovery. — Evidence: –

## P19 – Main Coder

- [ ] 19.1 worker loop. — Evidence: –
- [ ] 19.2 Qwen3-Coder 30B. — Evidence: –
- [ ] 19.3 32K context. — Evidence: –
- [ ] 19.4 tool protocol. — Evidence: –
- [ ] 19.5 completion. — Evidence: –
- [ ] 19.6 checkpoints. — Evidence: –
- [ ] 19.7 result schema. — Evidence: –
- [ ] 19.8 code task regression matrix. — Evidence: –

## P20 – Stagnation Detection

- [ ] 20.1 action fingerprints. — Evidence: –
- [ ] 20.2 error fingerprints. — Evidence: –
- [ ] 20.3 diff progress. — Evidence: –
- [ ] 20.4 thresholds. — Evidence: –
- [ ] 20.5 forced diagnose. — Evidence: –
- [ ] 20.6 strategy switch. — Evidence: –
- [ ] 20.7 replan. — Evidence: –
- [ ] 20.8 stop conditions. — Evidence: –

## P21 – Deterministic Verifier

- [ ] 21.1 scope. — Evidence: –
- [ ] 21.2 syntax. — Evidence: –
- [ ] 21.3 compile. — Evidence: –
- [ ] 21.4 lint. — Evidence: –
- [ ] 21.5 unit. — Evidence: –
- [ ] 21.6 integration. — Evidence: –
- [ ] 21.7 secrets. — Evidence: –
- [ ] 21.8 conflicts. — Evidence: –
- [ ] 21.9 presence evidence. — Evidence: –
- [ ] 21.10 absence evidence. — Evidence: –
- [ ] 21.11 command evidence. — Evidence: –
- [ ] 21.12 test evidence. — Evidence: –
- [ ] 21.13 diff evidence. — Evidence: –
- [ ] 21.14 report. — Evidence: –

## P22 – Heavy Review

- [ ] 22.1 review prompt. — Evidence: –
- [ ] 22.2 Qwen3.8 profile. — Evidence: –
- [ ] 22.3 structured findings. — Evidence: –
- [ ] 22.4 severity. — Evidence: –
- [ ] 22.5 major/blocker invariant. — Evidence: –
- [ ] 22.6 correction request. — Evidence: –

## P23 – Correction Pipeline

- [ ] 23.1 verifier failure correction. — Evidence: –
- [ ] 23.2 review correction. — Evidence: –
- [ ] 23.3 bounded attempts. — Evidence: –
- [ ] 23.4 evidence passed forward. — Evidence: –
- [ ] 23.5 regression rerun. — Evidence: –
- [ ] 23.6 escalation. — Evidence: –

## P24 – Replanning

- [ ] 24.1 failure package. — Evidence: –
- [ ] 24.2 Gemma replan. — Evidence: –
- [ ] 24.3 plan versioning. — Evidence: –
- [ ] 24.4 completed-step preservation. — Evidence: –
- [ ] 24.5 new dependencies. — Evidence: –
- [ ] 24.6 scope refresh. — Evidence: –

## P25 – Scheduler

- [ ] 25.1 DAG scheduler. — Evidence: –
- [ ] 25.2 ready steps. — Evidence: –
- [ ] 25.3 capabilities. — Evidence: –
- [ ] 25.4 resource leases. — Evidence: –
- [ ] 25.5 worker dispatch. — Evidence: –
- [ ] 25.6 retries. — Evidence: –
- [ ] 25.7 timeouts. — Evidence: –
- [ ] 25.8 parallel steps. — Evidence: –
- [ ] 25.9 dependency failure. — Evidence: –

## P26 – SSH/Admin Tools

- [ ] 26.1 host registry. — Evidence: –
- [ ] 26.2 SSH keys. — Evidence: –
- [ ] 26.3 command policy. — Evidence: –
- [ ] 26.4 read commands. — Evidence: –
- [ ] 26.5 mutation commands. — Evidence: –
- [ ] 26.6 audit. — Evidence: –
- [ ] 26.7 timeout. — Evidence: –
- [ ] 26.8 sandbox vs host separation. — Evidence: –

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

- [ ] 29.1 image step. — Evidence: –
- [ ] 29.2 video step. — Evidence: –
- [ ] 29.3 GPU leases. — Evidence: –
- [ ] 29.4 safe AI drain. — Evidence: –
- [ ] 29.5 model unload. — Evidence: –
- [ ] 29.6 artifacts. — Evidence: –
- [ ] 29.7 resume AI. — Evidence: –

## P30 – API

- [ ] 30.1 job API. — Evidence: –
- [ ] 30.2 controls. — Evidence: –
- [ ] 30.3 workers. — Evidence: –
- [ ] 30.4 models. — Evidence: –
- [ ] 30.5 resources. — Evidence: –
- [ ] 30.6 artifacts. — Evidence: –
- [ ] 30.7 events. — Evidence: –
- [ ] 30.8 research. — Evidence: –
- [ ] 30.9 auth. — Evidence: –

## P31 – UI

- [ ] 31.1 layout. — Evidence: –
- [ ] 31.2 dashboard. — Evidence: –
- [ ] 31.3 jobs. — Evidence: –
- [ ] 31.4 DAG. — Evidence: –
- [ ] 31.5 live events. — Evidence: –
- [ ] 31.6 diff. — Evidence: –
- [ ] 31.7 tests. — Evidence: –
- [ ] 31.8 verifier. — Evidence: –
- [ ] 31.9 review. — Evidence: –
- [ ] 31.10 sources. — Evidence: –
- [ ] 31.11 workers. — Evidence: –
- [ ] 31.12 resources. — Evidence: –
- [ ] 31.13 media. — Evidence: –
- [ ] 31.14 errors. — Evidence: –
- [ ] 31.15 controls. — Evidence: –

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

- [ ] 34.1 kill runtime during job. — Evidence: –
- [ ] 34.2 restart. — Evidence: –
- [ ] 34.3 reconstruct. — Evidence: –
- [ ] 34.4 leased worker recovery. — Evidence: –
- [ ] 34.5 model load recovery. — Evidence: –
- [ ] 34.6 workspace recovery. — Evidence: –
- [ ] 34.7 Git state recovery. — Evidence: –

## P35 – Failure Injection

- [ ] 35.1 Planner timeout — Evidence: –
- [ ] 35.2 invalid Planner JSON — Evidence: –
- [ ] 35.3 Coder timeout — Evidence: –
- [ ] 35.4 Worker offline — Evidence: –
- [ ] 35.5 Ollama unavailable — Evidence: –
- [ ] 35.6 GitLab unavailable — Evidence: –
- [ ] 35.7 DB reconnect — Evidence: –
- [ ] 35.8 process crash — Evidence: –
- [ ] 35.9 full disk warning — Evidence: –
- [ ] 35.10 scope violation — Evidence: –
- [ ] 35.11 secret found — Evidence: –
- [ ] 35.12 merge conflict — Evidence: –
- [ ] 35.13 stale base SHA — Evidence: –
- [ ] 35.14 verifier crash — Evidence: –
- [ ] 35.15 reviewer timeout — Evidence: –
- [ ] 35.16 WOL timeout — Evidence: –
- [ ] 35.17 research source unavailable — Evidence: –
- [ ] 35.18 network failure — Evidence: –
- [ ] 35.19 video preemption — Evidence: –

## P36 – Security Hardening

- [ ] 36.1 secret scan. — Evidence: –
- [ ] 36.2 permissions. — Evidence: –
- [ ] 36.3 worker auth. — Evidence: –
- [ ] 36.4 SSH restrictions. — Evidence: –
- [ ] 36.5 command policy. — Evidence: –
- [ ] 36.6 sandbox escape checks. — Evidence: –
- [ ] 36.7 network policy. — Evidence: –
- [ ] 36.8 dependency audit. — Evidence: –

## P37 – Generalization E2E

- [ ] 37.1 Python FastAPI Feature. — Evidence: –
- [ ] 37.2 SQLite Schema Migration. — Evidence: –
- [ ] 37.3 PostgreSQL Migration. — Evidence: –
- [ ] 37.4 Docker Compose Service. — Evidence: –
- [ ] 37.5 React/CSS Navigation. — Evidence: –
- [ ] 37.6 PHP Backend Form. — Evidence: –
- [ ] 37.7 TypeScript Utility. — Evidence: –
- [ ] 37.8 YAML-only Config. — Evidence: –
- [ ] 37.9 Shell/Linux Admin. — Evidence: –
- [ ] 37.10 Research-only Task. — Evidence: –
- [ ] 37.11 Image Job. — Evidence: –
- [ ] 37.12 Video Job. — Evidence: –
- [ ] 37.13 Wake-on-LAN Job. — Evidence: –
- [ ] 37.14 Worker failure/recovery. — Evidence: –
- [ ] 37.15 planner replan after failure. — Evidence: –

## P38 – Performance / Load

- [ ] 38.1 concurrent jobs. — Evidence: –
- [ ] 38.2 event volume. — Evidence: –
- [ ] 38.3 DB load. — Evidence: –
- [ ] 38.4 context build timings. — Evidence: –
- [ ] 38.5 model load/unload. — Evidence: –
- [ ] 38.6 worker wake. — Evidence: –
- [ ] 38.7 video preemption. — Evidence: –
- [ ] 38.8 memory usage. — Evidence: –

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

- [ ] 40.1 System vollständig starten. — Evidence: –
- [ ] 40.2 mehrere reale Jobs. — Evidence: –
- [ ] 40.3 24h/geeigneter Dauerbetrieb soweit Umgebung erlaubt. — Evidence: –
- [ ] 40.4 Ressourcen prüfen. — Evidence: –
- [ ] 40.5 Leaks. — Evidence: –
- [ ] 40.6 stuck leases. — Evidence: –
- [ ] 40.7 worker reconnect. — Evidence: –
- [ ] 40.8 event ordering. — Evidence: –
- [ ] 40.9 DB integrity. — Evidence: –

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

- [ ] 42.1 altes Hermclaw read-only. — Evidence: –
- [ ] 42.2 keine Daten löschen. — Evidence: –
- [ ] 42.3 neues System produktiv. — Evidence: –
- [ ] 42.4 Rollback dokumentieren. — Evidence: –
- [ ] 42.5 Monitoring. — Evidence: –
- [ ] 42.6 erst nach stabiler Laufzeit Altkomponenten stilllegen. — Evidence: –

