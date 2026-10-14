# TEST MATRIX

## Ebenen

- Unit
- Contract
- Integration
- Failure Injection
- Recovery
- Concurrency
- Security
- UI / Playwright
- Generalization E2E
- Performance / Load
- Soak

## Pflicht-Generalization-Fixtures

- [x] Python FastAPI Feature (37.1, `tests/e2e/`)
- [x] SQLite Migration (37.2, `tests/e2e/`)
- [x] PostgreSQL Migration (37.3, `tests/e2e/`)
- [x] Docker Compose Service (37.4, `tests/e2e/`)
- [x] React/CSS Navigation (37.5, `tests/e2e/`)
- [x] PHP Backend Form (37.6, `tests/e2e/`)
- [x] TypeScript Utility (37.7, `tests/e2e/`)
- [x] YAML-only Config (37.8, `tests/e2e/`)
- [x] Linux Admin (37.9, `tests/e2e/`)
- [x] Research-only Task (37.10, `tests/e2e/`)
- [x] Image Job (37.11, `tests/e2e/`)
- [x] Video Job (37.12, `tests/e2e/`)
- [x] Wake-on-LAN Job (37.13, `tests/e2e/`)
- [x] Worker Failure / Recovery (37.14, `tests/e2e/`)
- [x] Planner Replan after Failure (37.15, `tests/e2e/`)

## Komponenten-Testläufe (Build-Umgebung)

| Phase | Komponente | Befehl | Ergebnis | Live-Test |
|---|---|---|---|---|
| P25 | Scheduler | `pytest tests/integration/test_scheduler.py` | 17 passed | – |
| P06 | Git Engine | `pytest tests/*/test_gitops_*.py` | 185 passed, 1 skipped (live) | `pytest -m live tests/integration/test_gitops_live.py` (GitLab .226) |
| P07 | Worker-Protokoll/Daemons | `pytest tests/*/test_workers_*.py` | 134 passed, 1 skipped (Podman-Sandbox) | `pytest -m live tests/integration/test_workers_live.py` (HERMCLAW_LIVE_EXEC_TOKEN_FILE, HERMCLAW_LIVE_MODEL_TOKEN_FILE, HERMCLAW_LIVE_DATABASE_URL) |
| P14/P24 | Planner/Replanner | `pytest tests/*/test_planner_*.py` | 113 passed | Live-Gemma (14.2/24.2) über LiteLLM `planner-gemma` |
| P15 | Scope Engine | `pytest tests/*/test_scope_*.py` | 198 passed | – |
| P08 | Model Gateway | `HERMCLAW_TEST_LITELLM_BIN=<litellm-venv>/bin/litellm pytest tests/*/test_models_*.py` | 129 passed (inkl. 12 empirische LiteLLM-Proxy-Tests), 6 live deselektiert | `pytest -m live tests/integration/test_models_live.py` gegen LiteLLM `.225:4000` + Ollama `.224` |
| P16 | Context Builder | `pytest tests/*/test_context_builder_*.py` | 79 passed | – |
| P17 | Tool Engine | `pytest tests/*/test_tools_*.py` | 151 passed | Remote-Sandbox `.222` über Integration (P18/P19) |
| P30+ | Runtime Driver | `pytest tests/integration/test_runtime_driver.py` | 9 passed | – |
| P18 | Execution Sandbox | `pytest tests/*/test_sandbox_*.py` | 117 passed, 1 live deselektiert | `pytest -m live -k test_live_222_rootless_with_delegated_cgroups_v2` auf `.222` |
| P21 | Deterministic Verifier | `pytest tests/*/test_verifier_*.py` | 124 passed (echte pytest/go/cargo/tsc/php/node/bash) | – |
| P19/P23 | Coder-Loop + Implement-Handler | `pytest tests/integration/test_coder_loop.py tests/integration/test_coder_handler.py` | 12 passed | Live-Coder über LiteLLM `coder-main` |
| – | Operator-CLI | `pytest tests/integration/test_cli.py` | 3 passed | – |
| P20 | Stagnation Detection | `pytest tests/*/test_stagnation_*.py` | 145 passed | – |
| P22 | Heavy Review | `pytest tests/*/test_review_*.py` | 112 passed | Live-Review über LiteLLM `heavy-review` (Qwen3.8 27B) |
| P09 | Resource Manager | `pytest tests/*/test_resources_*.py` | 55 passed (Advisory Locks, Preemption, Recovery) | – |
| P29 | Media Worker | `pytest tests/integration/test_media_worker.py` | 4 passed (echtes ffmpeg/ffprobe) | NVENC + ComfyUI auf `.224` |
| P18/D-005 | Remote-Executor | `pytest tests/integration/test_tools_remote_executor.py` | 5 passed | gegen `.222` |
| P11 | Repository Intelligence | `pytest tests/*/test_repo_intelligence_*.py` | 91 passed, 1 live deselektiert (pgvector, ripgrep, tree-sitter) | – |
| P12 | Research Engine | `pytest tests/*/test_research_*.py` | 143 passed, 2 live deselektiert | SearXNG auf `.225:8888` |
| P01 | Clean Bootstrap (Ansible-Rollen, systemd-Units, Skripte) | `pytest tests/integration/test_bootstrap_{infra,roles,converge,nginx,postgresql,tooling}.py` (einzeln, ≈13 min; braucht podman, PostgreSQL-Server-Binaries + runuser, systemd-analyze, shellcheck, Images nginx:1.28 + python:3.12-slim) | 92 passed (inkl. ansible-lint production profile, echtes nginx -t + SSE in Podman, Wegwerf-PostgreSQL-Cluster mit gerenderter Konfiguration, Converge im Container) | Playbooks gegen `.222–.226` (BLOCKER-001) |
| P27 | Database Tools | `pytest tests/*/test_dbtools_*.py` (+ `tests/unit/test_sandbox_runtime_env.py`) | 193 passed (echtes PostgreSQL, pg_dump/pg_restore, Alembic in Wegwerf-DB) | Zieldatenbanken aus `config/databases.yaml` |
| P41/BLOCKER-001 | Live-Abnahme-Skript `scripts/ops/live-acceptance.sh` | `pytest tests/integration/test_live_acceptance_script.py` | 3 passed (`--prepare-db` gegen echtes PostgreSQL: Nicht-Superuser-Rolle legt Test-DBs aus dem pgvector-Template an, alle Migrationen; `--cleanup-db`; jede Live-Testdatei ist einer Suite zugeordnet) | `sudo scripts/ops/live-acceptance.sh` auf `.225` und `.222` |
| P33 | Backup/Restore | `pytest tests/unit/test_backup_units.py tests/integration/test_backup_{restore,rsync,cli,privileges}.py tests/failure/test_backup_failures.py` (einzeln) | 84 passed (echtes pg_dump/pg_restore, rsync über einen lokalen sshd mit rrsync-Forced-Command, Restore-Test in Wegwerf-DB als Nicht-Superuser mit pgvector-Template, Manifest-Signatur, Retention, Locks, Fehlerfälle) | `pytest -m live tests/integration/test_backup_live.py` gegen Terra `.60` |
| P28 | Container/Proxy Tools | `pytest tests/*/test_infratools_*.py` (einzeln) | 239 passed, 3 live deselektiert (echtes Podman 4.9 + podman-compose 1.0.6, Docker Compose v5.6 `config`, nginx 1.24 `-t`, PostgreSQL für Rollback-Artefakte und Deployment-Claims) | `pytest -m live tests/integration/test_infratools_live.py` gegen `.222`/`.223` |
| P36.8/P39 | Abhängigkeits-Audit | `scripts/dev/dependency-audit.sh .venv <litellm-venv>` | 2026-10-14: Runtime 108 Pakete, LiteLLM-venv 117 Pakete (1.104.2 + prisma 0.15.0): keine bekannten Schwachstellen (pip-audit); UI `npm audit`: 0 | auf `.225` mit `/opt/litellm/venv` wiederholen |
| P31/P39 | UI statisch + Unit | `cd ui && npm run typecheck && npm run lint && npm run test:unit && npm run build` | 2026-10-14: tsc strict ok, ESLint 0 Warnungen, 48 Vitest-Tests, Build ok | – |
| P26 | SSH/Admin Tools | `pytest tests/*/test_sshtools_*.py` | 200 passed, 4 live deselektiert (echter sshd mit Forced-Command-Gate, Policy-Klassifikation inkl. Bypass-Regressionen BUG-076/077, Freigaben, Schlüssel) | `pytest -m live tests/integration/test_sshtools_live.py` (über `live-acceptance.sh`) |
| P31 | Web UI | `cd ui && npm run test:unit`; `pytest tests/e2e/test_ui_playwright.py` (Playwright gegen die echte API) | 48 Vitest; Playwright: 10 Spezifikationen mit 36 Tests (`ui/tests/e2e/`), über 3 pytest-Läufe | Browser gegen `.223` |
| P30 | API | `pytest tests/*/test_api*.py` | 22 passed (Routen eindeutig, Metriken, Registrierung, Scopes) | – |
