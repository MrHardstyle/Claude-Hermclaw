# TEST MATRIX

## Ebenen

- Unit
- Contract
- Integration
- Failure Injection
- Recovery
- Concurrency
- Security
- UI / Playwright
- Generalization E2E
- Performance / Load
- Soak

## Pflicht-Generalization-Fixtures

- [x] Python FastAPI Feature (37.1, `tests/e2e/`)
- [x] SQLite Migration (37.2, `tests/e2e/`)
- [x] PostgreSQL Migration (37.3, `tests/e2e/`)
- [x] Docker Compose Service (37.4, `tests/e2e/`)
- [x] React/CSS Navigation (37.5, `tests/e2e/`)
- [x] PHP Backend Form (37.6, `tests/e2e/`)
- [x] TypeScript Utility (37.7, `tests/e2e/`)
- [x] YAML-only Config (37.8, `tests/e2e/`)
- [x] Linux Admin (37.9, `tests/e2e/`)
- [x] Research-only Task (37.10, `tests/e2e/`)
- [x] Image Job (37.11, `tests/e2e/`)
- [x] Video Job (37.12, `tests/e2e/`)
- [x] Wake-on-LAN Job (37.13, `tests/e2e/`)
- [x] Worker Failure / Recovery (37.14, `tests/e2e/`)
- [x] Planner Replan after Failure (37.15, `tests/e2e/`)

## Finale Testmatrix (P39.9 / P41.8, 2026-10-15)

`scripts/dev/test_matrix.py` (eine pytest-Instanz pro Datei) → `docs/testing/test-matrix-results.{md,json}`:
**208 Dateien, 3232 bestanden, 0 fehlgeschlagen, 0 Fehler**, 24 Live-Tests deselektiert (BLOCKER-001) – unit 1956,
integration 1042, failure 204, security 4, e2e 18, load 8; UI innerhalb von `tests/e2e/test_ui_playwright.py`: Vitest 48,
Playwright 35. Live-Suiten: `scripts/ops/live-acceptance.sh` (INSTALLATION.md Kapitel 12).

## Komponenten-Testläufe (Build-Umgebung)

| Phase | Komponente | Befehl | Ergebnis | Live-Test |
|---|---|---|---|---|
| P25 | Scheduler | `pytest tests/integration/test_scheduler.py` | 17 passed | – |
| P06 | Git Engine | `pytest tests/*/test_gitops_*.py` | 185 passed, 1 skipped (live) | `pytest -m live tests/integration/test_gitops_live.py` (GitLab .226) |
| P07 | Worker-Protokoll/Daemons | `pytest tests/*/test_workers_*.py` | 134 passed, 1 skipped (Podman-Sandbox) | `pytest -m live tests/integration/test_workers_live.py` (HERMCLAW_LIVE_EXEC_TOKEN_FILE, HERMCLAW_LIVE_MODEL_TOKEN_FILE, HERMCLAW_LIVE_DATABASE_URL) |
| P14/P24 | Planner/Replanner | `pytest tests/*/test_planner_*.py` | 113 passed | Live-Gemma (14.2/24.2) über LiteLLM `planner-gemma` |
| P15 | Scope Engine | `pytest tests/*/test_scope_*.py` | 198 passed | – |
| P08 | Model Gateway | `HERMCLAW_TEST_LITELLM_BIN=<litellm-venv>/bin/litellm pytest tests/*/test_models_*.py` | 129 passed (inkl. 12 empirische LiteLLM-Proxy-Tests), 6 live deselektiert | `pytest -m live tests/integration/test_models_live.py` gegen LiteLLM `.225:4000` + Ollama `.224` |
| P16 | Context Builder | `pytest tests/*/test_context_builder_*.py` | 79 passed | – |
| P17 | Tool Engine | `pytest tests/*/test_tools_*.py` | 151 passed | Remote-Sandbox `.222` über Integration (P18/P19) |
| P30+ | Runtime Driver | `pytest tests/integration/test_runtime_driver.py` | 9 passed | – |
| P18 | Execution Sandbox | `pytest tests/*/test_sandbox_*.py` | 117 passed, 1 live deselektiert | `pytest -m live -k test_live_222_rootless_with_delegated_cgroups_v2` auf `.222` |
| P21 | Deterministic Verifier | `pytest tests/*/test_verifier_*.py` | 124 passed (echte pytest/go/cargo/tsc/php/node/bash) | – |
| P19/P23 | Coder-Loop + Implement-Handler | `pytest tests/integration/test_coder_loop.py tests/integration/test_coder_handler.py` | 12 passed | Live-Coder über LiteLLM `coder-main` |
| – | Operator-CLI | `pytest tests/integration/test_cli.py` | 3 passed | – |
| P20 | Stagnation Detection | `pytest tests/*/test_stagnation_*.py` | 145 passed | – |
| P22 | Heavy Review | `pytest tests/*/test_review_*.py` | 112 passed | Live-Review über LiteLLM `heavy-review` (Qwen3.8 27B) |
| P09 | Resource Manager | `pytest tests/*/test_resources_*.py` | 55 passed (Advisory Locks, Preemption, Recovery) | – |
| P29 | Media Worker | `pytest tests/integration/test_media_worker.py` | 4 passed (echtes ffmpeg/ffprobe) | NVENC + ComfyUI auf `.224` |
| P18/D-005 | Remote-Executor | `pytest tests/integration/test_tools_remote_executor.py` | 5 passed | gegen `.222` |
| P11 | Repository Intelligence | `pytest tests/*/test_repo_intelligence_*.py` | 91 passed, 1 live deselektiert (pgvector, ripgrep, tree-sitter) | – |
| P12 | Research Engine | `pytest tests/*/test_research_*.py` | 143 passed, 2 live deselektiert | SearXNG auf `.225:8888` |
| P01 | Clean Bootstrap (Ansible-Rollen, systemd-Units, Skripte) | `pytest tests/integration/test_bootstrap_{infra,roles,converge,nginx,postgresql,tooling}.py` (einzeln, ≈13 min; braucht podman, PostgreSQL-Server-Binaries + runuser, systemd-analyze, shellcheck, Images nginx:1.28 + python:3.12-slim) | 92 passed (inkl. ansible-lint production profile, echtes nginx -t + SSE in Podman, Wegwerf-PostgreSQL-Cluster mit gerenderter Konfiguration, Converge im Container) | Playbooks gegen `.222–.226` (BLOCKER-001) |
| P27 | Database Tools | `pytest tests/*/test_dbtools_*.py` (+ `tests/unit/test_sandbox_runtime_env.py`) | 193 passed (echtes PostgreSQL, pg_dump/pg_restore, Alembic in Wegwerf-DB) | Zieldatenbanken aus `config/databases.yaml` |
| P41/BLOCKER-001 | Live-Abnahme-Skript `scripts/ops/live-acceptance.sh` | `pytest tests/integration/test_live_acceptance_script.py` | 3 passed (`--prepare-db` gegen echtes PostgreSQL: Nicht-Superuser-Rolle legt Test-DBs aus dem pgvector-Template an, alle Migrationen; `--cleanup-db`; jede Live-Testdatei ist einer Suite zugeordnet) | `sudo scripts/ops/live-acceptance.sh` auf `.225` und `.222` |
| P32 | Deployment | `pytest tests/integration/test_deploy_{release,infra,converge}.py tests/integration/test_bootstrap_postgresql.py` (einzeln) | 22 + 35 + 8 passed (Release-Build offline mit Hash-Lock in `python:3.13-slim-trixie`, Migrationen auf/ab; Rollen gegen echte Verzeichnisbäume, nginx 1.28, Stub-API; Converge von `site.yml` + `deploy.yml` in Debian-13-Containern: zweiter Lauf und `--check` changed=0, Trockenläufe ohne Änderung auf der Platte, Upgrade/Prune/Rollback mit Schema-Downgrade-Opt-in, `systemd-analyze verify` aller Units inkl. Restore-Test); PostgreSQL-Rolle inkl. Restore-Template (Klon als Dienstrolle) | Live-Deployment auf `.222`–`.225` (BLOCKER-001), danach `scripts/ops/live-acceptance.sh` |
| P33 | Backup/Restore | `pytest tests/unit/test_backup_units.py tests/integration/test_backup_{restore,rsync,cli,privileges}.py tests/failure/test_backup_failures.py` (einzeln) | 84 passed (echtes pg_dump/pg_restore, rsync über einen lokalen sshd mit rrsync-Forced-Command, Restore-Test in Wegwerf-DB als Nicht-Superuser mit pgvector-Template, Manifest-Signatur, Retention, Locks, Fehlerfälle) | `pytest -m live tests/integration/test_backup_live.py` gegen Terra `.60` |
| D-013 | Geteilter Web-Host `.223` (Portal Noob2Claw/Kowacore bleibt, Kontrollraum auf 8080/8443, Portal-Button) | `pytest` einzeln: `tests/integration/test_bootstrap_{infra,roles,nginx,converge}.py`, `tests/integration/test_deploy_infra.py`, `tests/unit/test_infratools_ports.py`, `tests/security/test_security_checks.py`; `ansible-lint --offline --profile=production`, `shellcheck scripts/bootstrap/check-host.sh` | 63 + 14 + 10 + 1 + 64 + 14 + 6 passed (2026-10-15): Portplan/`check-host.sh` synchron (8080/8443, nie 80/443), Rollen-Validierung (reservierte Ports, HSTS, ufw-Quellen), Debian-Default-Site nur als Paket-Symlink entfernt, `nginx -T`-Listener-Prüfung (auch auf echten nginx-Dumps), Live-Socket-Prüfung mit Stopp, ufw nur `status`/`allow` und idempotent, NodeSource-Schutz für Debians npm, Fremd-Konto-/Verzeichnis-Schutz; echtes nginx 1.28 neben fremdem Server auf :80 (Start ok, Default-Site scheitert, `nginx -t` merkt es nicht), Portal-Cookies verworfen, Host mit Port, SSE-Tokens in keinem Log; Portal-Button gegen den nachgebauten Menü-Parser des Portals (700 Bytes, unverankerte Regexe), statische Kopie = Deployment, Besitzer von `nav/`, 0644, changed=0, `--check`, fremde/verlinkte Datei verweigert, `false` entfernt; `hosts.yaml`-Probe und Health-/Button-URLs folgen den Ports; Laufzeit-Portplan `.223:80/443` = `legacy-portal`; Bootstrap-Converge im Debian-13-Container mit geteiltem `.223` (zweiter Lauf und `--check` changed=0). `test_deploy_converge.py` angepasst (8443/8080, `hosts.yaml` 8080, nichts weiter unter `/var/www`), Lauf durch den Integrator | Live auf `.223`: `apache2` behält :80, Kontrollraum auf :8080, Button im Portal (BLOCKER-001) |
| P28 | Container/Proxy Tools | `pytest tests/*/test_infratools_*.py` (einzeln) | 239 passed, 3 live deselektiert (echtes Podman 4.9 + podman-compose 1.0.6, Docker Compose v5.6 `config`, nginx 1.24 `-t`, PostgreSQL für Rollback-Artefakte und Deployment-Claims) | `pytest -m live tests/integration/test_infratools_live.py` gegen `.222`/`.223` |
| P36.8/P39 | Abhängigkeits-Audit | `scripts/dev/dependency-audit.sh .venv <litellm-venv>` | 2026-10-14: Runtime 108 Pakete, LiteLLM-venv 117 Pakete (1.104.2 + prisma 0.15.0): keine bekannten Schwachstellen (pip-audit); UI `npm audit`: 0 | auf `.225` mit `/opt/litellm/venv` wiederholen |
| P31/P39 | UI statisch + Unit | `cd ui && npm run typecheck && npm run lint && npm run test:unit && npm run build` | 2026-10-14: tsc strict ok, ESLint 0 Warnungen, 48 Vitest-Tests, Build ok | – |
| P26 | SSH/Admin Tools | `pytest tests/*/test_sshtools_*.py` | 200 passed, 4 live deselektiert (echter sshd mit Forced-Command-Gate, Policy-Klassifikation inkl. Bypass-Regressionen BUG-076/077, Freigaben, Schlüssel) | `pytest -m live tests/integration/test_sshtools_live.py` (über `live-acceptance.sh`) |
| P31 | Web UI | `cd ui && npm run test:unit`; `pytest tests/e2e/test_ui_playwright.py` (Playwright gegen die echte API) | 48 Vitest; Playwright: 10 Spezifikationen mit 36 Tests (`ui/tests/e2e/`), über 3 pytest-Läufe | Browser gegen `.223` |
| P30 | API | `pytest tests/*/test_api*.py` | 22 passed (Routen eindeutig, Metriken, Registrierung, Scopes) | – |
| P30/BUG-140 | Reservierte Job-Metadaten | `pytest tests/unit/test_job_metadata.py`, `pytest tests/integration/test_api_job_metadata.py` | 31 + 27 passed (Registry, Schreibweisen/Präfixe, 422 ohne Schreibzugriff, Prüfung vor der Repository-Auflösung, Control-Token ohne SSH-Freigabe, Guard-Scan von `hermclaw/` inkl. der SSH-Freigabe-Leser) | – |
| Live-Abnahme (Kapitel 12) | Echte Hosts .222/.223/.224/.225/.226 | `sudo scripts/ops/live-acceptance.sh --suite workers --suite models --suite repo_intelligence --suite research --suite sshtools --suite infratools --suite wol --suite gitops` (.225, GitLab: `local-ai/hermclaw-test`); `sudo scripts/ops/live-acceptance.sh --suite sandbox` (.222) | 2026-10-16: doctor 19 ok; workers 3, repo_intelligence 1, research 2, sshtools 4, infratools 4 (1 Skip: `nginx -t` ohne Sudo-Recht auf .223, R-038), wol 3 (2 Skips: WoL deaktiviert, `--full-wol` nicht ausgeführt), sandbox 25/25 PASS; models 5/6 → nach Deployment 0.1.0rc2 6/6 (BUG-150); gitops FAIL (BLOCKER-003) → nach Freigabe PASS. Erneuter erster Testauftrag (rc2, Job `eadf1b6f…`): Erstplanung ok, Replan gescheitert → BUG-153–157 (Regressionstests: `test_planner_replan.py::test_replan_reports_rule_and_scope_errors_in_one_repair_turn`, `test_sandbox_images.py::test_empty_repository_gets_the_toolchain_once_it_has_code`, `test_runtime_driver.py::test_failed_step_keys_put_the_cause_before_its_dependents`, `test_planner_prompt.py::test_prompts_state_that_the_sandbox_cannot_install_packages`). Backup-Suite und `--full-wol` bewusst nicht ausgeführt Testauftrag auf rc3 (Job `c04f9fb8…`): BUG-158/159 (`test_scope_engine.py::test_declared_new_file_left_in_the_work_tree_stays_creatable`, `test_planner_loop.py::test_model_facing_schemas_require_the_scope_fields`). Testauftrag auf rc4 (Job `bec4c5e9…`): BUG-160/161 (`test_sandbox_command_line.py::test_engine_exec_returns_when_the_engine_exits_even_if_a_daemon_keeps_the_pipes`, `test_egress_live.py::test_live_install_keeps_the_image_test_toolchain`). Testauftrag auf rc5 (Job `b0080372…`): BUG-162 (`test_tools_engine_exec.py::test_package_installs_in_commands_are_refused_without_running`). Testauftrag auf rc6 (Job `e3cab55e…`): BUG-163/164 (`test_scope_engine.py::test_uncommitted_repo_hint_target_stays_creatable`, `test_tools_engine_exec.py::test_a_command_that_timed_out_is_not_rerun_on_an_unchanged_workspace`). Testauftrag auf rc7 (Job `837dcd50…`): BUG-165 (`test_tools_engine_exec.py::test_reinstalling_an_unchanged_manifest_is_refused_and_missing_modules_are_named`). Ursachenanalyse aller Jobs (rc8, Job `0a98a5eb…`): BUG-166–169 (`test_verifier_engine.py::test_file_name_globs_in_diff_evidence_match_in_any_directory`, `test_planner_validation.py` (Recherche, Code+Test, must_change), `test_planner_loop.py::test_live_first_plan_is_repaired_into_one_step_without_web_research`, live .222: `test_egress_live.py::test_live_project_package_is_importable_with_plain_pytest`, sandbox 29/29) | Backup (.60), volles WoL, Auftragsbatterie J1–J6 nach Deployment 0.1.0rc9 |
| Lokale Gesamtsuite auf .225 | Regression der Live-Abnahme-Fixes | `pytest --ignore=tests/load tests` mit `HERMCLAW_TEST_ADMIN_URL` der Abnahme-Testrolle | 3137 passed, 146 skipped; 57 Fehler vorbestehend und umgebungsbedingt (identisch am Ausgangsstand 8307611: kein PostgreSQL-Superuser für DB-/Backup-Tools, kein root für `chown`, fehlende Container-Images/Go/Cargo); keine Regression durch die Fixes | volle Suite in der Build-Umgebung |
| Live-Auftragsbatterie (Ursachenanalyse 2026-10-16, §5) | Echte Aufträge J1–J6 gegen die echten Modelle, gemessene Kriterien | `scripts/ops/acceptance-fixtures.sh <repo-url>`; `hermclaw acceptance-jobs /etc/hermclaw/acceptance-jobs.yaml` (Spezifikation `config/acceptance-jobs.example.yaml`); Einzeljob: `hermclaw job-metrics <id>`; Tests: `tests/integration/test_acceptance_jobs.py` 4/4 | Kennzahlen der bisherigen Jobs rc6/rc8 reproduzieren die Analyse (alle Kriterien außer complete_step verfehlt) | Erster Lauf nach Deployment 0.1.0rc9; Abnahme: zwei Läufe in Folge bestanden |
| P43 Abhängigkeits-Installation (D-014) | Egress-Proxy, Gateway, Sandbox-Kommandozeile, Werkzeug, live auf .222 | `pytest tests/unit/test_egress.py tests/integration/test_tools_engine_exec.py -k install`; live: `sudo scripts/ops/live-acceptance.sh --suite sandbox` (.222, enthält `tests/integration/test_egress_live.py`) | 2026-10-16: unit 18/18, Werkzeug 2/2; live .222 27/27 (pip + npm über den Proxy, example.com 403, Direktverbindung unreachable, /deps read-only); nach BUG-160/161 28/28 (Pin gegen die Image-Toolchain scheitert, Test-Toolchain bleibt lauffähig) | Testauftrag mit eingeschalteter Installation nach Deployment 0.1.0rc9 |
