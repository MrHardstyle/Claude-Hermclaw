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
| R-010 | LiteLLM-Mapping von `response_format` auf Ollama `format` unklar | ungültige strukturierte Ausgaben | Contract-Test, Pydantic-Validierung + Repair (max 2), optional `format` per Passthrough | mitigiert in P08 |
| R-011 | Thinking-Abschaltung bei Gemma/Qwen versionsabhängig | leere `content`, Reasoning statt Antwort | nur `content` auswerten, Reasoning verwerfen, Repair-Versuch, ausreichendes `max_tokens` | mitigiert |
| R-012 | Qwen3.8/EmbeddingGemma 2 sehr neu, Angaben teils aus Drittquellen | falsche Tags/Größen | Tags vor Ort mit `ollama show` verifizieren; Profile konfigurierbar | offen (vor Ort) |
| R-013 | Sandbox-Limits ohne cgroup-Delegation wirkungslos | Ressourcenmissbrauch | Health prüft `podman info`; Deployment konfiguriert systemd-Delegation | mitigiert in P18/P32 |
| R-014 | Secrets in Prompts/Logs | Leak | Redactor, Secret-Scanner im Verifier, systemd credentials | mitigiert in P21/P36 |
| R-015 | Workspace-Sync `.225`↔`.222` inkonsistent | falsche Testergebnisse | Content-Hash-Manifest, Diff nur auf `.225` berechnet | mitigiert in P18 |
| R-016 | Modellqualität lokaler Planner (Gemma) begrenzt | schwache Pläne | Schema-Validierung, DAG-Checks, Replanning, Stagnation, Heavy Review | akzeptiert |
