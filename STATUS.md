# STATUS

## Aktueller Zustand
- Projekt: Hermclaw Next · Modus: Greenfield Build · Branch: `claude/hermclaw-next-build`
- Aktuelle Phase: P06–P31 (Komponenten, parallel in Agenten-Wellen)
- Abgeschlossen: P00 Research/Inventur · P02 Backend Skeleton · P03 Persistence · P04 Event Store · P05 State Machines · P13 Contracts · P30 API (Kern) · P25 Scheduler (25.1/25.2/25.6–25.9; 25.3–25.5 folgen mit Worker-/Ressourcen-Integration)
- In Arbeit: Welle 1 (P01, P06–P12, P18), Welle 2 (P14/P24, P15–P17, P20–P22), P31 UI
- Danach: Welle 3 (P26–P28, P33), Integration (P19 Coder-Loop, P23 Correction, P29 Media, P32 Deployment, Runtime-Wiring), P34–P42, Installationsanleitung
- Unterbrechung: Nutzungslimit 2026-10-08 ~15:00–17:10 UTC; Agenten-Wellen ab 17:15 UTC neu gestartet
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
