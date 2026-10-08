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

- [ ] Python FastAPI Feature
- [ ] SQLite Migration
- [ ] PostgreSQL Migration
- [ ] Docker Compose Service
- [ ] React/CSS Navigation
- [ ] PHP Backend Form
- [ ] TypeScript Utility
- [ ] YAML-only Config
- [ ] Linux Admin
- [ ] Research-only Task
- [ ] Image Job
- [ ] Video Job
- [ ] Wake-on-LAN Job
- [ ] Worker Failure / Recovery
- [ ] Planner Replan after Failure

## Komponenten-Testläufe (Build-Umgebung)

| Phase | Komponente | Befehl | Ergebnis | Live-Test |
|---|---|---|---|---|
| P25 | Scheduler | `pytest tests/integration/test_scheduler.py` | 17 passed | – |
| P06 | Git Engine | `pytest tests/*/test_gitops_*.py` | 185 passed, 1 skipped (live) | `pytest -m live tests/integration/test_gitops_live.py` (GitLab .226) |
| P07 | Worker-Protokoll/Daemons | `pytest tests/*/test_workers_*.py` | 134 passed, 1 skipped (Podman-Sandbox) | `pytest -m live tests/integration/test_workers_live.py` (HERMCLAW_LIVE_EXEC_TOKEN_FILE, HERMCLAW_LIVE_MODEL_TOKEN_FILE, HERMCLAW_LIVE_DATABASE_URL) |
| P14/P24 | Planner/Replanner | `pytest tests/*/test_planner_*.py` | 113 passed | Live-Gemma (14.2/24.2) über LiteLLM `planner-gemma` |
| P15 | Scope Engine | `pytest tests/*/test_scope_*.py` | 198 passed | – |
