# STATUS

## Aktueller Zustand (2026-10-13)
- Projekt: Hermclaw Next · Modus: Greenfield Build · Branch: `claude/hermclaw-next-build`
- Abgeschlossen: Komponenten P02–P25, P29, P30 (Kern) · Runtime-Integration (Composition Root `hermclaw/runtime/services.py`, Prozess `hermclaw scheduler`) · P34 Recovery · P35 Failure · P36 Security (ohne 36.4) · P37 E2E (ohne 37.3/37.4) · P38 Last
- In Arbeit: Build-Queue (P01 Bootstrap, P31 UI, P26 SSH, P27 DB, P28 Container/Proxy, P33 Backup) · P39 Shared-Fixes (Schema/BUG-030, Scope, Workspace-Retention)
- Danach: Step-Handler ssh/database/docker/deploy, 37.3/37.4, 36.4, P32 Deployment, P39 Bug-Bash + komplette Testmatrix, P40–P42, Installationsanleitung, Final Build Report
- Blocker: BLOCKER-001 (Zielhosts aus der Build-Umgebung nicht erreichbar) – Live-Schritte `[~]`
- Release-Status: NOT READY (RC erst nach P39–P41)

## Fortschritt BUILD_PLAN (automatisch: `python scripts/dev/plan_progress.py --markdown`)
| Phase | erledigt | erledigt, live offen (BLOCKER-001) | offen |
|---|---|---|---|
| P00 – Initiale Research- und Inventurphase | 6 | 14 | 0 |
| P01 – Clean System Bootstrap | 0 | 0 | 15 |
| P02 – Backend Skeleton | 9 | 1 | 0 |
| P03 – Persistence | 9 | 0 | 0 |
| P04 – Event Store | 7 | 0 | 1 |
| P05 – State Machines | 6 | 0 | 0 |
| P06 – Git Engine | 11 | 1 | 0 |
| P07 – Worker Protocol | 7 | 2 | 0 |
| P08 – Model Gateway | 3 | 8 | 0 |
| P09 – Resource Manager | 9 | 0 | 0 |
| P10 – Wake-on-LAN | 2 | 7 | 0 |
| P11 – Repository Intelligence | 11 | 1 | 0 |
| P12 – Research Engine | 10 | 3 | 0 |
| P13 – Contracts | 12 | 0 | 0 |
| P14 – Gemma Planner | 9 | 1 | 0 |
| P15 – Scope Engine | 9 | 0 | 0 |
| P16 – Context Builder | 9 | 0 | 0 |
| P17 – Tool Engine | 13 | 0 | 0 |
| P18 – Execution Sandbox | 8 | 1 | 0 |
| P19 – Main Coder | 6 | 2 | 0 |
| P20 – Stagnation Detection | 8 | 0 | 0 |
| P21 – Deterministic Verifier | 14 | 0 | 0 |
| P22 – Heavy Review | 5 | 1 | 0 |
| P23 – Correction Pipeline | 6 | 0 | 0 |
| P24 – Replanning | 5 | 1 | 0 |
| P25 – Scheduler | 9 | 0 | 0 |
| P26 – SSH/Admin Tools | 0 | 0 | 8 |
| P27 – DB Tools | 0 | 0 | 7 |
| P28 – Container/Proxy Tools | 0 | 0 | 6 |
| P29 – Media | 7 | 0 | 0 |
| P30 – API | 9 | 0 | 0 |
| P31 – UI | 0 | 0 | 15 |
| P32 – Deployment | 0 | 0 | 10 |
| P33 – Backup/Restore | 0 | 0 | 6 |
| P34 – Recovery | 7 | 0 | 0 |
| P35 – Failure Injection | 19 | 0 | 0 |
| P36 – Security Hardening | 7 | 0 | 1 |
| P37 – Generalization E2E | 13 | 0 | 2 |
| P38 – Performance / Load | 8 | 0 | 0 |
| P39 – FULL BUG BASH / HARDENING | 0 | 0 | 13 |
| P40 – SOAK TEST | 0 | 0 | 9 |
| P41 – RELEASE CANDIDATE | 0 | 0 | 9 |
| P42 – CUTOVER PLAN | 0 | 0 | 6 |
| **Summe** | **273** | **43** | **108** |

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
- Main Coder: `qwen3-coder:30b` (32K) · Heavy Reviewer: `qwen3.8:27b` (24–32K) · Embeddings: `embeddinggemma-2:740m` (768 dim; Fallback `embeddinggemma:300m`, R-025)

## Aktive Bugs
keine offenen P0/P1; P3-Nacharbeiten in P39 (BUG-030 u. a.), Register: BUGS.md (BUG-001 … BUG-042)

## Letzte Testresultate (2026-10-13, Build-Host, jeweils einzeln ausgeführt)
- Runtime/Integration: test_runtime_services (3), test_runtime_dispatch (2), test_research_step (3), test_coder_handler (9), test_runtime_driver (9) – grün
- Recovery P34: tests/failure/test_recovery_process_kill.py (2, echter SIGKILL des Scheduler-Prozesses) – grün
- Failure P35: tests/failure/test_runtime_failures.py (4) + Komponenten-Failure-Suites – grün
- Security P36: tests/security/test_security_checks.py (4), Dependency-Audit (pip-audit, 0 Befunde) – grün
- E2E P37: tests/e2e/test_e2e_code_tasks.py (8), tests/e2e/test_e2e_platform.py (5) – grün
- Last P38: tests/load/test_load.py (8) – Baseline docs/operations/performance-baseline.md
