# Hermclaw Next – Installationsanleitung

Diese Anleitung führt von leeren Rechnern bis zum laufenden, abgenommenen Hermclaw-Next-System.
Sie beschreibt jeden Host einzeln, alle Konfigurationsdateien, die Sie anpassen müssen, jeden Befehl
und alle Modelle, die geladen werden.

> **Stand:** Release Candidate `0.1.0rc1`, 2026-10-14. Alle Schritte sind automatisiert (Ansible, Skripte)
> und lokal getestet. Die Ziel-Hosts waren aus der Build-Umgebung nicht erreichbar (BLOCKER-001). Deshalb
> endet die Installation mit der **Live-Abnahme** (Kapitel 12), die alle Prüfungen gegen die echten Hosts
> nachholt.

<!-- INSTALL-TOC -->

---

## 1. Überblick

### 1.1 Rollen der Rechner

Die Rollen sind durch die Architektur fest vorgegeben (Bauplan §2) und dürfen nicht umverteilt werden.

| Host | IP | Rolle | Läuft dort |
|---|---|---|---|
| Orchestrator | 192.168.178.225 | `orchestrator` | Hermclaw-API (:8000), Scheduler/Agent-Runtime, PostgreSQL 17 + pgvector, LiteLLM-Proxy (:4000, nur lokal), SearXNG (:8888, rootless Podman), Backup-Timer, alle Git-Operationen |
| Web-UI | 192.168.178.223 | `webui` | nginx: statische React-Oberfläche + Reverse-Proxy `/api` (mit SSE) zum Orchestrator |
| Execution Worker | 192.168.178.222 | `execution_workers` | `hermclaw-exec-worker` (:8787): Befehle und Tests der Jobs in rootless-Podman-Sandboxes; Wake-on-LAN |
| Model/Media Worker | 192.168.178.224 | `model_workers` | Ollama (:11434, nur für .225 offen), `hermclaw-model-worker` (:8787), ffmpeg/NVENC; NVIDIA GTX 1080; Wake-on-LAN |
| GitLab | 192.168.178.226 | `gitlab` | Ihre bestehende GitLab-Instanz (Ziel der Pushes und Merge Requests); keine Hermclaw-Software |
| Terra (Unraid) | 192.168.178.60 | `backup` | Backup-Ziel (rsync über SSH); keine Hermclaw-Software |

### 1.2 Feste Modellrollen

| Rolle | Modell (Ollama-Tag) | LiteLLM-Alias | Zweck |
|---|---|---|---|
| Planner / Replanner | `gemma4:26b` (Fallback `gemma4:12b`) | `planner-gemma` / `planner-gemma-fallback` | Plan, Schritte, Akzeptanzkriterien, Neuplanung |
| Implementation Worker | `qwen3-coder:30b` | `coder-main` | Coder-Tool-Loop (Dateien, Befehle, Tests) |
| Heavy Reviewer | `qwen3.8:27b` | `heavy-review` | Review jedes Schritts vor dem Commit |
| Fast Router | `qwen3:8b` | `fast-router` | Triage, Klassifikation, kurze Entscheidungen |
| Embedding | `embeddinggemma:300m` (768 Dimensionen) | `embedding` | semantischer Repository-Index (pgvector) |

Warum `embeddinggemma:300m` statt EmbeddingGemma 2: EmbeddingGemma 2 läuft in Ollama derzeit nur auf dem
MLX-Runner (CUDA 13) und damit weder unter Linux noch auf Pascal-GPUs (Entscheidung D-010, Research R-026).
Sobald Ollama es unter Linux/CUDA 12 ausführt, wechseln Sie in `models.yaml` (Kapitel 9.2); die Dimension
(768) bleibt gleich.

### 1.3 Ablauf der Installation

| Schritt | Kapitel | Wo | Dauer (ca.) |
|---|---|---|---|
| Hardware, BIOS, Netzwerk vorbereiten | 2 | alle Hosts | 30 min |
| Debian 13 installieren | 3 | .225, .223, .222, .224 | je 20 min |
| Admin-Rechner einrichten, Inventar ausfüllen | 4 | Ihr Admin-Rechner | 15 min |
| Bootstrap (Pakete, Benutzer, PostgreSQL, Podman, NVIDIA, Ollama …) | 5 | per Ansible | 45–90 min (inkl. zwei Neustarts von .224) |
| Zugangsdaten (GitLab-Token, Backup-Schlüssel) | 6 | .225, GitLab, Terra | 15 min |
| Modelle laden | 7 | .224 | 1–3 h (≈ 70 GB Download) |
| Release bauen und ausrollen (Deployment) | 8 | per Ansible | 20–40 min |
| Konfiguration prüfen und anpassen | 9 | .225 (`/etc/hermclaw`) | 20 min |
| Erste Inbetriebnahme (Token, Repository, erster Job) | 10 | .225 / Browser | 15 min |
| Backup einrichten | 11 | .225, Terra | 15 min |
| Live-Abnahme | 12 | .225, .222 | 30–60 min |

Reihenfolge immer: **Orchestrator zuerst** (er erzeugt SSH-Schlüssel und Worker-Tokens), dann Web-UI und
Worker. Jeder Ansible-Lauf ist idempotent: Sie können ihn jederzeit wiederholen, ein zweiter Lauf meldet
`changed=0`.

### 1.4 Ports (Bauplan §6)

| Host | Port | Dienst | Erreichbar von |
|---|---|---|---|
| .225 | 8000 | Hermclaw-API | .223 (nginx), Worker (Heartbeats), Admin |
| .225 | 4000 | LiteLLM | nur 127.0.0.1 (Scheduler) |
| .225 | 5432 | PostgreSQL | nur lokal |
| .225 | 8888 | SearXNG | lokal (Research Engine) |
| .225 | 9100 | Metriken | LAN (optional) |
| .223 | 80 / 443 | nginx (UI + `/api`) | Browser im LAN |
| .222 | 8787 | Execution-Worker-API | .225 |
| .224 | 8787 | Model-Worker-API | .225 |
| .224 | 11434 | Ollama | nur .225 und lokal (nftables-Regel der Bootstrap-Rolle) |

---

## 2. Hardware, BIOS und Netzwerk

### 2.1 Mindestanforderungen

Der Preflight (`scripts/bootstrap/check-host.sh`) prüft diese Mindestwerte und bricht darunter ab:

| Host | CPU (logisch) | RAM | freier Platz unter `/var/lib` | Empfehlung |
|---|---|---|---|---|
| .225 Orchestrator | 4 | 8 GB | 20 GB | 8 Threads, 16 GB RAM, SSD ≥ 100 GB (Repos, Workspaces, Artefakte, PostgreSQL) |
| .223 Web-UI | 1 | 1 GB | 2 GB | 2 GB RAM (für den UI-Build mit npm) |
| .222 Execution Worker | 2 | 4 GB | 30 GB | 4–8 Threads, 16 GB RAM, SSD ≥ 100 GB (Sandbox-Images, Workspaces) |
| .224 Model Worker | 4 | 16 GB | 80 GB | **32 GB RAM**, NVIDIA GTX 1080 8 GB, SSD ≥ 150 GB (≈ 70 GB Modelle + Ollama-Versionen) |

Zum RAM auf .224: Die großen Modelle passen nicht vollständig in 8 GB VRAM. Ollama lagert die übrigen
Schichten in den Arbeitsspeicher aus (Research R-026):

| Modell | Download | RAM + VRAM bei Profil-Kontext | Anteil auf der GPU |
|---|---|---|---|
| `qwen3:8b` (16K) | 5,2 GB | ≈ 6,8 GB (KV-Cache q8_0) | 100 % |
| `gemma4:26b` (32K) | ≈ 18 GB | ≈ 20 GB | ≈ 35 % |
| `gemma4:12b` (32K) | ≈ 7,6 GB | ≈ 9–11 GB | überwiegend |
| `qwen3-coder:30b` (32K) | ≈ 19 GB | ≈ 21,5 GB | ≈ 33 % |
| `qwen3.8:27b` (24K) | ≈ 18 GB | ≈ 22 GB | ≈ 30 % (dicht, langsam) |
| `embeddinggemma:300m` | ≈ 0,6 GB | ≈ 1 GB | GPU oder CPU |

Hermclaw lädt immer nur **ein** großes Modell zur Zeit (Resource Manager, exklusive Lease `large-model-224`).
Mit 16 GB RAM passen die 26–30B-Modelle nicht. 32 GB sind die sinnvolle Größe.

### 2.2 BIOS/UEFI-Einstellungen

| Host | Einstellung | Wert | Warum |
|---|---|---|---|
| .222, .224 | Wake-on-LAN (oft „Power On By PCI-E“, „Wake on LAN“, „ErP“ aus) | **an**, ErP/Deep-Sleep **aus** | Hermclaw legt die Worker bei Leerlauf schlafen und weckt sie per Magic Packet |
| .222, .224 | Suspend-to-RAM (S3) | erlaubt | `systemctl suspend` als Leerlauf-Aktion |
| .224 | Secure Boot | **aus** | Der NVIDIA-Treiber wird per DKMS gebaut und ist unsigniert. Alternative: DKMS-MOK-Schlüssel einschreiben (Kapitel 5.6) |
| .224 | Primäre Grafik | PCIe/GTX 1080 oder iGPU, egal | keine Desktop-Nutzung nötig |
| alle | Virtualisierung (VT-x/AMD-V) | an (empfohlen) | rootless Podman braucht es nicht, schadet aber nicht |

### 2.3 Netzwerk

1. Feste IP-Adressen gemäß Tabelle 1.1 vergeben, am einfachsten als DHCP-Reservierung im Router (Fritz!Box:
   *Heimnetz → Netzwerk → Gerät → „Diesem Netzwerkgerät immer die gleiche IPv4-Adresse zuweisen“*).
2. Notieren Sie die **MAC-Adresse** der kabelgebundenen Netzwerkkarte von .222 und .224 (nach der Debian-
   Installation: `ip -br link`). Sie wird für Wake-on-LAN gebraucht. Wake-on-LAN funktioniert nur per Kabel.
3. Broadcast-Adresse des LANs: in einem /24-Netz `192.168.178.255`.
4. Die Hosts brauchen Internetzugang (apt, PGDG, NVIDIA-Treiber, Ollama-Release, Modelle, PyPI, npm, Container-
   Images). Nach der Installation brauchen nur noch die Research Engine (über SearXNG) und neue Modell-Downloads das
   Internet.

---

## 3. Debian 13 installieren (.225, .223, .222, .224)

Auf jedem der vier Hermclaw-Hosts:

1. Debian 13 „trixie“ amd64 installieren (netinst-Image genügt). In der Softwareauswahl **nur** „SSH server“
   und „Standard-Systemwerkzeuge“ wählen (keine Desktop-Umgebung).
2. Einen persönlichen Admin-Benutzer mit sudo-Recht anlegen, im Beispiel `admin`. **Nicht** `hermclaw-admin`
   nennen: Diesen Benutzer legt der Bootstrap als eingeschränktes Runtime-Konto an.
3. Nach dem ersten Start auf der Konsole:

   ```bash
   sudo apt update && sudo apt full-upgrade -y
   ip -br addr                                     # IP prüfen (Tabelle 1.1)
   ip -br link                                     # MAC notieren (.222/.224)
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub   # Fingerabdruck notieren (Schritt 4.2)
   ```

4. Ihren öffentlichen SSH-Schlüssel des Admin-Rechners hinterlegen (vom Admin-Rechner aus):

   ```bash
   ssh-copy-id admin@192.168.178.225     # ebenso .223, .222, .224
   ```

   Vergleichen Sie beim ersten Verbinden den angezeigten Fingerabdruck mit dem auf der Konsole notierten.
   `ansible.cfg` arbeitet mit `StrictHostKeyChecking=yes`.

5. Optional, wenn sudo ein Passwort verlangt: Ansible fragt es mit `-K` ab (alle Befehle unten).

GitLab (.226) und Terra (.60) werden **nicht** neu installiert. Sie brauchen nur ihre SSH-Host-Schlüssel
(Kapitel 4.3).

---

## 4. Admin-Rechner (Ansible-Steuerrechner)

Ein beliebiger Linux- oder macOS-Rechner mit `git`, `ssh` und Python 3.12–3.14 (ansible-core 2.21, Research R-029). Sie können
dafür auch den Orchestrator .225 selbst verwenden.

### 4.1 Repository holen und Ansible einrichten

```bash
git clone https://github.com/MrHardstyle/Claude-Hermclaw.git hermclaw-next
cd hermclaw-next
git checkout v0.1.0rc1            # oder der Release-Branch/Tag, den Sie installieren
scripts/bootstrap/bootstrap-control-node.sh
#   * installiert ansible-core (>=2.21.5,<2.22, research R-029) in ~/.local/share/hermclaw/ansible-venv
#   * legt infra/ansible/inventory/hosts.yml aus hosts.example.yml an (überschreibt nie)
#   * prüft das Inventar und alle Playbooks (Syntax)
export ANSIBLE_CONFIG=$PWD/infra/ansible/ansible.cfg
export PATH=$HOME/.local/share/hermclaw/ansible-venv/bin:$PATH
```

### 4.2 Inventar anpassen: `infra/ansible/inventory/hosts.yml`

| Variable | Wo | Wert / Beispiel | Bedeutung |
|---|---|---|---|
| `ansible_user` | `all.vars` | `admin` | Ihr sudo-Benutzer auf den Hosts |
| `hermclaw_version` | `all.vars` | `0.1.0rc1` | Version, die ausgerollt wird (Kapitel 8) |
| `hermclaw_orchestrator_ip` | `all.vars` | `192.168.178.225` | IP des Orchestrators |
| `hermclaw_lan_broadcast` | `all.vars` | `192.168.178.255` | Broadcast für Wake-on-LAN |
| `ansible_host` | je Host | IPs aus Tabelle 1.1 | |
| `hermclaw_web_server_name` | `hermclaw-webui` | `hermclaw.lan` | nginx `server_name` (`_` = Catch-all, nur wenn sonst keine Default-Site existiert) |
| `hermclaw_worker_id` | Worker-Hosts | `exec-222`, `model-224` | Worker-IDs, müssen zu `hosts.yaml` passen |
| `wol_mac` | `.222`, `.224` | `3c:7c:3f:12:34:56` | **echte MAC** aus Schritt 2.3 (der Platzhalter `00:00:00:00:00:00` wird von `hermclaw doctor` gemeldet) |
| `wol_interface` | `.222`, `.224` | leer oder `enp3s0` | leer = Schnittstelle der Default-Route |
| `common_known_hosts_extra` | `hermclaw-orchestrator` | siehe unten | SSH-Host-Schlüssel von GitLab und Terra |
| `nvidia_secureboot_mok_enrolled` | `hermclaw-model-224` | `false` | nur `true`, wenn Sie Secure Boot anlassen und den DKMS-Schlüssel eingeschrieben haben |

### 4.3 SSH-Host-Schlüssel von GitLab und Terra

Holen Sie die Schlüssel und vergleichen Sie den Fingerabdruck mit der Konsole des jeweiligen Systems
(`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`):

```bash
ssh-keyscan -t ed25519 192.168.178.226 192.168.178.60
```

und tragen Sie sie beim Orchestrator ein:

```yaml
    orchestrator:
      hosts:
        hermclaw-orchestrator:
          ansible_host: 192.168.178.225
          common_known_hosts_extra:
            - "gitlab.lan,192.168.178.226 ssh-ed25519 AAAAC3Nz..."
            - "192.168.178.60 ssh-ed25519 AAAAC3Nz..."
```

### 4.4 Inventar prüfen und Verbindung testen

```bash
scripts/bootstrap/bootstrap-control-node.sh          # erneut: validiert, warnt vor Platzhalter-MACs
cd infra/ansible
ansible -i inventory/hosts.yml 'all:!gitlab:!backup' -m ansible.builtin.ping -K
```

---

## 5. Bootstrap der Hosts (P01)

Der Bootstrap installiert Pakete, legt Benutzer und Verzeichnisse an, richtet PostgreSQL, Podman, Node.js,
nginx, den NVIDIA-Treiber und Ollama ein und installiert die systemd-Units (noch **deaktiviert**; sie werden
im Deployment, Kapitel 8, aktiviert). Ausführliche Prüfschritte: `docs/operations/bootstrap.md`.

Alle Befehle im Verzeichnis `infra/ansible` auf dem Admin-Rechner.

### 5.1 Preflight (nur lesend)

```bash
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --tags preflight -K
```

Exit-Code 0 = in Ordnung (Warnungen erlaubt). Typische Abbrüche:

| FAIL | Bedeutung | Abhilfe |
|---|---|---|
| `os` / `arch` | kein Debian 13 / kein x86_64 | Host neu installieren |
| `port:<n>` | ein fremder Dienst belegt einen geplanten Port | diesen Dienst verlegen/stoppen |
| `unit:<name>` | eine fremde Unit mit Hermclaw-Namen existiert | umbenennen/entfernen |
| `ram` / `cpu` / `disk` | unter dem Minimum (Tabelle 2.1) | aufrüsten |
| `gpu` (.224) | keine NVIDIA-GPU am PCI-Bus | Karte/Steckplatz prüfen |

### 5.2 Orchestrator .225

```bash
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit orchestrator -K
```

Was installiert wird: Basispakete, Python 3.13 + venv, Benutzer `hermclaw` und `hermclaw-admin`, Verzeichnisse
(`/opt/hermclaw`, `/etc/hermclaw`, `/var/lib/hermclaw/{workspaces,artifacts,repos}`, `/var/log/hermclaw`),
**PostgreSQL 17 + pgvector ≥ 0.8.7 aus dem PGDG-Repository** (Debians pgvector 0.8.0 ist von CVE-2026-3172
betroffen, 0.8.2–0.8.6 von CVE-2026-103484; Entscheidungen D-002, D-012), Datenbank `hermclaw`, rootless Podman für SearXNG, SSH-Schlüssel der Runtime
(`/etc/hermclaw/ssh/id_ed25519`, `backup_ed25519`), `known_hosts`, die verschlüsselten Credentials
(`/etc/credstore.encrypted/hermclaw-*`: API-Admin-Token, LiteLLM-Master-Key, je Worker ein Token).

Prüfen auf .225:

```bash
dpkg-query -W postgresql-17 postgresql-17-pgvector          # pgvector >= 0.8.7
sudo -u postgres psql -d hermclaw -tAc "SELECT extversion FROM pg_extension WHERE extname='vector'"
sudo ls -l /etc/hermclaw/ssh                                 # id_ed25519 (0600 hermclaw), backup_ed25519, known_hosts
sudo /opt/hermclaw/bin/hermclaw-credential list              # api-admin-token litellm-master-key worker-token-exec-222 worker-token-model-224
systemctl list-unit-files 'hermclaw-*'                       # installiert, disabled
```

### 5.3 Web-UI .223

```bash
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit webui -K
```

Installiert Node.js 22 (für den UI-Build) und nginx mit einer Platzhalter-Seite und der SSE-fähigen Proxy-
Konfiguration. Prüfen:

```bash
node --version                                   # v22.x (>= 22.12)
sudo nginx -t
curl -sI http://127.0.0.1/ -H 'Host: hermclaw.lan' | head -1    # HTTP/1.1 200
```

### 5.4 Execution Worker .222

```bash
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit execution_workers -K
```

Installiert rootless Podman (Benutzer `hermclaw` mit subuid/subgid, Lingering, cgroup-v2-Delegation für
cpu/memory/pids), Wake-on-LAN (persistentes `ethtool -s <if> wol g`), das Worker-Token (aus dem Credential-Store
des Orchestrators), die Unit `hermclaw-exec-worker`. Prüfen:

```bash
sudo ethtool "$(ip route show default | awk '{print $5; exit}')" | grep Wake-on     # Wake-on: g
sudo -u hermclaw XDG_RUNTIME_DIR=/run/user/$(id -u hermclaw) podman info --format '{{.Host.Security.Rootless}} {{.Host.CgroupControllers}}'
#   true [cpu memory pids ...]
sudo -l -U hermclaw-admin                         # nur: /usr/bin/systemctl suspend
```

### 5.5 Model Worker .224 – NVIDIA-Treiber und Ollama

```bash
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit model_workers -K
```

Der Treiberweg ist wegen der GTX 1080 (Pascal, Compute Capability 6.1) besonders (Entscheidung D-011,
Research R-026/R-027):

* Ollama ≥ 0.30 braucht für Pascal einen NVIDIA-Treiber **≥ 570**. Debian 13 liefert nur 550, und NVIDIAs
  `debian13`-Repository hat keinen Pascal-fähigen Zweig. **R580 ist der letzte Treiberzweig mit Pascal**
  (590+ unterstützt die GTX 10xx nicht mehr).
* Die Rolle `nvidia` installiert deshalb **NVIDIA 580.173.02 über den offiziellen `.run`-Installer mit DKMS**
  (proprietäre Kernelmodule, die offenen Module unterstützen Pascal nicht). Ein apt-Pinning
  (`/etc/apt/preferences.d/hermclaw-nvidia.pref`) verhindert, dass apt jemals einen paketierten NVIDIA-Treiber
  darüber installiert.
* nouveau wird abgeschaltet. Das erfordert **einen Neustart**, der Treiberbau einen **zweiten**. Der Play
  stoppt mit einem Hinweis (oder startet selbst neu mit `-e nvidia_allow_reboot=true`). Nach jedem Neustart
  denselben Befehl erneut ausführen, bis er durchläuft.
* Die Rolle `ollama` installiert **Ollama 0.40.4** aus dem gepinnten Release-Archiv
  `ollama-linux-amd64.tar.zst` (SHA-256 `15f31e95…ed3740a` geprüft) nach `/opt/ollama/0.40.4` mit Symlink
  `current`. Das Installationsskript `install.sh` wird **nie** verwendet: Es würde ohne `nvidia-smi` den neuesten
  CUDA-Treiber ohne Pascal-Unterstützung nachinstallieren.
* Ollama-Umgebung (systemd-Drop-in): `OLLAMA_HOST=0.0.0.0:11434` (mit nftables-Regel: nur .225 und lokal),
  `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KEEP_ALIVE=5m`, `OLLAMA_LOAD_TIMEOUT=10m`,
  `OLLAMA_NO_CLOUD=1`, `OLLAMA_VULKAN=0`. Flash Attention und KV-Cache-Quantisierung (`q8_0`) erst nach dem
  GPU-Rauchtest (5.6).

Prüfen auf .224:

```bash
nvidia-smi --query-gpu=driver_version,name,memory.total,compute_cap --format=csv,noheader
#   580.173.02, NVIDIA GeForce GTX 1080, 8192 MiB, 6.1
dkms status                                      # nvidia/580.173.02, <kernel>, x86_64: installed
curl -s http://127.0.0.1:11434/api/version       # {"version":"0.40.4"}
journalctl -u ollama -b --no-pager | grep -E 'inference compute|driver too old'
#   muss "library=CUDA compute=6.1" zeigen, darf NICHT "driver too old" oder "id=cpu" zeigen
sudo nft list table inet hermclaw_ollama         # 11434 nur lo + 192.168.178.225
```

Von .223 aus muss `curl -s --max-time 3 http://192.168.178.224:11434/api/version` **ablaufen** (Firewall).

### 5.6 GPU-Rauchtest und Feinabstimmung (.224)

```bash
ollama pull qwen3:8b                              # falls noch nicht geladen (Kapitel 7)
curl -s http://127.0.0.1:11434/api/generate -d '{"model":"qwen3:8b","prompt":"ok","stream":false,"think":false,"options":{"num_ctx":16384,"num_predict":8}}'
ollama ps                                         # qwen3:8b: PROCESSOR "100% GPU", CONTEXT 16384
curl -s http://127.0.0.1:11434/api/generate -d '{"model":"qwen3:8b","keep_alive":0}'   # entladen
```

Läuft der Test, schalten Sie Flash Attention und den q8_0-KV-Cache ein. Das spart auf den großen Modellen
1,5–3 GB. Im Inventar beim Host `hermclaw-model-224`:

```yaml
          ollama_flash_attention: true
          ollama_kv_cache_type: q8_0
```

Danach den Bootstrap für `model_workers` erneut ausführen. **Secure Boot anlassen?** Dann nach dem ersten
Treiberbau: `sudo mokutil --import /var/lib/dkms/mok.pub`, neu starten, im MokManager bestätigen,
`nvidia_secureboot_mok_enrolled: true` setzen, Bootstrap erneut ausführen.

**Kernel-Updates:** DKMS baut das Modul bei jedem Kernel-Update neu (`linux-headers-amd64` ist installiert).
Prüfen Sie nach jedem Kernel-Update `nvidia-smi` und den Rauchtest. Treiber-Updates nur innerhalb von 580.x,
nie auf 590+.

### 5.7 Abschluss des Bootstraps

```bash
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml -K                 # alle Hosts: changed=0
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --check --diff -K  # changed=0
```

Runtime-SSH vom Orchestrator testen:

```bash
sudo -u hermclaw ssh -i /etc/hermclaw/ssh/id_ed25519 -o UserKnownHostsFile=/etc/hermclaw/ssh/known_hosts \
  -o StrictHostKeyChecking=yes hermclaw-admin@192.168.178.222 true && echo OK      # ebenso .223, .224
```

---

## 6. Zugangsdaten und Schlüssel

Alle Geheimnisse liegen als **verschlüsselte systemd-Credentials** in `/etc/credstore.encrypted/hermclaw-<name>`
(verschlüsselt mit dem Host-Schlüssel bzw. TPM2, `systemd-creds`). Die Dienste bekommen sie über
`LoadCredentialEncrypted=` und lesen sie aus `$CREDENTIALS_DIRECTORY`. Kein Geheimnis steht in einer
Konfigurationsdatei, einer Umgebungsvariable, einem Log oder auf einer Kommandozeile. Werkzeug auf jedem Host:
`/opt/hermclaw/bin/hermclaw-credential` (`set`, `generate`, `check`, `list`; Werte nur über stdin).

| Credential | Host | Erzeugt von | Verwendet von |
|---|---|---|---|
| `api-admin-token` | .225 | Bootstrap (zufällig) | API: Admin-Token für die Erstanmeldung |
| `litellm-master-key` | .225 | Bootstrap (zufällig) | LiteLLM-Proxy und Scheduler |
| `worker-token-exec-222`, `worker-token-model-224` | .225 | Bootstrap (zufällig) | Scheduler/API (Signatur der Worker-Aufrufe) |
| `worker-token` | .222, .224 | Bootstrap (Kopie vom Orchestrator) | Worker-Daemons |
| `gitlab-token` | .225 | **Sie** (Schritt 6.1) | Scheduler: Merge Requests, geschützte Branches |

### 6.1 GitLab vorbereiten (.226)

1. Legen Sie in GitLab einen Bot-Benutzer an (z. B. `hermclaw-bot`) oder verwenden Sie einen
   **Project/Group Access Token** mit Rolle *Developer* und den Scopes `api`, `read_repository`,
   `write_repository`.
2. Schützen Sie `main`, `master` und `release/*` (*Settings → Repository → Protected branches*). Hermclaw pusht
   nie dorthin, sondern nur auf `hermclaw/*`-Job-Branches und öffnet Merge Requests.
3. Hinterlegen Sie den **Deploy-Key** des Orchestrators mit Schreibrecht (*Settings → Repository → Deploy keys*,
   „Grant write permissions“):

   ```bash
   sudo cat /etc/hermclaw/ssh/id_ed25519.pub          # auf .225
   ```

4. Token auf .225 speichern (Eingabe wird nicht angezeigt):

   ```bash
   sudo /opt/hermclaw/bin/hermclaw-credential set gitlab-token
   sudo /opt/hermclaw/bin/hermclaw-credential check gitlab-token      # OK gitlab-token
   ```

### 6.2 Backup-Schlüssel auf Terra (.60)

Hängen Sie den öffentlichen Backup-Schlüssel des Orchestrators an die `authorized_keys` des Backup-Benutzers auf
Terra an, eingeschränkt auf den Orchestrator:

```bash
sudo cat /etc/hermclaw/ssh/backup_ed25519.pub        # auf .225
# auf Terra (Unraid, als root) in /root/.ssh/authorized_keys – eine Zeile:
restrict,from="192.168.178.225" ssh-ed25519 AAAAC3Nz... hermclaw-backup
```

Unraid: `/root/.ssh` liegt im RAM. Damit der Schlüssel einen Neustart übersteht, legen Sie ihn zusätzlich unter
`/boot/config/ssh/root/authorized_keys` ab (bzw. in der Unraid-Oberfläche: *Users → root → SSH authorized keys*).
Das Backup selbst richten Sie in Kapitel 11 ein.

### 6.3 Token rotieren

```bash
# Worker-Token (auf .225), danach Worker-Play erneut, dann Dienste neu starten
sudo /opt/hermclaw/bin/hermclaw-credential generate worker-token-exec-222 --force
ssh admin@192.168.178.222 sudo rm /etc/credstore.encrypted/hermclaw-worker-token
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit execution_workers -K
sudo systemctl restart hermclaw-api hermclaw-scheduler        # .225
# GitLab: in GitLab ein neues Access Token erzeugen, dann
sudo /opt/hermclaw/bin/hermclaw-credential set gitlab-token --force && sudo systemctl restart hermclaw-scheduler
```

---

## 7. Modelle laden (.224)

Der Model Worker braucht diese sechs Modelle (≈ 70 GB). Die Modellrollen sind fest (Kapitel 1.2):

| Tag | Rolle | Download | min. Ollama |
|---|---|---|---|
| `gemma4:26b` | Planner | ≈ 18 GB | 0.20.0 |
| `gemma4:12b` | Planner-Fallback | ≈ 7,6 GB | 0.30.3 |
| `qwen3-coder:30b` | Implementation Worker | ≈ 19 GB | – |
| `qwen3.8:27b` | Heavy Reviewer | ≈ 18 GB | **0.32.12** |
| `qwen3:8b` | Fast Router | 5,2 GB | – |
| `embeddinggemma:300m` | Embedding (768 Dim., 2048 Token Kontext) | ≈ 0,6 GB | 0.11.10 |

Das Deployment (Kapitel 8) lädt sie automatisch mit `hermclaw-model-pull`. Manuell, z. B. vorab, während Sie
Kapitel 8 vorbereiten:

```bash
# auf .224 (aus dem Repository: scripts/ops/hermclaw-model-pull; nach dem Deployment unter /opt/hermclaw/bin)
df -h /var/lib/ollama                              # >= 80 GB frei
sudo -u ollama /opt/hermclaw/bin/hermclaw-model-pull --dry-run
sudo -u ollama /opt/hermclaw/bin/hermclaw-model-pull
#   prüft jedes Modell mit `ollama show`; fehlt ein Rollenmodell: Exit 1
/opt/ollama/current/bin/ollama list
```

Gleichwertig ohne Skript: `ollama pull gemma4:26b`, `ollama pull gemma4:12b`, `ollama pull qwen3-coder:30b`,
`ollama pull qwen3.8:27b`, `ollama pull qwen3:8b`, `ollama pull embeddinggemma:300m`.

Ohne Planner-Fallback (spart 7,6 GB): `hermclaw-model-pull --skip-fallback-planner` und in `models.yaml` das
Profil `planner-gemma-fallback` auf `enabled: false` setzen.

Später auf EmbeddingGemma 2 wechseln (sobald Ollama es unter Linux/CUDA ausführt): `hermclaw-model-pull
--embedding embeddinggemma-2:740m`. Das Skript fällt automatisch auf `embeddinggemma:300m` zurück und nennt die
nötige Änderung in `models.yaml` (Kapitel 9.2).

---

<!-- INSTALL-PART-2: Kapitel 6–14 (Zugangsdaten, Modelle, Deployment, Konfiguration, Inbetriebnahme, Backup,
Live-Abnahme, Betrieb, Fehlersuche) – wird nach P32/P33 vervollständigt. -->
