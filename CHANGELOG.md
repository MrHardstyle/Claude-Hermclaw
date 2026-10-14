# CHANGELOG

Format: [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionen nach PEP 440.

## [0.1.0rc1] – Release Candidate (2026-10-14)

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
  Playbook mit versionierten venv-Releases; `hermclaw doctor` als Installations-Preflight.
- **Backup/Restore (P33):** konsistenter `pg_dump`, Konfiguration, Artefakte, signiertes Manifest, rsync nach
  Terra (.60), Retention, wöchentlicher Restore-Test in eine Wegwerf-Datenbank, Disaster-Recovery `restore-full`.

### Hinzugefügt – Qualität (P34–P42)

- Recovery-, Failure-Injection-, Security-, E2E- (15 Generalisierungs-Szenarien), Last- und Soak-Tests;
  Testmatrix Datei für Datei (`scripts/dev/test_matrix.py`); Cutover-Plan; Live-Abnahmeskript
  `scripts/ops/live-acceptance.sh`; vollständige Installationsanleitung `docs/operations/INSTALLATION.md`.

### Geändert (Entscheidungen, `DECISIONS.md`)

- D-010: Embedding-Modell `embeddinggemma:300m` statt EmbeddingGemma 2 (in Ollama nur auf dem MLX-Runner, nicht
  unter Linux/Pascal).
- D-011: NVIDIA-Treiber R580 über den Runfile-Installer auf `.224` (kein paketierter Treiber ≥ 570 für Pascal in
  Debian 13).

### Sicherheit

- Geheimnisse nur als verschlüsselte systemd-Credentials; Redaktion in Logs, Events, Prompts und Diffs;
  Secret-Scan des Repositorys und jedes Job-Diffs.
- Sandbox: rootless, `--read-only`, `--network=none` ohne Netz-Capability, `--cap-drop=ALL`, Ressourcenlimits,
  Image-Sicherheitsgate gegen Podman CVE-2026-57231/CVE-2026-94603 (BUG-108).
- pgvector ≥ 0.8.7 (CVE-2026-3172, CVE-2026-103484; Untergrenze 0.8.2, D-012), LiteLLM ohne bekannte Advisories in der
  gepinnten Version (Research R-027).

### Behoben

- 109 Fehler wurden während des Aufbaus gefunden und behoben, jeweils mit Regressionstest (`BUGS.md`, BUG-001 bis
  BUG-109). Es gibt keine offenen Bugs.

### Bekannte Einschränkungen

Siehe `docs/operations/known-limitations.md`.
