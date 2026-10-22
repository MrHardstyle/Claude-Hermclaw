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
| R-025 | Model tag re-check (install) | docs/research/20261013-025-model-tag-recheck.md | `qwen3.8:27b` bestätigt; Embedding-Fallback `embeddinggemma:300m` (768 dim) |
| R-026 | GPU-Stack `.224` (Treiber, Ollama, Pascal) | docs/research/20261014-026-gpu-ollama-stack.md | Ollama v0.40.4 läuft auf CC 6.1 nur mit cuda_v12 + Treiber ≥ 570; Debian 13 hat nur 550 und NVIDIAs debian13-Repo kein 580 → R580-`.run` (DKMS, proprietär); `embeddinggemma-2` ist MLX-only → `embeddinggemma:300m`; Ollama-Env (FA, KV q8_0, MAX_LOADED_MODELS 2) |
| R-027 | Orchestrator-Stack `.225` (PostgreSQL 17 + pgvector aus PGDG, LiteLLM-Pin, SearXNG, Python 3.13) | docs/research/20261014-027-orchestrator-stack.md | pgvector 0.8.7 (neue CVE-2026-103484, IVFFlat) → D-012; LiteLLM 1.104.2 ohne Advisory, Installation hash-gepinnt + `prisma==0.15.0` (sonst 500 statt 401, BUG-097); SearXNG per Digest gepinnt, `GRANIAN_HOST=0.0.0.0`, kein `secret_key` in der Datei (BUG-093); Python 3.13 kompatibel, OpenAPI-Phrasen normalisiert; tree-sitter-Grammatiken werden zur Laufzeit geladen → Vorab-Download (BUG-109); gegengeprüft (64 Aussagen) |
| R-028 | Web-UI, Exec-Worker, Hosts (nginx SSE/TLS, Node 22, rootless Podman + cgroup-Delegation, WoL) | docs/research/20261014-028-webui-workers-hosts.md | nginx ≥ 1.26.3-3+deb13u9 aus trixie, SSE-Location mit HTTP/1.1, `proxy_buffering off`, 1h-Timeouts, HTTP/2, Log ohne Query (Token); Podman-Delegation-Drop-in; WoL per systemd `.link` (Gegenprüfung ausstehend) |
| R-029 | Deployment-Praktiken (ansible-core, versionierte venv-Releases, systemd-Härtung, Credentials, uvicorn hinter nginx) | docs/research/20261014-029-deployment-practices.md | Steuerrechner `ansible-core==2.21.5` (Bereich `>=2.21.5,<2.22`, Python 3.12–3.14), nicht das trixie-Paket; Releases `/opt/hermclaw/venv-<version>` + relativer Symlink, 3 Releases behalten, Wheelhouse auf Debian 13/CPython 3.13 bauen und offline (`--no-index`) installieren; Idempotenz: zweiter Lauf und `--check` `changed=0`; systemd 257: `LoadCredentialEncrypted=`; gegengeprüft (62 Aussagen) |
| R-030 | DB-Tools für Projekt-Datenbanken (P27) | docs/research/20261014-030-dbtools.md | READ-ONLY-Transaktion + Lexer + Funktions-Denylist + statement_timeout; SQLite `mode=ro` + Authorizer; Migrationen nur in Wegwerf-Sandbox-DBs; pg_dump/pg_restore-Restore-Test |
| R-031 | Abhängigkeits-Installation über einen Registry-only-Egress-Proxy (D-014) | docs/research/20261016-031-dependency-egress.md | internes Podman-Netz (`--internal --disable-dns`, keine Route), Proxy-Container als einziger Teilnehmer beider Netze mit fester IP und `--dns` (kein aardvark-dns auf .222); CONNECT-Allowlist pypi.org/files.pythonhosted.org/registry.npmjs.org, nur öffentliche Zieladressen; pip `--target /deps/python` + `PYTHONPATH`, npm `--ignore-scripts --no-save --package-lock=false --cache /tmp/npm-cache` in eingehängtes `node_modules`; live auf .222 verifiziert |
| R-032 | Coder-Modell und Inferenz-Runtime auf dem 8-GB-Modellhost (D-015) | docs/research/20261017-032-model-runtime.md | llama.cpp b11361 (CUDA sm_61) mit `--n-cpu-moe` ~2x Ollama (Prefill 283 vs 154 tok/s, Decode 30 vs 13 tok/s für qwen3.6:35b); qwen3.6:35b (SWE-bench Verified 73,4) als ein residentes Modell für alle Chat-Rollen; Grammatik-erzwungene JSON-Ausgaben über LiteLLM `openai/` |
| R-033 | Planer-Modell-Kandidaten für den 8-GB-Modellhost | docs/research/20261018-033-planner-candidates.md | Turnier-Sieger qwen3.8-27b (99/120 vs. 81 für qwen3.6, G1 29/40 vs. Agents-A1 ≤23, qwen3.6 ungültig); auf dem Papier qwen3.8:27b (IFBench 79,5, dicht, 4,7 tok/s) und gemma4:26b (IFBench 77,25 vs. 63,71 für qwen3.6-35B-A3B, unabhängig) vor dem aktuellen qwen3.6:35b; Agents-A1 35B-A3B als Ersatz ohne Modellwechsel; A/B-Prüfstand mit Wegwerf-DB nach der Batterie |
| R-034 | llama.cpp-Tuning auf dem 8-GB-Modellhost (Paket C) | docs/research/20261021-034-llamacpp-tuning.md | b11696: `-ub 1024` für qwen3.6 bei `--n-cpu-moe 30` bringt +50 % Prefill bei 32K ohne Decode-Verlust (D-022); `-ub 2048` passt nur mit `--n-cpu-moe 32` und kostet Decode; MTP-Schichten in beiden GGUFs, beim Planer Decode +28–52 % bei Prefill −11 % (Betreiberentscheidung); n-gram und `--moe-cache-mib` ohne Gewinn |
| R-035 | Modellfamilien als Coder-Kandidaten (Paket B) | docs/research/20261022-035-model-families.md | Screening auf b11696 (fit, `-ub 1024`): Gemma 4 26B-A4B 21,1 tok/s Decode / 387 Prefill, gpt-oss-20b (ggml-org MXFP4, `reasoning_effort: low`) 25,1 / 623, GLM-4.7-Flash (Gating sigmoid) 14,4 / 86; alle drei 0 von 20 Schemafehlern, kein Markup, Cache im D-021-Aufbau, keine Schleifen; Lücke: `reasoning_effort` bzw. `chat_template_kwargs` je Profil (Teil 2, rc69); Vorturnier A1/A3/A5: Gemma 4 26B 3/3 korrekt mit 29 statt 37 Aufrufen vor `coder-main` 3/3, GLM 1/3, gpt-oss 0/3 (Denken trotz `low` zu lang) |
