# RISK REGISTER

| ID | Risiko | Auswirkung | Mitigation | Status |
|---|---|---|---|---|
| R-001 | Begrenzter RAM/VRAM auf .224 | große Modelle konkurrieren | Resource Leases, sequenzielles Model Loading, Video Priority | mitigiert in P09 |
| R-002 | Lokale Modelle stagnieren | Endlosschleifen / schlechte Änderungen | 20-Turn-Steps, Fingerprints, Gemma Replan, Verifier | mitigiert in P20 |
| R-003 | Scope-Verwechslung | legitime Arbeit verloren / unerlaubte Änderungen | expliziter ScopeContract, keine Source-Inference | mitigiert in P15 |
| R-004 | Runtime-Crash | verlorener Jobstatus | PostgreSQL State, Recovery Tests | mitigiert in P34 |
| R-005 | Research ohne Nachvollziehbarkeit | falsche Entscheidungen | Source Records + Claims + Decision Links | mitigiert in P12 |
| R-006 | Video/AI GPU-Kollision | Jobfehler | Prioritäts-Leases + sichere Checkpoints | mitigiert in P09/P29 |
| R-007 | Keine LAN-Erreichbarkeit aus der Build-Umgebung (BLOCKER-001) | Live-Integration/Inventur/Deployment nicht verifizierbar | vollständige Ansible-/Skript-Automation, Fakes nur in Tests, Installationsanleitung, `[~]`-Markierung | offen (extern) |
| R-008 | pgvector CVE-2026-3172 (0.6.0–0.8.1) | Datenleck/Crash bei parallelem HNSW-Build | PGDG ≥ 0.8.2, `max_parallel_maintenance_workers=0` beim Index-Build, Versionswarnung im Health | mitigiert |
| R-009 | Pascal-Supportende (Treiber 590, CUDA 13) | GPU-Beschleunigung fällt bei Upgrades weg | Treiber 580 pinnen, Ollama-Version pinnen, GPU-Selftest vor Upgrade | offen (Betrieb) |
| R-010 | LiteLLM-Mapping von `response_format` auf Ollama `format` unklar | ungültige strukturierte Ausgaben | Contract-Test, Pydantic-Validierung + Repair (max 2), optional `format` per Passthrough | mitigiert in P08: empirisch mit LiteLLM 1.104.2 verifiziert – `json_schema` → Ollama `format`, `num_ctx`/`think`/`keep_alive` werden durchgereicht (tests/integration/test_models_litellm_proxy.py) |
| R-011 | Thinking-Abschaltung bei Gemma/Qwen versionsabhängig | leere `content`, Reasoning statt Antwort | nur `content` auswerten, Reasoning verwerfen, Repair-Versuch, ausreichendes `max_tokens` | mitigiert |
| R-012 | Qwen3.8/EmbeddingGemma 2 sehr neu, Angaben teils aus Drittquellen | falsche Tags/Größen | Tags vor Ort mit `ollama show` verifizieren; Profile konfigurierbar | offen (vor Ort) |
| R-013 | Sandbox-Limits ohne cgroup-Delegation wirkungslos | Ressourcenmissbrauch | Health prüft `podman info`; Deployment konfiguriert systemd-Delegation | mitigiert in P18: Preflight prüft rootless + cgroups-v2-Delegation (`EngineInfo.warnings`); systemd `Delegate=yes` in P32; Live-Test `test_live_222_rootless_with_delegated_cgroups_v2` |
| R-014 | Secrets in Prompts/Logs | Leak | Redactor, Secret-Scanner im Verifier, systemd credentials | mitigiert in P21: Verifier-Secret-Scan auf hinzugefügten Diff-Zeilen (Redaction-Muster + Entropie + Private-Key-Header), immer blockierend |
| R-015 | Workspace-Sync `.225`↔`.222` inkonsistent | falsche Testergebnisse | Content-Hash-Manifest, Diff nur auf `.225` berechnet | mitigiert in P18 |
| R-016 | Modellqualität lokaler Planner (Gemma) begrenzt | schwache Pläne | Schema-Validierung, DAG-Checks, Replanning, Stagnation, Heavy Review | akzeptiert |
| R-017 | Sandbox kann `.git/config` im Workspace verändern (Filter/Hooks/Driver), Runtime-Git auf `.225` würde das ausführen | Code-Ausführung auf dem Orchestrator | GitOps: gehärtete Git-Umgebung (`GIT_CONFIG_NOSYSTEM`, `-c`-Overrides, kein Repo-lokales Config-Ausführen), Integritäts-Allowlist → `WORKSPACE_TAMPERED`; Sync von `.222` schließt `.git/` aus | mitigiert in P06 |
| R-018 | Replay-Cache der Worker-HMAC-Prüfung ist pro Prozess im Speicher | Replay nach Neustart innerhalb des Zeitfensters | kurzes Zeitfenster (Timestamp-Skew), Body-Hash, Neustart leert Cache – akzeptiert für LAN | akzeptiert |
| R-019 | LiteLLM-Standard-Retries/Cooldowns verdecken Ausfälle und verdreifachen Lastspitzen | lange Hänger, doppelte Modellläufe | generierte Config: `num_retries: 0`, `disable_cooldowns`, Fallback nur über Hermclaw-Gateway; Health nur über liveliness/readiness | mitigiert in P08 |
| R-020 | LiteLLM-Proxy-Abhängigkeiten kollidieren mit Orchestrator-Venv (FastAPI-Pins) | kaputte API nach Update | eigenes venv `/opt/hermclaw/litellm-venv` (`litellm[proxy]==1.104.2`), eigener systemd-Dienst | mitigiert in P08/P32 |
| R-021 | Docker-CLI injiziert Proxy-Variablen aus `~/.docker/config.json` in Sandbox-Container | Netzwerk trotz `network=none` erreichbar | Podman ist Standard auf `.222`; Docker-Adapter dokumentiert, `~/.docker/config.json` ohne `proxies` betreiben | akzeptiert |
