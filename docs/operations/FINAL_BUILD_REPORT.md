# FINAL BUILD REPORT – Hermclaw Next 0.1.0rc1

Stand: 2026-10-15 · Branch `claude/hermclaw-next-build` · Tag `v0.1.0rc1` · Commit: der mit `v0.1.0rc1` getaggte Commit

Hermclaw Next ist nach Bauplan v2 vollständig gebaut (P00–P42) und lokal gegen echte Dienste getestet
(PostgreSQL, Podman, podman-compose, nginx, Git, LiteLLM, ffmpeg, sshd/rsync, Ansible-Converge in Debian-13-Containern,
Playwright). Die Ziel-Hosts im Heim-LAN waren aus der Build-Umgebung nicht erreichbar (**BLOCKER-001**). Alle
Schritte, die echte Hardware oder das LAN brauchen, sind deshalb automatisiert, dokumentiert und in `BUILD_PLAN.md`
mit `[~]` markiert. Die Live-Abnahme holt sie nach: `docs/operations/INSTALLATION.md` abarbeiten, dann
`scripts/ops/live-acceptance.sh` (Kapitel 12).

## Version

| | |
|---|---|
| Hermclaw | `0.1.0rc1` (PEP 440; `pyproject.toml`, `hermclaw/__init__.py`, `hermclaw --version`) |
| Worker-Protokoll | Version 1 (Bearer-Token + HMAC-Signatur) |
| Datenbankschema | Alembic-Head `0002` (`migrations/versions/`, im Wheel unter `share/hermclaw`) |
| Laufzeit | CPython 3.13 (Debian 13), Release-Wheelhouse mit Hash-Lock (`scripts/deploy/build-release.sh`) |
| LiteLLM | `litellm[proxy]==1.104.2` + `prisma==0.15.0` in eigener venv |
| Änderungen | `CHANGELOG.md` [0.1.0rc1] |

## Hosts deployed

| Host | Rolle | Live-Status | Lokal verifiziert |
|---|---|---|---|
| 192.168.178.225 | Orchestrator (API, Scheduler, PostgreSQL 17 + pgvector, LiteLLM, SearXNG, Backup) | nicht deployt (BLOCKER-001) | Bootstrap + Deployment im Debian-13-Container (`test_bootstrap_converge.py`, `test_deploy_converge.py`): zweiter Lauf changed=0, Upgrade, Rollback, Trockenläufe |
| 192.168.178.222 | Execution Worker (rootless Podman-Sandbox) | nicht deployt (BLOCKER-001) | Converge im Container, echte Podman-Sandbox-Tests |
| 192.168.178.224 | Model/Media Worker (GTX 1080, Ollama 0.40.4, NVIDIA R580) | nicht deployt (BLOCKER-001) | Converge im Container mit simuliertem NVIDIA-Gerät; GPU-Pfad nur live |
| 192.168.178.223 | Web-UI / nginx | nicht deployt (BLOCKER-001) | nginx 1.28 im Container (SSE, CSP, TLS), Playwright gegen echtes Backend |
| 192.168.178.226 | GitLab | bestehend, nicht verändert | GitLab-API gegen Testdoppel; Live-Push in der Abnahme |
| 192.168.178.60 | Terra/Unraid Backup | bestehend, nicht verändert | rsync über sshd mit rrsync-Forced-Command lokal |

## Services

| Host | Unit / Prozess | Port | Aufgabe |
|---|---|---|---|
| .225 | `postgresql@17-main` | 5432 (localhost) | Zustand, Events, pgvector-Index |
| .225 | `hermclaw-litellm.service` | 4000 (localhost) | Modell-Gateway vor Ollama, Master-Key-Pflicht |
| .225 | `hermclaw-api.service` | 8000 | REST + SSE, Token-Scopes read/control/admin, `/api/metrics` |
| .225 | `hermclaw-scheduler.service` | – | Planner → Scope → Coder → Verifier → Review → Commit; Leases, WoL, Recovery |
| .225 | `hermclaw-searxng` (rootless Quadlet, Image per Digest) | 8888 (localhost) | Research-Suche |
| .225 | `hermclaw-backup.timer` / `hermclaw-restore-test.timer` | – | Backup täglich 02:30, Restore-Test sonntags 04:30 |
| .222 | `hermclaw-exec-worker.service` | 8787 | Befehle/Tests in der rootless Podman-Sandbox |
| .224 | `hermclaw-model-worker.service`, `ollama.service`, `hermclaw-ollama-firewall.service` | 8787, 11434 (nur von .225) | Modelle, Media (ffmpeg/NVENC) |
| .223 | `nginx` (Site `hermclaw-next.conf`) | 80/443 | statische UI, Proxy `/api` inkl. SSE |
| .222/.224 | `hermclaw-wol@<nic>.service` | – | Wake-on-LAN im NIC aktivieren |

## Models

| Rolle | Ollama-Tag | Kontext | Hinweis |
|---|---|---|---|
| Planner / Replanner | `gemma4:26b` | 32K | fest (Gemma) |
| Planner-Fallback (technisch) | `gemma4:12b` | 32K | nur bei Infrastrukturfehlern |
| Implementation Worker | `qwen3-coder:30b` | 32K | fest |
| Heavy Reviewer | `qwen3.8:27b` | 24–32K | fest |
| Fast Router | `qwen3:8b` | 16K | fest, voll auf der GPU |
| Embeddings | `embeddinggemma:300m` | 2K, 768 dim | D-010 (EmbeddingGemma 2 in Ollama nur MLX) |

Download auf .224: `sudo -u ollama /opt/hermclaw/bin/hermclaw-model-pull` (INSTALLATION.md Kapitel 7; das Deployment
lädt fehlende Tags automatisch).

## Worker status

| Worker | Implementiert | Lokal getestet | Live |
|---|---|---|---|
| `exec-222` (execution) | Daemon, Sandbox (rootless, read-only, `--network=none`, cap-drop ALL, Limits, Image-Gate), Workspace-Sync, Heartbeats, Drain | ja (echtes Podman, Recovery nach Kill, Reconnect) | offen (BLOCKER-001) |
| `model-224` (model/media) | Daemon, Ollama-Residenz, ffmpeg/NVENC, ComfyUI-Adapter, GPU-Lease | ja (echtes ffmpeg, Lease/Preemption) | offen (BLOCKER-001) |

## Completed phases

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
| P41 – RELEASE CANDIDATE | 9 | 0 | 0 |
| P42 – CUTOVER PLAN | 2 | 4 | 0 |
| **Summe** | **348** | **76** | **0** |

`[~]` = lokal vollständig umgesetzt und getestet, Live-Verifikation auf den Ziel-Hosts offen (BLOCKER-001).

## Research reports

30 Research-Berichte unter `docs/research/`, Index `RESEARCH_INDEX.md` (R-001 bis R-030), jeweils mit
Primärquellen, Gegenprüfung und Konsequenzen. Entscheidungen daraus: `DECISIONS.md` (u. a. D-010 Embedding-Modell,
D-011 NVIDIA-R580-Runfile, D-012 pgvector-Untergrenze).

## Test totals

| Suite | Dateien | bestanden | fehlgeschlagen | Fehler | deselektiert (live) |
|---|---|---|---|---|---|
| Unit | 71 | 1956 | 0 | 0 | 0 |
| Integration (inkl. Contract, Deployment, Bootstrap, UI-Backend) | 112 | 1042 | 0 | 0 | 24 |
| Failure Injection + Recovery | 20 | 204 | 0 | 0 | 0 |
| Security | 1 | 4 | 0 | 0 | 0 |
| E2E (Generalisierung + UI) | 3 | 18 | 0 | 0 | 0 |
| Last/Performance | 1 | 8 | 0 | 0 | 0 |
| **gesamt** | **208** | **3232** | **0** | **0** | 24 |

Quelle: `docs/testing/test-matrix-results.{md,json}` (`scripts/dev/test_matrix.py`, eine pytest-Instanz pro Datei, Python
3.12 auf dem Build-Host; zweiter vollständiger Lauf am 2026-10-15 plus Nachlauf der fünf danach geänderten Dateien).
Zusätzlich innerhalb von `tests/e2e/test_ui_playwright.py`: **Vitest 48** und **Playwright 35** Tests (Desktop + Mobil)
gegen das echte Backend. Weitere Prüfungen: `ansible-lint --profile=production` (0 Befunde), `shellcheck`, `ruff`,
`mypy`, `ansible-playbook --syntax-check`; Converge von `site.yml` + `deploy.yml` in Debian-13-Containern
(`test_deploy_converge.py`, 30 min: zweiter Lauf und `--check` changed=0, Upgrade, Prune, Rollback).
Die 24 deselektierten Tests sind die Live-Tests gegen die echten Hosts (`-m live`, BLOCKER-001); sie laufen mit
`scripts/ops/live-acceptance.sh`.

## E2E matrix

| # | Szenario | Test | Ergebnis |
|---|---|---|---|
| 37.1 | Python FastAPI Feature | `tests/e2e/test_e2e_code_tasks.py::test_37_1_python_fastapi_feature` | grün |
| 37.2 | SQLite Schema Migration | `…::test_37_2_sqlite_schema_migration` | grün |
| 37.3 | PostgreSQL Migration | `…::test_37_3_postgresql_migration` | grün |
| 37.4 | Docker Compose Service | `…::test_37_4_docker_compose_service` | grün |
| 37.5 | React/CSS Navigation | `…::test_37_5_react_css_navigation` | grün |
| 37.6 | PHP Backend Form | `…::test_37_6_php_backend_form` | grün |
| 37.7 | TypeScript Utility | `…::test_37_7_typescript_utility` | grün |
| 37.8 | YAML-only Config | `…::test_37_8_yaml_only_config` | grün |
| 37.9 | Shell/Linux Admin | `…::test_37_9_shell_linux_admin` | grün |
| 37.10 | Research-only Task | `tests/e2e/test_e2e_platform.py::test_37_10_research_only_task` | grün |
| 37.11 | Image Job | `…::test_37_11_image_job` | grün |
| 37.12 | Video Job | `…::test_37_12_video_job` | grün |
| 37.13 | Wake-on-LAN Job | `…::test_37_13_wake_on_lan_job_runs_on_the_woken_remote_worker` | grün |
| 37.14 | Worker failure/recovery | `…::test_37_14_worker_restart_and_connection_loss_are_recovered` | grün |
| 37.15 | Planner replan after failure | `tests/e2e/test_e2e_code_tasks.py::test_37_15_planner_replan_after_failure` | grün |
| P31 | Web-UI (Vitest + Playwright gegen echtes Backend) | `tests/e2e/test_ui_playwright.py` | grün |

Die E2E-Tests fahren die vollständige Runtime (Triage, Gemma-Plan, Scope, Qwen3-Coder-Werkzeugschleife, Verifier,
Heavy Review, Commit/Push, Bericht) mit echten Werkzeugen; nur die Modellantworten sind skriptgesteuert (die
Modelle laufen erst live auf .224).

## Failure injection results

Alle 19 Szenarien aus P35 grün (`tests/failure/`, 22 Dateien), u. a.: Planner-Timeout und ungültiges Planner-JSON
(Reparatur, dann Fehler mit Code), Coder-Timeout, Worker offline (Offline-Sweep, Wecken), Ollama/LiteLLM nicht
erreichbar (technischer Fallback, redigierte Fehler), GitLab nicht erreichbar, DB-Verbindungen mitten im Job
getrennt (`pg_terminate_backend`, Job beendet), Prozessabsturz (SIGKILL), Plattenplatz-Warnung (Annahmestopp),
Scope-Verletzung, gefundenes Geheimnis, Merge-Konflikt, veralteter Basis-SHA, Verifier-Absturz, Reviewer-Timeout
(fail closed), WoL-Timeout, Research-Quelle nicht erreichbar, Netzwerkfehler, Video-Preemption. Dazu Backup-,
Infra-Tools-, SSH-Tools- und DB-Tools-Fehlerfälle. Ergebnis im finalen Lauf: `tests/failure/` 20 Dateien, 204 bestanden, 0 fehlgeschlagen.

## Recovery results

P34 grün (`tests/failure/test_recovery_process_kill.py`, echter SIGKILL des Scheduler-Prozesses in zwei Szenarien):
Neustart mit stabiler Instanz-ID, Rückgewinnung der eigenen Step-Leases, verlorene Versuche als `WORKER_LOST`,
Checkpoint-Wiederaufnahme des Coders, Freigabe der Modell-Lease, derselbe Workspace (unkommittierte Änderung
erhalten), genau ein verifizierter Commit auf dem Job-Branch. Soak: Execution-Worker per SIGKILL beendet, nach
2,2 s wieder bereit. Ergebnis im finalen Lauf: `test_recovery_process_kill.py` 2 und `test_resources_recovery.py` 9 bestanden, E2E 37.14 grün.

## Security results

P36 grün: Secret-Scan des Repositorys und jedes Job-Diffs; API-Scopes; HMAC-signierte Worker-Aufrufe mit
Replay-Schutz; SSH-Allowlist mit gepinnten Host-Keys und Einmal-Freigaben; Befehlsklassen
read/mutate/destructive/forbidden; Sandbox-Ausbruchsprüfungen (rootless, read-only, keine Capabilities, kein Netz
ohne Capability, Image-Gate gegen Podman-CVEs, BUG-108); Netzwerkrichtlinie inkl. SSRF-Schutz der Research-Engine;
Dependency-Audit vom 2026-10-15 ohne bekannte Schwachstellen (Laufzeit-venv 107, LiteLLM-venv 116 Pakete, UI `npm audit` 0, `docs/security/dependency-audit.md`). Härtung der Units (systemd), Geheimnisse nur als verschlüsselte systemd-Credentials,
keine Chain-of-Thought in Logs, Events oder UI. Ergebnis im finalen Lauf: `tests/security/` 4 bestanden, Sandbox-, SSH-, Befehls- und Worker-Auth-Suiten grün.

## Open bugs by severity

| Severity | gefunden | behoben | offen |
|---|---|---|---|
| P0 | 2 | 2 | 0 |
| P1 | 25 | 25 | 0 |
| P2 | 61 | 61 | 0 |
| P3 | 48 | 48 | 0 |
| P4 | 3 | 3 | 0 |
| **gesamt** | **139** | **139** | **0** |

Externer Blocker: **BLOCKER-001** (Ziel-LAN aus der Build-Umgebung nicht erreichbar) – kein Bug, siehe `BUGS.md`.

## Known limitations

`docs/operations/known-limitations.md`, Kurzfassung:

* Live-Verifikation offen (BLOCKER-001): GPU/Ollama auf der GTX 1080, Modell-Downloads und Token-Raten, Wake-on-LAN
  und Suspend, Pushes/Merge Requests gegen GitLab .226, Backups nach Terra .60, Deployment auf den echten Hosts,
  24-h-Soak auf der Zielhardware.
* GTX 1080 (8 GB): die 26–30B-Modelle laufen größtenteils aus dem RAM, also langsam; immer nur ein großes Modell
  geladen.
* Embedding `embeddinggemma:300m` statt EmbeddingGemma 2 (D-010); NVIDIA R580 per Runfile (D-011); pgvector ≥ 0.8.7
  mit dokumentierter Ausnahme ab 0.8.2 (D-012).
* Kein HA (ein Orchestrator, eine PostgreSQL-Instanz); Credentials an den Host gebunden (Passwortmanager).
* Offline-Releases: Sicherheitsupdates der Python-Abhängigkeiten nur mit neuem Release.

## Backup/restore result

Lokal vollständig getestet (Live-Lauf gegen Terra .60: BLOCKER-001):

* `tests/unit/test_backup_units.py` 43, `tests/integration/test_backup_restore.py` 7, `test_backup_rsync.py` 5,
  `test_backup_cli.py` 5, `test_backup_privileges.py` 1, `tests/failure/test_backup_failures.py` 24 – alle grün:
  konsistenter `pg_dump` auf exportiertem Snapshot, Konfiguration ohne Klartext-Geheimnisse, Artefakte, signiertes
  Manifest, rsync über einen echten sshd mit `rrsync`-Forced-Command, Retention, Sperren, Restore-Test in eine
  Wegwerf-Datenbank als Nicht-Superuser über das pgvector-Template, `restore-full`, Fehlerfälle (abgebrochene
  Übertragung, manipulierte Archive, falsche Signatur, fehlendes Ziel).
* Deployment richtet Backup-Units (täglich 02:30), Restore-Test (sonntags 04:30), Skripte, CREATEDB und
  `hermclaw_restore_template` ein (`test_bootstrap_postgresql.py` 12, `test_deploy_converge.py`).
* Erster Live-Lauf: INSTALLATION.md Kapitel 11 und Live-Suite `backup` (`tests/integration/test_backup_live.py`).

## Release commit/tag

* Commit: der Commit, den das Tag `v0.1.0rc1` markiert, auf `claude/hermclaw-next-build` (GitHub `MrHardstyle/Claude-Hermclaw`)
* Tag: `v0.1.0rc1` (annotiert)
* Release-Artefakt: wird auf dem Admin-Rechner gebaut (`scripts/deploy/build-release.sh` → `dist/release-0.1.0rc1/`,
  INSTALLATION.md Kapitel 8.3) – reproduzierbar aus dem Tag, Selbsttest inklusive.

## Cutover steps

`docs/runbooks/cutover.md`:

0. Go/No-Go: RC-Tests grün, Bootstrap idempotent, `/api/health?deep=true` ok, Modelle auf .224, Worker bereit,
   Backup + Restore-Test ok, Live-Rauchtest-Job mit Merge Request, Portplan ohne Fremdbelegung
   (`scripts/ops/live-acceptance.sh` deckt die technischen Punkte ab).
1. Altsystem read-only einfrieren: alte UI nur GET/HEAD, Patch-Watcher stoppen und deaktivieren (nicht entfernen),
   altes GitLab-Projekt archivieren, Snapshot nach .60 – nichts wird gelöscht.
2. Umschalten: die vom Deployment aktivierte Site `hermclaw-next.conf` bleibt, der Symlink der alten Site wird
   entfernt, `nginx -t`, Reload, `/api/health` über nginx.
3. Stabilisierung beobachten: Liveness der Units, `/api/metrics` mit Alarmvorschlägen, Health, tägliche
   Backup-/wöchentliche Restore-Test-Kontrolle, Bug-Triage.
4. Altkomponenten erst nach ≥ 14 Tagen ohne P0/P1, ≥ 20 erfolgreichen Jobs und zwei bestandenen Restore-Tests
   stilllegen, einzeln und jeweils mit frischer Sicherung.

## Rollback steps

* **Cutover-Rollback** (jederzeit in der Stabilisierungsphase, ohne Datenverlust): alte nginx-Site wieder verlinken,
  `hermclaw-next.conf` entfernen, Read-only-Block der alten Site zurücknehmen, `nginx -t` + Reload, Patch-Watcher
  wieder aktivieren, GitLab-Projekt de-archivieren, auf .225 `hermclaw-scheduler` und `hermclaw-api` stoppen
  (Zustand bleibt in PostgreSQL) – `docs/runbooks/cutover.md` §4.
* **Release-Rollback**: `ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -e hermclaw_version=<alt> -K`;
  die alte venv ist noch installiert, nur die Symlinks schalten zurück. Kennt die alte Version das Datenbankschema
  nicht, stoppt der Lauf vor jeder Änderung; dann nach einem Backup mit
  `-e hermclaw_orchestrator_allow_schema_downgrade=true` oder Backup von vor dem Upgrade zurückspielen –
  `docs/runbooks/upgrade.md`.
* **Host-Verlust**: `docs/runbooks/disaster-recovery.md` (pro Host, Credentials aus dem Passwortmanager,
  `restore-full` aus dem neuesten getesteten Backup-Satz).

## Fortsetzung (Live-Abnahme)

1. Auf dem Admin-Rechner und den Hosts `docs/operations/INSTALLATION.md` Kapitel 2–11 abarbeiten.
2. Live-Abnahme nach Kapitel 12: Checkout von `v0.1.0rc1` nach `/opt/hermclaw/src` auf .225 und .222,
   `sudo scripts/ops/live-acceptance.sh --prepare-db`, dann `sudo scripts/ops/live-acceptance.sh` auf beiden Hosts
   (GitLab- und volle WoL-Suite wie dort beschrieben), zum Schluss `--cleanup-db`. Jedes bestandene Ergebnis in
   `BUILD_PLAN.md` von `[~]` auf `[x]` setzen und in `STATUS.md` eintragen.
3. Erst wenn alle Suiten grün sind: Cutover nach `docs/runbooks/cutover.md`.
