# STATUS

## Aktueller Zustand
- Projekt: Hermclaw Next · Modus: Greenfield Build · Branch: `claude/hermclaw-next-build`
- Aktuelle Phase: P01–P05 (Fundament)
- Letzter abgeschlossener Schritt: P00 Research/Inventur (24 Research-Berichte, Inventur-Automation, Systemdiagramm)
- Nächster Schritt: Backend-Fundament (pyproject, Core, Contracts, Persistence, Event Store, State Machines)
- Blocker: BLOCKER-001 (Zielhosts nicht erreichbar aus der Build-Umgebung) – alle unabhängigen Arbeiten laufen weiter
- Release-Status: NOT READY

## Deploymentstatus pro Host
| Host | Rolle | Status |
|---|---|---|
| 192.168.178.223 | WebUI / Nginx | nicht deployt (BLOCKER-001) |
| 192.168.178.222 | Coding / Execution Worker | nicht deployt (BLOCKER-001) |
| 192.168.178.224 | Model / Media Worker | nicht deployt (BLOCKER-001) |
| 192.168.178.225 | Orchestrator | nicht deployt (BLOCKER-001) |
| 192.168.178.226 | GitLab | nicht inventarisiert (BLOCKER-001) |
| 192.168.178.60 | Terra / Unraid Backup | nicht inventarisiert (BLOCKER-001) |

## Modellrollen (Ollama-Tags laut Research)
- Fast Router: `qwen3:8b` (16K) · Planner: `gemma4:26b` (32K) · Planner-Fallback: `gemma4:12b`
- Main Coder: `qwen3-coder:30b` (32K) · Heavy Reviewer: `qwen3.8:27b` (24–32K) · Embeddings: `embeddinggemma-2:740m` (768 dim)

## Aktive Bugs
keine

## Letzte Testresultate
- Inventurskript lokal: gültiges JSON (`scripts/inventory/hermclaw-inventory.sh --role buildenv | python3 -m json.tool`)
