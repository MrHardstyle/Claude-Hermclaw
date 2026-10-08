# RESEARCH INDEX

Jede externe Integration erhält vor Umsetzung einen Research-Bericht unter `docs/research/`.
Pflichtfelder: Question · Why needed · Sources · Source date/version · Relevant facts · Compatibility · Rejected alternatives · Decision fixed by architecture · Implementation consequences · Open risks.

| ID | Thema | Datei | Kernergebnis |
|---|---|---|---|
| R-001 | Debian 13 trixie | docs/research/20261008-001-debian-13.md | Python 3.13, PG 17, Nginx 1.26, Podman 5.4.2; Debian-pgvector 0.8.0 verwundbar |
| R-002 | FastAPI/Pydantic/SQLAlchemy/SSE | docs/research/20261008-002-python-fastapi.md | native SSE `fastapi.sse` (≥0.135) |
| R-003 | PostgreSQL + pgvector | docs/research/20261008-003-postgresql-pgvector.md | PG17 + pgvector ≥0.8.2 (CVE-2026-3172), HNSW cosine, 768 dim |
| R-004 | Alembic async | docs/research/20261008-004-alembic.md | Cookbook-Muster `run_sync` |
| R-005 | rootless Podman | docs/research/20261008-005-podman-rootless.md | `--network=none --read-only --userns=keep-id` + Limits |
| R-006 | LiteLLM | docs/research/20261008-006-litellm.md | `ollama_chat/`, Health-Endpoints, json_schema-Mapping empirisch prüfen |
| R-007 | Ollama | docs/research/20261008-007-ollama.md | `format`, `think`, `/api/ps`, `keep_alive:0`, `num_ctx` explizit |
| R-008 | Gemma 4 26B A4B | docs/research/20261008-008-gemma4-26b-a4b.md | `gemma4:26b`, MoE 4B aktiv, 256K nativ |
| R-009 | Gemma 4 12B | docs/research/20261008-009-gemma4-12b.md | existiert seit 2026-06-03, Fallback |
| R-010 | Qwen3 8B | docs/research/20261008-010-qwen3-8b.md | `qwen3:8b`, hybrides Thinking |
| R-011 | Qwen3-Coder 30B | docs/research/20261008-011-qwen3-coder-30b.md | `qwen3-coder:30b`, MoE, non-thinking |
| R-012 | Qwen3.8 27B | docs/research/20261008-012-qwen3.8-27b.md | `qwen3.8:27b`, dicht, Ollama ≥0.32.12 |
| R-013 | EmbeddingGemma 2 | docs/research/20261008-013-embeddinggemma-2.md | `embeddinggemma-2:740m`, 768 dim, 8K ctx |
| R-014 | Nginx SSE | docs/research/20261008-014-nginx-sse.md | `proxy_buffering off`, HTTP/1.1, Heartbeats |
| R-015 | GitLab API | docs/research/20261008-015-gitlab-api.md | MR-/Protected-Branch-API |
| R-016 | Wake-on-LAN | docs/research/20261008-016-wake-on-lan.md | Magic Packet 102 Byte, `ethtool wol g` |
| R-017 | NVIDIA GTX 1080 | docs/research/20261008-017-nvidia-gtx1080.md | Treiber-Branch 580 (letzter für Pascal), CUDA 12 |
| R-018 | React/Vite | docs/research/20261008-018-react-vite.md | Vite 8, Node ≥20.19/22.12 |
| R-019 | Playwright | docs/research/20261008-019-playwright.md | echte SSE-Tests gegen lokales Backend |
| R-020 | systemd credentials | docs/research/20261008-020-systemd-credentials.md | `LoadCredentialEncrypted=` |
| R-021 | SSH-Policies | docs/research/20261008-021-ssh-policies.md | `restrict,from=,command=` + Gate |
| R-022 | Backup/Restore | docs/research/20261008-022-backup-restore.md | `pg_dump -Fc` + echter Restore-Test |
| R-023 | Web-Research-API | docs/research/20261008-023-web-research-api.md | SearXNG JSON (selbst gehostet) |
| R-024 | Media-Pipeline | docs/research/20261008-024-media-pipeline.md | ffmpeg NVENC (H.264/HEVC), ComfyUI-API |
