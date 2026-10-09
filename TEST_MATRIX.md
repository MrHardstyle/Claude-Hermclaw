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
