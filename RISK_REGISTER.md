# RISK REGISTER

| ID | Risiko | Auswirkung | Mitigation | Status |
|---|---|---|---|---|
| R-001 | Begrenzter RAM/VRAM auf .224 | große Modelle konkurrieren | Resource Leases, sequenzielles Model Loading, Video Priority | offen |
| R-002 | Lokale Modelle stagnieren | Endlosschleifen / schlechte Änderungen | 10–20 Turn Steps, Fingerprints, Gemma Replan, Verifier | offen |
| R-003 | Scope-Verwechslung | legitime Arbeit verloren / unerlaubte Änderungen | expliziter ScopeContract, keine Source-Inference | offen |
| R-004 | Runtime-Crash | verlorener Jobstatus | PostgreSQL State, Recovery Tests | offen |
| R-005 | Research ohne Nachvollziehbarkeit | falsche Entscheidungen | Source Records + Claims + Decision Links | offen |
| R-006 | Video/AI GPU-Kollision | Jobfehler | Prioritäts-Leases + sichere Checkpoints | offen |
