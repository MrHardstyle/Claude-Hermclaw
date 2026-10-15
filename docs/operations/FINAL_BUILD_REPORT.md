# FINAL BUILD REPORT – Hermclaw Next 0.1.0rc1

Stand: 2026-10-15 · Branch `claude/hermclaw-next-build` · Release-Candidate-Commit: der letzte Commit, der diesen Bericht ändert (Tag `v0.1.0rc1`, siehe *Release commit/tag*)

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
| Worker-Protokoll | Version 1 (HMAC-SHA256-Signatur mit dem Worker-Token, Zeitstempel + Nonce gegen Replay; `Authorization: Bearer` nur über HTTPS, im LAN über HTTP also nicht) |
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
| Heavy Reviewer | `qwen3.8:27b` | 24K (24576; zulässig bis 32K) | fest |
| Fast Router | `qwen3:8b` | 16K | fest, voll auf der GPU |
| Embeddings | `embeddinggemma:300m` | 2K, 768 dim | D-010 (EmbeddingGemma 2 in Ollama nur MLX) |

Download auf .224: das Deployment lädt fehlende Tags automatisch (`hermclaw_worker_pull_models`, mit angehaltenem
`hermclaw-model-worker`). Von Hand nach INSTALLATION.md Kapitel 7: `sudo systemctl stop hermclaw-model-worker`, dann
`sudo systemd-run --unit=hermclaw-model-pull --setenv=OLLAMA_HOST=http://127.0.0.1:11434 /opt/hermclaw/bin/hermclaw-model-pull --ollama /usr/local/bin/ollama`,
zum Schluss `sudo systemctl start hermclaw-model-worker`.

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
| P41 – RELEASE CANDIDATE | 8 | 1 | 0 |
| P42 – CUTOVER PLAN | 2 | 4 | 0 |
| **Summe** | **347** | **77** | **0** |

`[~]` = lokal vollständig umgesetzt und getestet, Live-Verifikation auf den Ziel-Hosts offen (BLOCKER-001); bei 41.9 ist der Tag-Push offen (BLOCKER-002).

## Research reports

30 Research-Berichte unter `docs/research/`, Index `RESEARCH_INDEX.md` (R-001 bis R-030), jeweils mit
Quellen (bevorzugt Primärquellen) und Konsequenzen bzw. Entscheidung; R-025 bis R-029 (2026-10-13/14) prüfen
Versionen, Modell-Tags und Sicherheitshinweise vor der Installation erneut, R-030 ergänzt die DB-Tools (P27). Entscheidungen daraus: `DECISIONS.md` (u. a. D-010 Embedding-Modell,
D-011 NVIDIA-R580-Runfile, D-012 pgvector-Untergrenze).

## Test totals

| Suite | Dateien | bestanden | fehlgeschlagen | Fehler | deselektiert (live) |
|---|---|---|---|---|---|
| Unit (inkl. Contract-Tests) | 71 | 1956 | 0 | 0 | 0 |
| Integration (inkl. API, Deployment, Bootstrap) | 112 | 1042 | 0 | 0 | 24 |
| Failure Injection + Recovery | 20 | 204 | 0 | 0 | 0 |
| Security | 1 | 4 | 0 | 0 | 0 |
| E2E (Generalisierung + UI) | 3 | 18 | 0 | 0 | 0 |
| Last/Performance | 1 | 8 | 0 | 0 | 0 |
| **gesamt** | **208** | **3232** | **0** | **0** | 24 |

Nach dem Matrixlauf kam der Sicherheitsfix BUG-140 hinzu (reservierte Job-Metadaten-Schlüssel auf `POST /api/jobs`):
`tests/unit/test_job_metadata.py` 31 und `tests/integration/test_api_job_metadata.py` 27 neue Tests, grün; die
berührten Suiten (`test_api.py`, `test_scheduler.py`, `test_runtime_driver.py`, Contract-Docs, Security, UI/Playwright)
wurden einzeln erneut ausgeführt, ebenfalls grün.

Quelle: `docs/testing/test-matrix-results.{md,json}` (`scripts/dev/test_matrix.py`, eine pytest-Instanz pro Datei, Python
3.12 auf dem Build-Host; zweiter vollständiger Lauf am 2026-10-15 plus Nachlauf der fünf danach geänderten Dateien).
Zusätzlich innerhalb von `tests/e2e/test_ui_playwright.py`: **Vitest 48** UI-Unit-Tests sowie **Playwright 35** Tests
(33 Desktop + 2 Mobil) gegen das echte Backend. Weitere Prüfungen: `ansible-lint --profile=production` (0 Befunde), `shellcheck`, `ruff`,
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
Heavy Review, Commit/Push, Bericht) mit echten Werkzeugen (Git, pytest, php, tsc/node, podman-compose, ffmpeg).
Ersetzt sind: die Modellantworten (skriptgesteuert, die Modelle laufen erst live auf .224), GitLab durch ein lokales
Bare-Repository (`file://`), das Web durch einen lokalen Fixture-Webserver, die Worker-Hosts durch Daemons im
Testprozess (ASGI) und in 37.13 der WakeController durch einen Stellvertreter (Magic Packet live:
`tests/integration/test_wol_live.py`). Befehle laufen im Test-Executor ohne Podman-Sandbox (die Sandbox testet P18).

## Failure injection results

Alle 19 Szenarien aus P35 grün (`tests/failure/`, 20 Dateien, ergänzt um Integrations- und Unit-Tests laut `BUILD_PLAN.md` P35), u. a.: Planner-Timeout und ungültiges Planner-JSON
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
51,5 s als offline erkannt (Heartbeat-Timeout) und 2,2 s nach dem Neustart wieder bereit. Ergebnis im finalen Lauf: `test_recovery_process_kill.py` 2 und `test_resources_recovery.py` 9 bestanden, E2E 37.14 grün.

## Security results

P36 grün: Secret-Scan des Repositorys und jedes Job-Diffs; API-Scopes; HMAC-signierte Worker-Aufrufe mit
Replay-Schutz; SSH-Allowlist mit gepinnten Host-Keys und Einmal-Freigaben; Befehlsklassen
read/mutate/destructive/forbidden; Sandbox-Ausbruchsprüfungen (rootless, read-only, keine Capabilities, kein Netz
ohne Capability, Image-Gate gegen Podman-CVEs, BUG-108); Netzwerkrichtlinie inkl. SSRF-Schutz der Research-Engine;
Dependency-Audit vom 2026-10-15 ohne bekannte Schwachstellen (Laufzeit-venv 107, LiteLLM-venv 116 Pakete, UI `npm audit` 0, `docs/security/dependency-audit.md`). Härtung der Units (systemd), Tokens und Schlüssel der Dienste als verschlüsselte systemd-Credentials (SSH-Schlüssel als 0600-Dateien unter `/etc/hermclaw/ssh`, SearXNG-Secret als Podman-Secret),
keine Chain-of-Thought in Logs, Events oder UI. Ergebnis im finalen Lauf: `tests/security/` 4 bestanden, Sandbox-, SSH-, Befehls- und Worker-Auth-Suiten grün.

## Open bugs by severity

| Severity | gefunden | behoben | offen |
|---|---|---|---|
| P0 | 2 | 2 | 0 |
| P1 | 26 | 26 | 0 |
| P2 | 61 | 61 | 0 |
| P3 | 48 | 48 | 0 |
| P4 | 3 | 3 | 0 |
| **gesamt** | **140** | **140** | **0** |

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
* Der Bootstrap (Rolle `postgresql`) richtet CREATEDB und `hermclaw_restore_template` ein (`test_bootstrap_postgresql.py` 12).
  Das Deployment installiert die Skripte `hermclaw-backup`/`hermclaw-restore-test` und startet die Timer für Backup
  (täglich 02:30) und Restore-Test (sonntags 04:30) (`test_deploy_converge.py`).
* Erster Live-Lauf: INSTALLATION.md Kapitel 11 und Live-Suite `backup` (`tests/integration/test_backup_live.py`).

## Release commit/tag

* Commit: der letzte Commit auf `claude/hermclaw-next-build` (GitHub `MrHardstyle/Claude-Hermclaw`), der diesen Bericht
  ändert: `git log -1 --format=%H origin/claude/hermclaw-next-build -- docs/operations/FINAL_BUILD_REPORT.md`.
* Tag: `v0.1.0rc1` (annotiert) – in der Build-Umgebung angelegt, der Push nach GitHub wurde dort aber mit HTTP 403
  abgelehnt (nur der Arbeits-Branch darf gepusht werden; `BUGS.md` BLOCKER-002). Einmalig in einem Checkout mit
  Schreibrecht setzen:

  ```bash
  git fetch origin claude/hermclaw-next-build && git tag -a v0.1.0rc1 -m "Hermclaw Next 0.1.0rc1 – Release Candidate" $(git log -1 --format=%H origin/claude/hermclaw-next-build -- docs/operations/FINAL_BUILD_REPORT.md) && git push origin v0.1.0rc1
  ```
* Release-Artefakt: wird auf dem Admin-Rechner gebaut (`scripts/deploy/build-release.sh` → `dist/release-0.1.0rc1/`,
  INSTALLATION.md Kapitel 8.3) – aus dem Tag baubar (Linux x86_64, CPython 3.13), Selbsttest inklusive. Einen identischen Neubau gibt es nur mit `--constraints <constraints.txt des Release>` oder `--offline-from <Wheel-Cache aus --wheel-cache>`, denn die Laufzeit-Abhängigkeiten in `pyproject.toml` sind nur nach unten begrenzt.

## Cutover steps

`docs/runbooks/cutover.md`:

0. Go/No-Go: RC-Tests grün, Bootstrap idempotent (`--check` changed=0), Portplan ohne Fremdbelegung (Preflight
   `--tags preflight`), `/api/health?deep=true` ok, Modelle auf .224, Worker bereit, Live-Abnahme
   (`scripts/ops/live-acceptance.sh` auf .225 und .222: jede Suite `PASS` oder `SKIP` mit Grund), Backup + Restore-Test ok,
   Live-Rauchtest-Job `succeeded` mit gepushtem Job-Branch (Merge Request nur mit `git.create_merge_request: true`).
1. Altsystem read-only einfrieren: alte UI nur GET/HEAD, Patch-Watcher stoppen und deaktivieren (nicht entfernen),
   altes GitLab-Projekt archivieren, Snapshot nach .60 – nichts wird gelöscht.
2. Umschalten: die vom Deployment aktivierte Site `hermclaw-next.conf` bleibt, der Symlink der alten Site wird
   entfernt, `nginx -t`, Reload, `/api/health` über nginx.
3. Stabilisierung beobachten: Liveness der Units, `/api/metrics` mit Alarmvorschlägen, Health, tägliche
   Backup-/wöchentliche Restore-Test-Kontrolle, Bug-Triage.
4. Altkomponenten erst nach ≥ 14 Tagen ohne P0/P1-Bug, ≥ 20 erfolgreichen echten Jobs, zwei bestandenen Restore-Tests
   und ohne Rollback stilllegen, einzeln und jeweils mit frischer Sicherung; das archivierte GitLab-Projekt bleibt
   erhalten (`cutover.md` §5).

## Rollback steps

* **Cutover-Rollback** (jederzeit in der Stabilisierungsphase, ohne Datenverlust): alte nginx-Site wieder verlinken,
  `hermclaw-next.conf` entfernen, Read-only-Block der alten Site zurücknehmen, `nginx -t` + Reload, Patch-Watcher
  wieder aktivieren, GitLab-Projekt de-archivieren, auf .225 `hermclaw-scheduler` und `hermclaw-api` stoppen
  (Zustand bleibt in PostgreSQL) – `docs/runbooks/cutover.md` §4.
* **Release-Rollback**: `ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -e hermclaw_version=<alt> -K`;
  venv, Payload und UI-Build der alten Version liegen noch auf den Hosts (`hermclaw_keep_releases`, Standard 3), und
  `dist/release-<alt>/` muss auf dem Admin-Rechner noch vorhanden sein; .222/.224 vorher wecken, dann schalten nur die
  Symlinks zurück. Kennt die alte Version das Datenbankschema nicht, stoppt der Lauf vor jeder Änderung; dann nach
  einem Backup mit `-e hermclaw_orchestrator_allow_schema_downgrade=true` erneut ausführen (Downgrade mit den Migrationen
  des neueren Release). Nur wenn eine Migration laut Release Notes nicht sicher umkehrbar ist: Scheduler, API und
  Backup-Timer stoppen, Datenbank leer neu anlegen, das Backup von vor dem Upgrade mit `restore-full` einspielen, dann
  `<alt>` deployen – `docs/runbooks/upgrade.md`.
* **Host-Verlust**: `docs/runbooks/disaster-recovery.md` (pro Host, Credentials aus dem Passwortmanager,
  `restore-full` aus dem neuesten getesteten Backup-Satz).

## Fortsetzung (Live-Abnahme)

1. Auf dem Admin-Rechner und den Hosts `docs/operations/INSTALLATION.md` Kapitel 2–11 abarbeiten.
2. Live-Abnahme nach Kapitel 12: vorher den Leerlauf-Suspend von `exec-222` und `model-224` abschalten
   (`hermclaw_config_host_overrides` mit `idle_sleep_command: null`, dann Deployment). Checkout von `v0.1.0rc1` mit
   dev-venv (`pip install -e '.[dev]'`) nach `/opt/hermclaw/src` auf .225 und .222, einmalig auf .225
   `sudo scripts/ops/live-acceptance.sh --prepare-db`, dann `sudo scripts/ops/live-acceptance.sh` auf beiden Hosts
   (GitLab- und volle WoL-Suite wie dort beschrieben), zum Schluss auf .225 `--cleanup-db`. Danach die Overrides wieder
   entfernen und das Deployment für `orchestrator` erneut ausführen. Jedes bestandene Ergebnis in
   `BUILD_PLAN.md` von `[~]` auf `[x]` setzen und in `STATUS.md` eintragen.
3. Erst wenn alle Suiten grün sind: Cutover nach `docs/runbooks/cutover.md`.
