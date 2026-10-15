# Bekannte Einschränkungen – Hermclaw Next 0.1.0rc1

Stand 2026-10-15. Jede Einschränkung nennt ihre Auswirkung und den Umgang damit. Offene Bugs gibt es nicht
(`BUGS.md`). Risiken mit Bewertung stehen in `RISK_REGISTER.md`.

## 1. Nicht live verifiziert (BLOCKER-001)

Die Ziel-Hosts (192.168.178.0/24) waren aus der Build-Umgebung nicht erreichbar. Alles ist lokal gegen echte
Dienste getestet: PostgreSQL, Podman, podman-compose, nginx, Git, LiteLLM, ffmpeg, sshd, rsync, Ansible-Converge
in Debian-13-Containern. Die Schritte, die echte Hardware oder das LAN brauchen, sind in `BUILD_PLAN.md` mit `[~]`
markiert:

* NVIDIA-Treiber/GPU-Nutzung durch Ollama auf der GTX 1080, Modell-Downloads, Token-Raten;
* Wake-on-LAN und Suspend der Worker;
* Pushes und Merge Requests gegen GitLab .226, Backups nach Terra .60;
* Deployment-Playbook gegen die echten Hosts, Soak-Test auf der Zielhardware.

**Umgang:** Nach der Installation `scripts/ops/live-acceptance.sh` ausführen (INSTALLATION.md, Kapitel 12). Erst
wenn alle Suiten grün sind, ist das System abgenommen.

## 2. Modelle und Hardware

* **GTX 1080 (8 GB):** Die 26–30B-Modelle laufen nur zu etwa 30–35 % auf der GPU, der Rest liegt im RAM. Planner,
  Coder und Reviewer sind deshalb langsam (geschätzt einstellige Token/s; gemessen wird erst bei der Live-Abnahme).
  Es ist immer nur ein großes Modell geladen (Lease `large-model-224`). Modellwechsel kosten Ladezeit
  (`OLLAMA_LOAD_TIMEOUT=10m`).
* **Embedding:** `embeddinggemma:300m` (2048 Token Kontext) statt EmbeddingGemma 2 (D-010). Chunks bleiben unter
  ≈ 1 500 Token. Der Wechsel auf EmbeddingGemma 2 ist eine reine Konfigurationsänderung, sobald Ollama das Modell
  unter Linux/CUDA 12 ausführt.
* **NVIDIA-Treiber per Runfile (D-011):** Updates nur innerhalb von 580.x und nie auf 590+ (dort fehlt Pascal).
  Nach Kernel-Updates `nvidia-smi` prüfen (DKMS baut neu). Secure Boot muss aus sein oder der DKMS-Schlüssel per
  MOK eingeschrieben werden.
* **NVENC:** kein AV1 auf Pascal; HEVC nur ohne B-Frames (`-bf 0`).

## 3. Funktionale Grenzen

* **DB-Tools:** MySQL-Ziele werden angenommen, aber als nicht unterstützt gemeldet. Produktionsziele sind immer
  read-only. Django/knex/Laravel/Prisma/Rails-Migrationen laufen nur mit explizitem Kommando (Bibliotheks-API). Bei
  Alembic-Merge-Historien wird der Downgrade nur über erneutes Upgrade geprüft.
* **Container-Tools:** Ohne laufenden Docker-Daemon laufen Compose-Sandboxes über Podman/podman-compose. Eine reine
  Docker-Präferenz wird als „skipped“ gemeldet. Die Compose-Sandbox erzeugt eine vollständige generierte Compose-Datei
  statt eines Overrides (Compose führt `ports` additiv zusammen).
* **SSH-Tools:** Die Klassifikation ist statisch: Shell-Startdateien und Glob-Expansion auf dem Zielhost werden nicht
  modelliert. Netzwerkbefehle ohne ssh (z. B. `curl`) gelten als `mutate` und brauchen eine Freigabe. Lesende
  Befehle sehen alles, was der Admin-Benutzer lesen darf. Das optionale Forced-Command-Gate braucht eine Allowlist,
  die auch die Befehle der Container-Tools erlaubt.
* **Scope:** `ScopeContract` kennt keine Operationen pro Pfad. Die Einschränkung auf vorgesehene Löschungen setzt
  das Audit durch, nicht die Prüfung zur Werkzeugzeit.
* **Git:** Lokale `file://`-Repositories darf nur ein Admin-Token registrieren. Ein vorhandener aktiver Workspace
  eines Jobs wird auch dann wiederverwendet, wenn ein anderer Basis-Branch angefragt wird.
* **Web-UI:** Der globale Dashboard-Stream hat kein REST-Gegenstück. Ein stiller Stream wird nur über die
  regelmäßigen Zähler-Abfragen bemerkt.
* **Wake-on-LAN:** Ein Idle-Suspend kann mit einem gleichzeitigen Weckvorgang kollidieren. Der Schritt scheitert dann
  mit `SSH_TIMEOUT` und wird vom Scheduler-Retry korrekt geweckt.

## 4. Betrieb

* **Ein Orchestrator, eine PostgreSQL-Instanz:** keine Hochverfügbarkeit. Ausfall von .225 = Ausfall von Hermclaw;
  Wiederherstellung über das Backup (Kapitel 11, `docs/operations/backup-restore.md`).
* **Credentials sind an den Host gebunden:** Die verschlüsselten systemd-Credentials lassen sich nur auf dem Host
  (bzw. mit dessen TPM) entschlüsseln. Nach einem Totalverlust werden sie aus dem Passwortmanager neu angelegt. Bewahren
  Sie GitLab-Token, LiteLLM-Master-Key, API-Admin-Token und Backup-Manifest-Key dort auf.
* **Sicherheitsupdates der Distribution:** Debian 13 liefert podman 5.4.2 und nginx 1.26.3 mit offenen CVEs
  (Research R-028, RISK R-030). Hermclaw entschärft sie (rootless, Image-Gate, nginx ohne `alias`+`rewrite`/AIO/HTTP3),
  ersetzt aber keine Updates: `unattended-upgrades` aktiv lassen.
* **LiteLLM:** Ohne Datenbank (unser Betrieb) meldet der Proxy Auth-Fehler als 400/500. Hermclaw behandelt sie als
  Authentifizierungsfehler (BUG-097). Jedes Versions-Update erst nach Advisory-Prüfung und den empirischen
  Proxy-Tests (`tests/integration/test_models_litellm_proxy.py`).
* **tree-sitter-Grammatiken** lädt `tree-sitter-language-pack` ≥ 1.0 beim ersten Gebrauch aus dem Internet. Das
  Release bringt die Grammatiken mit (`/opt/hermclaw/current/tree-sitter`, Prüfsummen in `SHA256SUMS`), die Dienste
  laden nichts nach. Fehlen sie trotzdem, indiziert Hermclaw JS/TS/PHP ohne Symbolstruktur (BUG-109).
* **Offline-Releases:** Abhängigkeiten werden beim Bauen des Release eingefroren (Hash-Lock). Sicherheitsupdates von
  Python-Paketen erreichen die Hosts nur mit einem neuen Release: monatlich `scripts/dev/dependency-audit.sh`, bei
  Befunden neu bauen und ausrollen (RISK R-032).
* **Rollback über Schema-Migrationen** stoppt standardmäßig; ein Downgrade braucht ein Backup und
  `-e hermclaw_orchestrator_allow_schema_downgrade=true` (RISK R-033, `docs/runbooks/upgrade.md`).
* **Restore-Test:** Die Dienstrolle `hermclaw` hat `CREATEDB`, damit der wöchentliche Restore-Test seine Wegwerf-
  Datenbank anlegen kann (RISK R-034).
