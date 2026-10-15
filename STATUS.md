# STATUS

## Aktueller Zustand (2026-10-15)
- Projekt: Hermclaw Next · Modus: Greenfield Build · Branch: `claude/hermclaw-next-build` · Version `0.1.0rc1`
- **Release Candidate gebaut und getestet:** P00–P42 vollständig bearbeitet; 347 Schritte erledigt, 77 erledigt mit
  offenem externem Teil (`[~]`: 76 Live-Verifikation BLOCKER-001, 41.9 Tag-Push BLOCKER-002), 0 offen.
- Abschlussbericht: `docs/operations/FINAL_BUILD_REPORT.md` · Tag `v0.1.0rc1`: Push aus der Build-Umgebung verweigert
  (HTTP 403, BLOCKER-002) – Befehl zum Setzen im Bericht, Abschnitt *Release commit/tag*
- Blocker: BLOCKER-001 (Zielhosts aus der Build-Umgebung nicht erreichbar), BLOCKER-002 (Tag-Push verweigert) – alle Live-Schritte sind automatisiert und
  in `docs/operations/INSTALLATION.md` beschrieben; Abnahme mit `scripts/ops/live-acceptance.sh` (Kapitel 12)
- Release-Status: **RC – lokal abgenommen, Live-Abnahme ausstehend**

## Fortschritt BUILD_PLAN (automatisch: `python scripts/dev/plan_progress.py --markdown`)
| Phase | erledigt | erledigt, live offen (BLOCKER-001) | offen |
|---|---|---|---|
| P00 – Initiale Research- und Inventurphase | 6 | 14 | 0 |
| P01 – Clean System Bootstrap | 3 | 12 | 0 |
| P02 – Backend Skeleton | 9 | 1 | 0 |
| P03 – Persistence | 9 | 0 | 0 |
| P04 – Event Store | 8 | 0 | 0 |
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
| P26 – SSH/Admin Tools | 7 | 1 | 0 |
| P27 – DB Tools | 7 | 0 | 0 |
| P28 – Container/Proxy Tools | 4 | 2 | 0 |
| P29 – Media | 7 | 0 | 0 |
| P30 – API | 9 | 0 | 0 |
| P31 – UI | 15 | 0 | 0 |
| P32 – Deployment | 0 | 10 | 0 |
| P33 – Backup/Restore | 5 | 1 | 0 |
| P34 – Recovery | 7 | 0 | 0 |
| P35 – Failure Injection | 19 | 0 | 0 |
| P36 – Security Hardening | 8 | 0 | 0 |
| P37 – Generalization E2E | 15 | 0 | 0 |
| P38 – Performance / Load | 8 | 0 | 0 |
| P39 – FULL BUG BASH / HARDENING | 13 | 0 | 0 |
| P40 – SOAK TEST | 6 | 3 | 0 |
| P41 – RELEASE CANDIDATE | 8 | 1 | 0 |
| P42 – CUTOVER PLAN | 2 | 4 | 0 |
| **Summe** | **347** | **77** | **0** |

## Deploymentstatus pro Host
| Host | Rolle | Status |
|---|---|---|
| 192.168.178.225 | Orchestrator | nicht deployt (BLOCKER-001); Bootstrap + Deployment im Debian-13-Container verifiziert |
| 192.168.178.222 | Coding / Execution Worker | nicht deployt (BLOCKER-001); Converge im Container, echte Podman-Sandbox-Tests |
| 192.168.178.224 | Model / Media Worker | nicht deployt (BLOCKER-001); Converge im Container (GPU-Pfad nur live) |
| 192.168.178.223 | WebUI / Nginx (Kontrollraum auf 8080/8443) neben dem bestehenden Portal Noob2Claw/Kowacore (Apache :80, bleibt, D-013) | nicht deployt (BLOCKER-001); nginx 1.28 im Container (auch neben fremdem Server auf :80), Portal-Button gegen den Menü-Parser des Portals getestet, Playwright gegen echtes Backend |
| 192.168.178.226 | GitLab | bestehend, unverändert; Vorbereitung INSTALLATION.md 6.1 |
| 192.168.178.60 | Terra / Unraid Backup | bestehend, unverändert; Vorbereitung INSTALLATION.md 6.2 / 11 |

## Modellrollen (fest, `config/models.example.yaml`)
- Planner/Replanner: `gemma4:26b` (32K) · Planner-Fallback (technisch): `gemma4:12b`
- Implementation Worker: `qwen3-coder:30b` (32K) · Heavy Reviewer: `qwen3.8:27b` · Fast Router: `qwen3:8b` (16K)
- Embeddings: `embeddinggemma:300m` (768 dim; EmbeddingGemma 2 erst, wenn Ollama es unter Linux/CUDA ausführt, D-010)

## Bugs
- 142 gefunden, **141 behoben**, 1 offen (P0 2, P1 26, P2 62, P3 49, P4 3) – `BUGS.md` (BUG-141: Koexistenz mit dem Portal auf .223, behoben;
  BUG-142: optionales SSH-Gate liest seine Standard-Allowlist nicht, P3, offen, Gate standardmäßig aus)
- Externer Blocker: BLOCKER-001

## Letzte Testresultate (2026-10-15, Build-Host)
- Finale Testmatrix `docs/testing/test-matrix-results.md`: **208 Dateien, 3232 bestanden, 0 fehlgeschlagen**, 24 Live-Tests
  deselektiert (unit 1956, integration 1042, failure 204, security 4, e2e 18, load 8); UI: vitest 48, Playwright 35
- Deployment: `test_deploy_release` 22, `test_deploy_infra` 38, `test_deploy_converge` 8 (30 min, Debian 13), Bootstrap-Suiten grün,
  ansible-lint (production) sauber
- Dependency-Audit 2026-10-15: keine bekannten Schwachstellen (runtime 107, LiteLLM 116 Pakete, UI 0)

## Betreiberentscheidung D-013 (2026-10-15)
`.223` bleibt das Portal Noob2Claw/Kowacore (Login/2FA, Wiki; wird nicht neu installiert). Hermclaws nginx läuft
daneben auf 8080/8443. Ins Portal kommt nur der Menüpunkt **Arbeit → KI-Kontrollraum** mit dem Button zum
Kontrollraum. Umsetzung:

- Ansible: Rollen nginx, common, nodejs und hermclaw_ui, Portplan, `check-host.sh`;
- `hosts.yaml`-Probe, Laufzeit-Portplan;
- Doku: INSTALLATION, cutover.md, Runbooks, Architektur;
- Risiken R-036–R-040, BUG-141/142.

Tests 2026-10-15, einzeln ausgeführt: `test_bootstrap_infra` 63, `test_bootstrap_roles` 15, `test_bootstrap_nginx` 10,
`test_bootstrap_converge` 1, `test_deploy_infra` 64, `test_infratools_ports` 14 und `test_security_checks` 6, alle
bestanden; ansible-lint (production) sauber. `test_deploy_converge` ist angepasst; ihn führt der Integrator aus.

## Nächster Schritt
Installation auf den Zielhosts nach `docs/operations/INSTALLATION.md` (`.223` nur ergänzen, nicht neu installieren),
danach Live-Abnahme (Kapitel 12) und Cutover nach `docs/runbooks/cutover.md`.
