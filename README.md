# Claude-Hermclaw / Hermclaw Next

Greenfield-Neuaufbau von Hermclaw als universelles Multi-Agent-System.

Dieses Repository ist die neue Implementierungsbasis. Die bisherige Hermclaw-/Noob2Claw-Runtime dient nur als Read-only-Referenz für gelernte Fehler, Anforderungen und gewünschtes Verhalten.

## Stand: Release Candidate 0.1.0rc83

- **Installation (pro Host, Konfiguration, Befehle, Modelle):** `docs/operations/INSTALLATION.md`
- **Abschlussbericht:** `docs/operations/FINAL_BUILD_REPORT.md` · Status: `STATUS.md` · Änderungen: `CHANGELOG.md`
- **Betrieb:** `docs/runbooks/` (Admin, Upgrade, Disaster Recovery, Cutover), bekannte Einschränkungen:
  `docs/operations/known-limitations.md`
- Lokal vollständig getestet (`docs/testing/test-matrix-results.md`); die Live-Abnahme auf den Ziel-Hosts erfolgt mit
  `scripts/ops/live-acceptance.sh` (INSTALLATION.md Kapitel 12).

## Verbindliche Startdokumente

- `docs/Hermclaw_Next_Vollstaendiger_Bauplan_v2.md` – vollständige Zielarchitektur und Build-Phasen P00–P42
- `docs/Claude_Code_Hermclaw_Next_FULL_BUILD_PROMPT_v2.md` – Master-Prompt für Claude Code

## Kernprinzipien

- Gemma als Planner/Replanner
- Qwen3-Coder 30B als Implementation Worker
- Qwen3.8 27B als Heavy Reviewer
- Qwen3 8B als Fast Router
- EmbeddingGemma + pgvector für semantisches Retrieval
- PostgreSQL als persistente Source of Truth
- expliziter Scope für jeden mutierenden Step
- deterministic Verifier
- vollständige Research-Quellen
- Stagnation Detection
- Runtime-kontrollierte Git-Operationen
- Worker-/GPU-/Wake-on-LAN-Steuerung
- keine benchmark-spezifische Produktionslogik

## Build Control

Claude Code muss diese Dateien laufend pflegen:

- `BUILD_PLAN.md`
- `STATUS.md`
- `BUGS.md`
- `DECISIONS.md`
- `RESEARCH_INDEX.md`
- `TEST_MATRIX.md`
- `RISK_REGISTER.md`
- `CHANGELOG.md`

Der vollständige Build soll P00 bis P42 ohne künstliche Milestone-Stopps durchlaufen. Nicht-blockierende Bugs werden erfasst und spätestens in der finalen Hardening-/Bug-Bash-Phase abgearbeitet.
