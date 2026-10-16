# CHANGELOG

Format: [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionen nach PEP 440.

## [0.1.0rc10] – Release Candidate (2026-10-16)

Fix aus dem ersten Lauf der Live-Auftragsbatterie (J1 auf 0.1.0rc9: Plan und Umsetzung korrekt, Verifier-Ablehnung
wegen fehlendem Test-Kriterium, BUG-170).

### Behoben
- **Test-Kriterium (BUG-170):** Umsetzungs-Steps, die Tests schreiben oder in einem Repository mit Tests arbeiten,
  erhalten automatisch ein Test-Kriterium; Plan-Defekte gehen ohne Korrekturversuch an den Replanner; doppelte
  Akzeptanzkriterien werden zusammengefasst.

## [0.1.0rc9] – Release Candidate (2026-10-16)

Ursachenanalyse aller Live-Jobs (Ursachenanalyse `docs/analysis/live-job-root-causes-2026-10-16.md`) und allgemeine Fixes der Ursachen (BUG-166 bis BUG-169) statt weiterer
Symptom-Fixes. Neue Live-Abnahme mit gemessenen Kriterien.

### Behoben
- **Verifier-False-Negative (BUG-166):** `must_change: ["*"]` und andere Dateinamen-Globs passen in jeder Tiefe;
  die Plan-Validierung weist `must_change`-Globs zurück, die auf keinen Pfad des Steps passen können.
- **Projekt-Importe (BUG-167):** Das Repository-Wurzelverzeichnis steht in jedem Container auf `PYTHONPATH`.
- **Recherche (BUG-168):** Fragen über das eigene Repository und zur Technologiewahl werden in Plan und Coder-Werkzeug
  abgelehnt (Web-Recherche kann sie nicht beantworten); externe Fragen bleiben erlaubt.
- **Planstruktur und Einfachheit (BUG-169):** Code und seine Tests gehören in einen Step (Validierung); Planner und
  Coder wählen die einfachste vollständige Lösung (Standardbibliothek und vorhandene Werkzeuge zuerst).

### Hinzugefügt
- `hermclaw job-metrics JOB_ID` und `hermclaw acceptance-jobs SPEC`: Live-Auftragsbatterie mit gemessenen Kriterien
  (Status/MR, Dauer, Replans, Erstplan-Größe, unbeantwortbare Recherche, neue Abhängigkeiten, Wiederholungen,
  `complete_step`); Spezifikation `config/acceptance-jobs.example.yaml`, Fixtures `tests/live/fixtures/`,
  `scripts/ops/acceptance-fixtures.sh`.

## [0.1.0rc8] – Release Candidate (2026-10-16)

Fix aus dem Testauftrag nach dem Deployment von 0.1.0rc7 (BUG-165).

### Behoben
- **Leerlauf-Installationen (BUG-165):** `install_dependencies` lehnt ein unverändertes Manifest ab (`INSTALL_UNCHANGED`);
  scheitert ein Lauf an einem fehlenden Modul, nennt die Ausgabe es mit dem Weg ins Manifest. Coder-Vertrag: Step
  abschließen, sobald sein Ziel erreicht ist, auch wenn Tests außerhalb des Scopes noch scheitern.

## [0.1.0rc7] – Release Candidate (2026-10-16)

Fixes aus dem Testauftrag nach dem Deployment von 0.1.0rc6 (BUG-163, BUG-164).

### Behoben
- **Liegengebliebene Datei als Repo-Hinweis (BUG-163):** Ein Step, dessen Ziel eine uncommittete Datei eines
  abgebrochenen Vorgängers ist, darf sie wieder schreiben; bisher wurde jede Änderung als „create“ abgelehnt.
- **Server blockieren Tests (BUG-164):** Ein Timeout erklärt, dass Befehle von selbst enden müssen; derselbe Befehl
  wird auf unverändertem Arbeitsbaum nicht erneut für den vollen Timeout gestartet. Coder-Vertrag: keine Server,
  Watcher oder REPLs; Tests starten die App im Prozess und schließen sie.

## [0.1.0rc6] – Release Candidate (2026-10-16)

Fix aus dem Testauftrag nach dem Deployment von 0.1.0rc5 (BUG-162).

### Behoben
- **Paket-Installation im Befehl (BUG-162):** `pip install`, `npm install` & Co. in `run_command`/`run_test` werden ohne
  Netz sofort abgelehnt (`PACKAGE_INSTALL_REFUSED`) und verweisen auf `install_dependencies`; vorher liefen sie in
  einen Netz-Timeout und der Coder stagnierte.

## [0.1.0rc5] – Release Candidate (2026-10-16)

Fixes aus dem Testauftrag nach dem Deployment von 0.1.0rc4 mit eingeschalteter Abhängigkeits-Installation (BUG-160, BUG-161).

### Behoben
- **Egress-Proxy-Start (BUG-160):** Der erste Start des Proxy-Containers lief in den 300-s-Timeout, obwohl der
  Container lief (ein Podman-Hintergrundprozess hielt die Ausgabe-Pipes offen). Engine-Hilfsbefehle kehren jetzt
  zurück, sobald der Engine-Prozess beendet ist.
- **Installation und Test-Toolchain (BUG-161):** `install_dependencies` (Python) beginnt mit leerem `/deps/python`
  und hält die Pakete des Sandbox-Images auf deren Versionen (pip-Constraints). Ein Pin wie `pytest==7.4.3` scheitert
  jetzt verständlich, statt pytest und seine Plugins zu brechen. Coder-Vertrag: fehlendes Modul zuerst ins Manifest,
  Test-Werkzeuge des Images nicht pinnen.

## [0.1.0rc4] – Release Candidate (2026-10-16)

Fixes aus dem ersten Testauftrag nach dem Deployment von 0.1.0rc3 (BUG-158, BUG-159) und Abhängigkeits-Installation (D-014).

### Neu
- **Abhängigkeiten installieren (D-014):** Werkzeug `install_dependencies` für den Coder (pip/npm aus dem Manifest,
  Befehl von der Runtime gebaut). Netz nur für diesen Befehl über den Proxy-Container `hermclaw-egress-proxy` auf
  .222, der nur `pypi.org`, `files.pythonhosted.org` und `registry.npmjs.org` erreicht; Pakete pro Workspace unter
  `/deps` (für alle anderen Befehle read-only). Standard aus, Inventar-Schalter `hermclaw_config_dependency_installs`.

### Behoben
- **Scope (BUG-158):** Eine als neu deklarierte Datei, die ein abgebrochener Versuch im Work Tree hinterlassen hat,
  bleibt anlegbar (vorher nur „modify“ erlaubt, Schreibprüfung verlangte „create“ → Stagnation).
- **Planner-Schema (BUG-159):** Das Schema für Gemma verlangt `depends_on`, `repo_hints`, `allowed_new_paths` und
  `acceptance` an jedem Step; übersprungene Scope-Felder waren im Replan nicht mehr nachtragbar.

## [0.1.0rc3] – Release Candidate (2026-10-16)

Fixes aus dem erneuten ersten Testauftrag nach dem Deployment von 0.1.0rc2 (BUG-153–157).

### Behoben
- **Replanner (BUG-153, BUG-154):** Eine Repair-Runde meldet Replan-Regel-, Schema- und Scope-Fehler gemeinsam
  (vorher eine Fehlerklasse pro Runde, Repair-Budget erschöpft); `rerun_reason` an nicht abgeschlossenen Steps wird
  ignoriert statt abgelehnt.
- **Sandbox-Image (BUG-155):** Ein leeres Repository bekommt das Toolchain-Image, sobald es Code enthält.
- **Replan-Trigger (BUG-156):** Der fehlgeschlagene Step ist die Ursache, nicht ein nur abhängig blockierter Step.
- **Prompts (BUG-157):** Planner und Coder wissen, dass Code-Steps offline mit fester Toolchain laufen und keine
  Pakete installieren können.

## [0.1.0rc2] – Release Candidate (2026-10-16)

Fixes aus der Live-Abnahme auf den Ziel-Hosts (INSTALLATION.md Kapitel 12, BUG-143–152).

### Behoben
- **Planner (BUG-143):** Pläne mit schreibenden Steps ohne `repo_hints`, `allowed_new_paths` oder Presence-Evidence
  werden beim Planen abgelehnt (vorher `SCOPE_UNAVAILABLE` zur Laufzeit, erster Testauftrag gescheitert); die Meldung
  für Prüf-Steps ohne Abhängigkeit nennt Kandidaten-IDs.
- **Planner-Prompt (BUG-144):** Web-Research nur für externe Fakten, nicht für das eigene Repository.
- **Modelle (BUG-150):** optionales Profilfeld `gpu_layers` (Ollama `num_gpu`); `planner-gemma-fallback` lädt mit
  32K auf der GTX 1080 nur mit 30 GPU-Layern.
- **Git (BUG-152):** abgelehnte Pushes nennen die Begründung des Servers.
- **Sandbox (BUG-147):** Docker-Adapter unter rootless Engines.
- **Deployment (BUG-149):** `podman-compose` auf dem Execution Worker.
- **Abnahme/Tests (BUG-145, -146, -148, -151):** Dev-Extra mit `aiohttp`, Sandbox-Suite prüft alle Podman-Tests,
  SKIP statt PASS bei vollständig übersprungenen Suiten, SearXNG-URL aus der Konfiguration, WoL-Test ohne WoL.

## [0.1.0rc1] – Release Candidate (2026-10-15)

Erster vollständiger Aufbau von Hermclaw Next (Greenfield, Bauplan v2, Phasen P00–P42). Lokal vollständig
getestet; die Live-Abnahme auf den Ziel-Hosts steht aus (BLOCKER-001, `scripts/ops/live-acceptance.sh`).

### Hinzugefügt – Kern (P02–P25)

- **Backend und Persistenz:** FastAPI-App, typisierte YAML-Konfiguration (`hosts`, `models`, `policies`,
  `capabilities`, `logging`, optional `databases`, `backup`), Settings über `HERMCLAW_*`, PostgreSQL 17 + pgvector
  mit Alembic-Migrationen (im Wheel unter `share/hermclaw`), Event Store mit fortlaufender Sequenz und SSE-Replay
  (`Last-Event-ID`), Zustandsautomaten für Jobs, Schritte und Versuche.
- **Git Engine:** Runtime-kontrollierte Git-Mutationen (Workspaces pro Job, Job-Branches `hermclaw/*`, geschützte
  Branches, Push, Merge Requests über die GitLab-API, Locks, Wiederaufnahme nach Abbruch).
- **Worker-Protokoll:** Execution-, Model- und Media-Worker-Daemons mit Bearer-Token + HMAC-Signatur,
  Heartbeats, Protokollversion, Workspace-Sync per Tar-Stream.
- **Model Gateway:** LiteLLM-Proxy (gepinnt `litellm[proxy]==1.104.2`) vor Ollama, feste Modellrollen
  (Gemma-Planner, Qwen3-Coder-30B-Worker, Qwen3.8-27B-Reviewer, Qwen3-8B-Router, EmbeddingGemma), strukturierte
  Ausgaben über JSON-Schema, technische Fallbacks nur bei Infrastrukturfehlern, Residenzsteuerung.
- **Resource Manager, Wake-on-LAN und Idle-Sleep:** PostgreSQL-Leases (exklusiv für große Modelle), Weckpipeline
  (Magic Packet → Ping → SSH → API → Dienste), Leerlauf-Suspend der Worker.
- **Repository Intelligence:** Inventar, lexikalische Suche (ripgrep), Symbole (Python-AST, tree-sitter für
  JS/TS/TSX/PHP), Abhängigkeiten, semantischer Index (pgvector), Ranking-Fusion.
- **Research Engine:** SearXNG-Suche, sicheres Fetching, Quellenbewertung, Zitate im Abschlussbericht.
- **Planner/Replanner (Gemma):** Plan-Vertrag mit Schritten, Abhängigkeiten, Capabilities und maschinell
  prüfbaren Akzeptanzkriterien; Reparaturschleife; Neuplanung nach Fehlschlägen.
- **Scope Engine:** expliziter Scope pro Schritt, ScopeGuard für jeden Dateizugriff, Scope-Erweiterungsanträge,
  Audit des Diffs inkl. Umbenennungen.
- **Context Builder, Tool Engine, Coder-Loop:** frischer Kontext pro Runde, Werkzeuge als einziger Weg in den
  Workspace (Dateien, Befehle, Tests, Ops-Tools), Stagnationserkennung, Abschluss- und Blockade-Berichte.
- **Deterministic Verifier und Heavy Review:** generische Prüfungen (Tests, Befehle, Präsenz, Diff, Scope, Schema,
  Secrets, Artefakte) und Review jedes Schritts vor dem Commit; Korrektur-Pipeline.
- **Scheduler und Runtime:** Composition Root, `hermclaw scheduler`/`hermclaw api`, Recovery nach Neustart,
  Scratch-Workspaces für repository-lose Ops-Jobs.

### Hinzugefügt – Betrieb und Werkzeuge (P26–P33)

- **SSH/Admin-Tools (P26):** Host-Allowlist, gepinnte Host-Keys, Klassifikation read/mutate/destructive/forbidden,
  Einmal-Freigaben durch Admins (`/api/jobs/{id}/ssh-approvals`), Forced-Command-Gate `hermclaw-ssh-gate`.
- **DB-Tools (P27):** Introspektion und Read-only-Abfragen auf Projektdatenbanken, Migrationen nur in
  Wegwerf-Sandbox-Datenbanken (Alembic, Roh-SQL, Upgrade/Downgrade/Upgrade), Backups und Restore-Tests.
- **Container/Proxy-Tools (P28):** Container-Inventar, Compose-Validierung und Compose-Sandbox (127.0.0.1-Ports,
  Healthchecks, garantierter Abbau), Portkollisionen, `nginx -t` mit Proxy-Regeln (SSE), Rollback-Artefakte.
- **Media (P29):** ffmpeg/NVENC-Video und ComfyUI-Bildadapter des Media-Workers.
- **API (P30) und Web-UI (P31):** REST + SSE, Token-Scopes read/control/admin, React/TypeScript-Oberfläche mit
  Plan-DAG, Live-Status ohne Chain-of-Thought, Diffs, Freigaben, Artefakten; Playwright-Tests.
- **Deployment (P32):** Ansible-Bootstrap (P01) für Debian 13 auf allen Hosts (PostgreSQL 17 aus PGDG, rootless
  Podman mit cgroup-Delegation, Node.js 22, nginx, NVIDIA R580 per Runfile/DKMS, Ollama 0.40.4) und Deployment-
  Playbook `deploy.yml`: Offline-Release (`scripts/deploy/build-release.sh`: CPython-3.13-Wheelhouse mit Hash-Lock,
  UI-Build, tree-sitter-Grammatiken, `SHA256SUMS`, Selbsttest), versionierte venvs mit atomarem Symlink-Wechsel,
  Konfiguration aus den Beispielen + Inventar mit `config-check` vor dem Einspielen, Schema-Entscheidung vor jeder
  Live-Änderung, Migration einmal vor dem Neustart, LiteLLM-Gate (lebendig und authentifiziert), Health-Gates,
  Upgrade/Rollback mit Schema-Downgrade-Opt-in, Prune nach Aktivierungsreihenfolge, Trockenlauf ohne Änderungen,
  Operator-CLI `sudo hermclaw …`; `hermclaw doctor` als Installations-Preflight.
- **Backup/Restore (P33):** konsistenter `pg_dump`, Konfiguration, Artefakte, signiertes Manifest, rsync nach
  Terra (.60), Retention, wöchentlicher Restore-Test in eine Wegwerf-Datenbank (Units und Restore-Template vom
  Deployment eingerichtet), Disaster-Recovery `restore-full`.

### Hinzugefügt – Qualität (P34–P42)

- Recovery-, Failure-Injection-, Security-, E2E- (15 Generalisierungs-Szenarien), Last- und Soak-Tests;
  Testmatrix Datei für Datei (`scripts/dev/test_matrix.py`); Cutover-Plan; Live-Abnahmeskript
  `scripts/ops/live-acceptance.sh`; vollständige Installationsanleitung `docs/operations/INSTALLATION.md`.

### Geändert (Entscheidungen, `DECISIONS.md`)

- D-010: Embedding-Modell `embeddinggemma:300m` statt EmbeddingGemma 2 (in Ollama nur auf dem MLX-Runner, nicht
  unter Linux/Pascal).
- D-011: NVIDIA-Treiber R580 über den Runfile-Installer auf `.224` (kein paketierter Treiber ≥ 570 für Pascal in
  Debian 13).
- D-012: pgvector ≥ 0.8.7 als Standard, dokumentierte Betreiber-Ausnahme ab 0.8.2, solange PGDG für trixie noch
  kein 0.8.7 liefert.
- SearXNG-Image per Tag und Digest gepinnt, `GRANIAN_HOST=0.0.0.0` (Research R-027).

### Sicherheit

- Geheimnisse nur als verschlüsselte systemd-Credentials; Redaktion in Logs, Events, Prompts und Diffs;
  Secret-Scan des Repositorys und jedes Job-Diffs.
- Sandbox: rootless, `--read-only`, `--network=none` ohne Netz-Capability, `--cap-drop=ALL`, Ressourcenlimits,
  Image-Sicherheitsgate gegen Podman CVE-2026-57231/CVE-2026-94603 (BUG-108).
- pgvector ≥ 0.8.7 (CVE-2026-3172, CVE-2026-103484; Untergrenze 0.8.2, D-012), LiteLLM ohne bekannte Advisories in der
  gepinnten Version (Research R-027).
- `POST /api/jobs` lehnt laufzeitreservierte Metadaten-Schlüssel (u. a. `ssh_approvals`, `triage`, `replan_requested`)
  mit 422 ab – ein `control`-Token kann keine SSH-Freigaben mehr vorab setzen (BUG-140, `hermclaw/core/job_metadata.py`).

### Behoben

- 142 Fehler wurden während des Aufbaus gefunden und behoben, jeweils mit Regressionstest bzw. (bei reinen
  Dokumentationsfehlern) einer Prüfung gegen den Code (`BUGS.md`, BUG-001 bis BUG-142; P0: 2, P1: 26, P2: 62,
  P3: 49, P4: 3). Es gibt keine offenen Bugs.
- Installationsanleitung und Runbooks wurden Befehl für Befehl gegen Code und Rollen geprüft (71 bestätigte Befunde,
  alle behoben; BUG-133 bis BUG-139).

### Bekannte Einschränkungen

Siehe `docs/operations/known-limitations.md`.
