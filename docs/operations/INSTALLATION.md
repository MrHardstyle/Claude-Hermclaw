# Hermclaw Next – Installationsanleitung

Diese Anleitung führt von leeren Rechnern bis zum laufenden, abgenommenen Hermclaw-Next-System.
Sie beschreibt jeden Host einzeln, alle Konfigurationsdateien, die Sie anpassen müssen, jeden Befehl
und alle Modelle, die geladen werden.

> **Stand:** Release Candidate `0.1.0rc1`, 2026-10-14. Alle Schritte sind automatisiert (Ansible, Skripte)
> und lokal getestet. Die Ziel-Hosts waren aus der Build-Umgebung nicht erreichbar (BLOCKER-001). Deshalb
> endet die Installation mit der **Live-Abnahme** (Kapitel 12), die alle Prüfungen gegen die echten Hosts
> nachholt.

## Inhalt

- [1. Überblick](#1-überblick)
- [2. Hardware, BIOS und Netzwerk](#2-hardware-bios-und-netzwerk)
- [3. Debian 13 installieren (.225, .223, .222, .224)](#3-debian-13-installieren-225-223-222-224)
- [4. Admin-Rechner (Ansible-Steuerrechner)](#4-admin-rechner-ansible-steuerrechner)
- [5. Bootstrap der Hosts (P01)](#5-bootstrap-der-hosts-p01)
- [6. Zugangsdaten und Schlüssel](#6-zugangsdaten-und-schlüssel)
- [7. Modelle laden (.224)](#7-modelle-laden-224)
- [8. Release bauen und ausrollen (Deployment)](#8-release-bauen-und-ausrollen-deployment)
- [9. Konfiguration](#9-konfiguration)
- [10. Erste Inbetriebnahme](#10-erste-inbetriebnahme)
- [11. Backup und Restore-Test (Terra .60)](#11-backup-und-restore-test-terra-60)
- [12. Live-Abnahme](#12-live-abnahme)
- [13. Betrieb](#13-betrieb)
- [14. Fehlersuche](#14-fehlersuche)

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

Der Preflight (`scripts/bootstrap/check-host.sh`) prüft diese Mindestwerte und bricht darunter ab (beim RAM mit
10 % Toleranz, Kapitel 5.1):

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
| .224 | Secure Boot | **aus** | Der NVIDIA-Treiber wird per DKMS gebaut und ist unsigniert. Alternative: DKMS-MOK-Schlüssel einschreiben (Kapitel 5.5) |
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
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub   # Fingerabdruck notieren (Schritt 4)
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
| `hermclaw_web_server_name` | `hermclaw-webui` | `hermclaw.lan 192.168.178.223` | nginx `server_name` (Namen und/oder Adressen). Die Adresse drinlassen, sonst landen Aufrufe per IP auf Debians Default-Site. `_` = Catch-all, nur wenn sonst keine Default-Site existiert |
| `hermclaw_worker_id` | Worker-Hosts | `exec-222`, `model-224` | Worker-IDs, müssen zu `hosts.yaml` passen |
| `wol_mac` | `.222`, `.224` | `3c:7c:3f:12:34:56` | **echte MAC** aus Schritt 2.3 (der Platzhalter `00:00:00:00:00:00` wird von `hermclaw doctor` gemeldet) |
| `wol_interface` | `.222`, `.224` | leer oder `enp3s0` | leer = Schnittstelle der Default-Route |
| `common_known_hosts_extra` | `hermclaw-orchestrator` | siehe unten | SSH-Host-Schlüssel von GitLab und Terra |
| `nvidia_secureboot_mok_enrolled` | `hermclaw-model-224` | `false` | nur `true`, wenn Sie Secure Boot anlassen und den DKMS-Schlüssel eingeschrieben haben (Kapitel 5.5) |

### 4.3 SSH-Host-Schlüssel von GitLab und Terra

Holen Sie die Schlüssel und vergleichen Sie den Fingerabdruck mit der Konsole des jeweiligen Systems
(`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`):

```bash
ssh-keyscan -t ed25519 192.168.178.226 192.168.178.60 2>/dev/null > keyscan.txt   # Zeilen zum Eintragen
ssh-keygen -lf keyscan.txt                                                        # Fingerabdrücke zum Vergleich
```

Stimmen die Fingerabdrücke, tragen Sie die Zeilen aus `keyscan.txt` beim Orchestrator ein:

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
| `ram` / `cpu` / `disk` | unter dem Minimum (Tabelle 2.1). Den RAM misst der Preflight als `MemTotal` aus `/proc/meminfo`. Der Kernel reserviert Speicher, ein Rechner mit genau 8 GB bzw. 16 GB meldet weniger. Deshalb genügen 90 % des Minimums | aufrüsten, oder das Minimum im Inventar beim Host bewusst senken: `common_preflight_min_ram_mb` (MiB), `common_preflight_min_cpus`, `common_preflight_min_disk_gb` |
| `gpu` (.224) | keine NVIDIA-GPU am PCI-Bus | Karte/Steckplatz prüfen |

### 5.2 Orchestrator .225

```bash
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit orchestrator -K
```

Was installiert wird: Basispakete, Python 3.13 + venv, Benutzer `hermclaw` (das eingeschränkte SSH-Konto
`hermclaw-admin` entsteht nur auf .223, .222 und .224), Verzeichnisse
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
node --version                                   # v22.x (>= 22.23.2, letztes Sicherheitsrelease)
sudo nginx -t
curl -sI http://127.0.0.1/ -H 'Host: hermclaw.lan' | head -1    # HTTP/1.1 200
```

**Node.js-Mindestversion 22.23.2** (letztes Sicherheitsrelease der 22er-Linie). NodeSource hinkt Sicherheitsreleases
manchmal hinterher (Research R-028 B9). Bricht die Rolle mit „does not satisfy 22.x >= 22.23.2“ ab, installieren Sie
das offizielle Tarball (Prüfsumme gegen `SHASUMS256.txt`):

```bash
V=22.23.3
cd /tmp && curl -fsSLO https://nodejs.org/dist/v$V/node-v$V-linux-x64.tar.xz && curl -fsSLO https://nodejs.org/dist/v$V/SHASUMS256.txt
grep " node-v$V-linux-x64.tar.xz\$" SHASUMS256.txt | sha256sum -c -
sudo tar -xJf node-v$V-linux-x64.tar.xz -C /opt && sudo ln -sfn /opt/node-v$V-linux-x64 /opt/node22
sudo ln -sfn /opt/node22/bin/node /usr/local/bin/node && sudo ln -sfn /opt/node22/bin/npm /usr/local/bin/npm
node --version                                   # v22.23.3
```

Danach den Bootstrap für `webui` erneut ausführen.

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

**Secure Boot anlassen?** Dann richten Sie den DKMS-Signaturschlüssel **vor** dem ersten Bootstrap von .224 ein.
Solange `nvidia_secureboot_mok_enrolled` nicht `true` ist, bricht die Rolle `nvidia` schon vor dem Treiberbau ab.
Auf .224:

```bash
sudo apt install dkms mokutil
sudo dkms generate_mok                           # legt /var/lib/dkms/mok.key und mok.pub an
sudo mokutil --import /var/lib/dkms/mok.pub      # fragt ein Einmal-Passwort für den MokManager ab
sudo reboot                                      # im MokManager „Enroll MOK“ wählen, Passwort eingeben
```

Danach im Inventar beim Host `hermclaw-model-224` `nvidia_secureboot_mok_enrolled: true` setzen. DKMS signiert die
Treibermodule mit diesem Schlüssel. Mit Secure Boot aus (Kapitel 2.2) entfällt dieser Schritt.

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
sudo journalctl -u ollama -b --no-pager | grep -E 'inference compute|driver too old'
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

Danach den Bootstrap für `model_workers` erneut ausführen.

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
Konfigurationsdatei, einem Log oder auf einer Kommandozeile. Zwei Ausnahmen bekommen ihr Geheimnis als
Umgebungsvariable, weil sie es nur dort lesen: LiteLLM den Master-Key als `LITELLM_MASTER_KEY` (beim Start von
`hermclaw-cred-env` aus dem Credential gesetzt), SearXNG sein Secret als `SEARXNG_SECRET` (ein Podman-Secret, kein
systemd-Credential). Werkzeug auf .225, .222 und .224 (nicht auf .223, dort liegen keine Credentials):
`/opt/hermclaw/bin/hermclaw-credential` (`set`, `generate`, `check`, `list`; Werte nur über stdin).

| Credential | Host | Erzeugt von | Verwendet von |
|---|---|---|---|
| `api-admin-token` | .225 | Bootstrap (zufällig) | API: Admin-Token für die Erstanmeldung |
| `litellm-master-key` | .225 | Bootstrap (zufällig) | LiteLLM-Proxy und Scheduler |
| `worker-token-exec-222`, `worker-token-model-224` | .225 | Bootstrap (zufällig) | Scheduler/API (Signatur der Worker-Aufrufe) |
| `worker-token` | .222, .224 | Bootstrap (Kopie vom Orchestrator) | Worker-Daemons |
| `gitlab-token` | .225 | **Sie** (Schritt 6.1) | Scheduler: Merge Requests, geschützte Branches |
| `api-operator-token` | .225 | Deployment (einmalig) | Ihr erstes Admin-Token für die Oberfläche (Kapitel 8.6) |
| `backup-manifest-key` | .225 | **Sie** (Kapitel 11.3, optional, empfohlen) | Backup-Units: Signatur der Backup-Manifeste |

### 6.1 GitLab vorbereiten (.226)

1. Legen Sie in GitLab einen Bot-Benutzer an (z. B. `hermclaw-bot`) oder verwenden Sie einen
   **Project/Group Access Token** mit Rolle *Developer* und den Scopes `api`, `read_repository`,
   `write_repository`.
2. Schützen Sie `main`, `master` und `release/*` (*Settings → Repository → Protected branches*). Hermclaw pusht
   nie dorthin, sondern nur auf `hermclaw/*`-Job-Branches und öffnet auf Wunsch Merge Requests
   (`git.create_merge_request`, Kapitel 9.3).
3. Hinterlegen Sie den **Deploy-Key** des Orchestrators mit Schreibrecht (*Settings → Repository → Deploy keys*,
   „Grant write permissions“):

   ```bash
   sudo cat /etc/hermclaw/ssh/id_ed25519.pub          # auf .225
   ```

   Hermclaw verwendet diesen Schlüssel und die geprüften Host-Schlüssel aus `/etc/hermclaw/ssh/known_hosts`
   (Kapitel 4.3) für Clone, Fetch und Push über SSH. Dafür hat der Eintrag `gitlab-226` in `hosts.yaml` einen
   `ssh`-Block (`user: git`; Schlüssel und `known_hosts` sind die Standardwerte). Im Inventar ergänzen Sie dafür
   nichts.

4. Token auf .225 speichern (Eingabe wird nicht angezeigt):

   ```bash
   sudo /opt/hermclaw/bin/hermclaw-credential set gitlab-token
   sudo /opt/hermclaw/bin/hermclaw-credential check gitlab-token      # OK gitlab-token
   ```

### 6.2 Backup-Schlüssel auf Terra (.60)

Hängen Sie den öffentlichen Backup-Schlüssel des Orchestrators an die `authorized_keys` von root auf Terra an
(Unraid bietet SSH nur für root an). Der Schlüssel wird auf den Orchestrator und auf rsync in das Hermclaw-
Verzeichnis eingeschränkt (`rrsync`, Research R-022, RISK R-028):

```bash
sudo cat /etc/hermclaw/ssh/backup_ed25519.pub        # auf .225
# auf Terra (als root):
mkdir -p /mnt/user/backups/hermclaw /root/.ssh && chmod 700 /root/.ssh
echo 'restrict,from="192.168.178.225",command="/usr/local/bin/rrsync /mnt/user/backups/hermclaw" ssh-ed25519 AAAA… hermclaw-backup@orchestrator' >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
```

* Unraid ≥ 6.9 legt `/root/.ssh` dauerhaft auf dem Flash-Laufwerk ab. Prüfen Sie nach einem Neustart, dass der
  Eintrag noch da ist.
* `rrsync` braucht Python 3 auf Unraid (Plugin „NerdTools“). Ohne Python: denselben Eintrag ohne `command=…`, aber
  mit `restrict,from=…` (schwächer), oder die Freigabe als NFS-Mount (Variante B in
  `infra/backup/unraid-share-setup.md`).
* Terras Host-Schlüssel steht nach Kapitel 4.3 bereits in `/etc/hermclaw/ssh/known_hosts`.

Das Backup selbst richten Sie in Kapitel 11 ein.

### 6.3 Token rotieren

```bash
# Worker-Token (auf .225), danach Worker-Play erneut, dann Dienste auf .225 und .222 neu starten
sudo /opt/hermclaw/bin/hermclaw-credential generate worker-token-exec-222 --force
ssh -t admin@192.168.178.222 sudo rm /etc/credstore.encrypted/hermclaw-worker-token
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit execution_workers -K
sudo systemctl restart hermclaw-api hermclaw-scheduler        # .225
ssh -t admin@192.168.178.222 sudo systemctl restart hermclaw-exec-worker   # .222: liest das Token nur beim Start
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

Ohne Planner-Fallback (spart 7,6 GB): `sudo -u ollama /opt/hermclaw/bin/hermclaw-model-pull --skip-fallback-planner`
und in `models.yaml` das Profil `planner-gemma-fallback` auf `enabled: false` setzen.

Später auf EmbeddingGemma 2 wechseln (sobald Ollama es unter Linux/CUDA ausführt): `sudo -u ollama
/opt/hermclaw/bin/hermclaw-model-pull --embedding embeddinggemma-2:740m`. Das Skript fällt automatisch auf `embeddinggemma:300m` zurück und nennt die
nötige Änderung in `models.yaml` (Kapitel 9.2).

---

## 8. Release bauen und ausrollen (Deployment)

Ein **Release** enthält das Hermclaw-Wheel samt aller Abhängigkeiten für CPython 3.13 (Python von Debian 13), den
LiteLLM-Proxy, den UI-Build, die Beispielkonfiguration, die Betriebsskripte und die tree-sitter-Grammatiken. Es wird
einmal auf dem Admin-Rechner gebaut, per SHA-256 geprüft und offline auf allen Hosts installiert. Details:
`docs/operations/deployment.md`, Architektur: `docs/architecture/deployment.md`.

### 8.1 Voraussetzungen auf dem Admin-Rechner

* ansible-core 2.21 aus Kapitel 4.
* **CPython 3.13** mit `venv` (auf Debian 13: `sudo apt install python3 python3-venv`; sonst `--python /pfad/python3.13`).
  Das Release muss zur Python-Version der Hosts passen; der Preflight vergleicht das.
* **Node.js ≥ 22.23.2** mit npm (für den UI-Build), `tar`, `gzip`, `sha256sum`, Zugang zu PyPI, npm und GitHub.
* Hat Ihr Admin-Rechner kein Python 3.13, bauen Sie das Release auf dem Orchestrator .225 (Debian 13) oder in einem
  `debian:trixie`-Container aus demselben Checkout. Dort fehlt Node.js ≥ 22.12: Debian 13 liefert Node 20, und die
  Rolle `nodejs` richtet Node 22 nur auf .223 ein. Bauen Sie die UI deshalb vorher auf einem Rechner mit Node.js
  (`cd ui && npm ci && npm run build`), kopieren Sie `ui/dist` in den Checkout auf .225 bzw. im Container und
  übergeben Sie `--ui-dist ui/dist`. Das fertige `dist/release-<version>/` kopieren Sie danach in `dist/` des
  Checkouts auf dem Admin-Rechner, denn `playbooks/deploy.yml` liest es dort (oder Sie übergeben
  `-e hermclaw_release_root=<Verzeichnis auf dem Admin-Rechner>`).

### 8.2 Inventar ergänzen

Zusätzlich zu Kapitel 4.2 in `infra/ansible/inventory/hosts.yml`:

| Variable | Wert | Bedeutung |
|---|---|---|
| `hermclaw_version` | `"0.1.0rc1"` (in Anführungszeichen) | Version, die ausgerollt wird = Name von `dist/release-<version>/` |
| `wol_mac` (Worker) | echte MAC | **Pflicht**: das Deployment verweigert Platzhalter-MACs |
| `nginx_tls_enabled` (.223) | `true` / `false` | HTTPS für die Oberfläche (siehe 8.5) |
| `hermclaw_config_overrides` | siehe Kapitel 9 | Ihre Anpassungen der Konfiguration |
| `hermclaw_keep_releases` | `3` | ältere Releases je Host (1–99, zusätzlich zum aktiven) für ein Rollback ohne Netz; unter `all.vars` gilt der Wert für alle Hosts |
| `hermclaw_worker_pull_models` (.224) | `true` | Modelle automatisch laden (Kapitel 7) |

### 8.3 Release bauen

```bash
# im Checkout der zu installierenden Version (Kapitel 4.1)
scripts/deploy/build-release.sh --wheel-cache ~/hermclaw-wheels
#   -> dist/release-0.1.0rc1/  (release.json, Wheelhouses mit Hash-Lock, UI-Build, SHA256SUMS)
#   Selbsttest: Offline-Installation in eine frische venv, pip check, alle Konsolen-Einstiegspunkte laden,
#   hermclaw config-check der Beispiele, LiteLLM-Lock ohne den litellm_init.pth-Schadcode, UI ohne Inline-Skripte
```

Später ohne Internet neu bauen: `scripts/deploy/build-release.sh --offline-from ~/hermclaw-wheels --ui-dist ui/dist`.
`build-release.sh` baut die UI nur in einem temporären Verzeichnis, und `ui/dist` gehört nicht zum Checkout. Legen Sie
`ui/dist` deshalb an, solange npm erreichbar ist: `cd ui && npm ci && npm run build`. Gibt es `dist/release-<version>/`
schon, hängen Sie `--force` an.

### 8.4 Ausrollen

Vorher muss das GitLab-Token gespeichert sein (Kapitel 6.1). Der Preflight prüft jedes Credential, das die Units
laden, und bricht vor jeder Änderung ab, wenn eines fehlt.

```bash
cd infra/ansible
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml --check --diff -K     # Trockenlauf
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -K                    # Ausrollen (alle Hosts)
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -K                    # zweiter Lauf: changed=0
```

Was dabei passiert:

| Host | Schritte |
|---|---|
| .225 | Release-venv `/opt/hermclaw/venv-<v>` (offline, Hash-geprüft) → `/etc/hermclaw/*.yaml` in einer Staging-Kopie rendern + mit `hermclaw config-check` des neuen Release prüfen → LiteLLM-venv installieren (noch nicht aktiv) → Schema-Entscheidung (bis hier ist nichts Laufendes verändert) → Units aktivieren → laufende Schritte sauber anhalten → `hermclaw migrate` (einmal, vor dem Neustart) → `/etc/hermclaw/*.yaml` + `litellm.yaml` installieren → Symlinks `/opt/litellm/venv` und `/opt/hermclaw/venv` umschalten → `hermclaw-litellm` muss gesund sein und unauthentifizierte Anfragen ablehnen → SearXNG, API, Scheduler, Backup- und Restore-Test-Timer starten → Health-Checks → Admin-API-Token anlegen (einmalig) |
| .222 | Release-venv → Sandbox-Images bauen (rootless, nur fehlende) → `capabilities.yaml`, `policies.yaml` unmittelbar vor dem Umschalten → `hermclaw-exec-worker` → `/health` |
| .224 | Release-venv → `capabilities.yaml` → ffmpeg → `hermclaw-model-worker` → `/health` → fehlende Modelle laden (in einer eigenen Unit, übersteht SSH-Abbrüche, bis 6 h) |
| .223 | nginx-Site (SSE, CSP, Sicherheits-Header, optional TLS) → UI nach `/var/www/hermclaw-next-releases/<v>/` → Symlink umschalten → `nginx -t` → Reload → Prüfung über nginx |
| alle | zum Schluss `hermclaw doctor` auf .225 im Dienstkontext; jeder `fail` stoppt den Lauf |

Worker und Oberfläche kommen dem Orchestrator nie zuvor: Ein Lauf mit `--limit execution_workers`, `--limit
model_workers`, `--limit webui` oder `--tags hermclaw_ui` ohne den Orchestrator bricht ab, wenn .225 noch nicht
`hermclaw_version` meldet (`/api/health`). Nur wenn der Orchestrator absichtlich nicht läuft (Wiederherstellung eines
Hosts, `docs/runbooks/disaster-recovery.md`), mit `-e hermclaw_release_require_orchestrator_first=false` ausführen.

Der erste Lauf ersetzt die leere venv des Bootstraps (sie bleibt als `/opt/hermclaw/venv.pre-release-<zeit>` erhalten;
löschen Sie sie, wenn alles läuft).

### 8.5 HTTPS für die Oberfläche (optional, empfohlen)

```yaml
    webui:
      hosts:
        hermclaw-webui:
          nginx_tls_enabled: true
          nginx_tls_source: self-signed        # oder: provided + nginx_tls_cert_src / nginx_tls_key_src (Dateien auf dem Admin-Rechner)
          hermclaw_web_server_name: "hermclaw.lan 192.168.178.223"   # auch die SANs des Zertifikats
```

HTTP leitet dann auf HTTPS um. `nginx_hsts_max_age` erst setzen, wenn alle Browser dem Zertifikat vertrauen.

### 8.6 Prüfen

```bash
# .225
systemctl status hermclaw-litellm hermclaw-api hermclaw-scheduler hermclaw-backup.timer hermclaw-restore-test.timer
readlink /opt/hermclaw/venv /opt/litellm/venv
curl -s http://127.0.0.1:4000/health/liveliness                              # "I'm alive!"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/v1/models    # 401 (200 hieße: kein Master-Key!)
curl -s http://127.0.0.1:8888/healthz                                         # OK (SearXNG)
sudo hermclaw doctor
# .222 / .224
curl -s http://192.168.178.222:8787/health; curl -s http://192.168.178.224:8787/health
# .223
curl -s http://192.168.178.223/healthz; curl -sI http://192.168.178.223/ | grep -i content-security-policy
```

Das Admin-API-Token für die erste Anmeldung in der Oberfläche (es wird nie ausgegeben):

```bash
sudo systemd-creds decrypt --name=api-operator-token /etc/credstore.encrypted/hermclaw-api-operator-token -
```

### 8.7 Upgrade und Rollback

* **Upgrade:** neues Release bauen, Backup (Kapitel 11), dann
  `ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -e hermclaw_version=<neu> -K`. Die neue venv wird neben der
  alten installiert; erst nach erfolgreicher Migration wird umgeschaltet. Scheitert die Migration, starten die alten
  Dienste wieder. Ablauf: `docs/runbooks/upgrade.md`.
* **Rollback:** `ansible-playbook … playbooks/deploy.yml -e hermclaw_version=<alt> -K`. Nichts wird neu installiert, die
  Symlinks schalten zurück. Hat die neue Version Datenbank-Migrationen hinzugefügt, stoppt der Lauf und erklärt die
  Optionen: nach einem Backup mit `-e hermclaw_orchestrator_allow_schema_downgrade=true` erneut ausführen, oder das
  Backup von vor dem Upgrade zurückspielen.

---

## 9. Konfiguration

### 9.1 Wo die Konfiguration liegt

| Datei | Host | Inhalt |
|---|---|---|
| `/etc/hermclaw/hosts.yaml` | .225 | Hosts, Adressen, Worker-APIs, Wake-on-LAN-MACs, SSH-Ziele, Idle-Sleep |
| `/etc/hermclaw/models.yaml` | .225 | Modellprofile (Rollen, Ollama-Tags, Kontext, Timeouts), LiteLLM-Adresse |
| `/etc/hermclaw/policies.yaml` | .225, .222 | Befehls-/Sandbox-/Scope-/Coder-/Review-/Research-/Git-/SSH-/WoL-Richtlinien |
| `/etc/hermclaw/capabilities.yaml` | alle Worker | Schrittarten → Capability → Worker-Art, Modellrolle, Ressourcen |
| `/etc/hermclaw/logging.yaml` | .225 | Log-Level je Logger |
| `/etc/hermclaw/databases.yaml` | .225 | Datenbanken Ihrer Projekte für die DB-Tools (optional) |
| `/etc/hermclaw/backup.yaml` | .225 | Backup-Ziel, Aufbewahrung (Kapitel 11) |
| `/etc/hermclaw/litellm.yaml` | .225 | aus `models.yaml` erzeugt (`hermclaw litellm-config`), nicht von Hand ändern |
| `/etc/hermclaw/hermclaw.env`, `worker.env` | .225 / Worker | Prozess-Einstellungen (`HERMCLAW_*`, `WORKER_*`), vom Bootstrap/Deployment gerendert |

### 9.2 So ändern Sie die Konfiguration

**Das Deployment besitzt diese Dateien.** Es erzeugt sie aus `config/<name>.example.yaml` des Releases, setzt die
Werte aus dem Inventar ein (Adressen, Worker-URLs, MACs, Sandbox-Images) und mischt Ihre Overrides hinein. Danach
prüft es das Ergebnis mit `hermclaw config-check`, bevor es eine Datei ersetzt (die alte bleibt als
`<name>.yaml.<zeit>~`). Änderungen direkt auf dem Host überschreibt der nächste Lauf; `--check --diff` zeigt sie als
Abweichung an. Tragen Sie Änderungen deshalb ins Inventar ein:

```yaml
all:
  vars:
    hermclaw_config_overrides:
      policies:
        git: {create_merge_request: true}          # Merge Requests in GitLab öffnen (Standard: nur Branch pushen)
        coder: {max_turns: 24}
      models:
        litellm: {request_timeout_seconds: 1800}
    hermclaw_config_host_overrides:
      model-224: {wake_on_lan: {service_timeout_seconds: 300}}
    hermclaw_config_unmanaged: [databases]         # einmal anlegen, danach pflegen Sie die Datei selbst
```

Mappings werden Schlüssel für Schlüssel gemischt, Listen ersetzen die Liste des Beispiels vollständig. Danach das
Deployment erneut ausführen (Kapitel 8.4).

### 9.3 Einstellungen, die Sie prüfen sollten

| Datei → Schlüssel | Standard | Wann ändern |
|---|---|---|
| `policies` → `git.create_merge_request` | `false` | `true`, wenn Hermclaw für jeden Job einen Merge Request öffnen soll |
| `policies` → `git.protected_branches` | `[main, master, "release/*"]` | weitere geschützte Branches |
| `policies` → `git.author_name` / `author_email` | `Hermclaw Runtime` / `hermclaw@localhost` | Commit-Autor in GitLab |
| `policies` → `ssh.require_approval_for_mutations` | `true` | nur ausschalten, wenn Host-Änderungen ohne Freigabe laufen dürfen (nicht empfohlen) |
| `policies` → `ssh.allowed_hosts` | `[webui-223, exec-222, model-224]` | Hosts, die SSH-Schritte überhaupt erreichen dürfen |
| `policies` → `sandbox.network_default` | `none` | Netz in der Sandbox nur über die Schritt-Capability |
| `policies` → `sandbox.cpus` / `memory` / `pids_limit` | 2 / `2g` / … | an die Hardware von .222 anpassen |
| `policies` → `sandbox.env_allowlist` | PATH, HOME, LANG, LC_ALL, CI, PYTHONDONTWRITEBYTECODE | Variablen, die Projekt-Befehle setzen dürfen |
| `policies` → `coder.max_turns` | 20 | Werkzeugrunden je Schritt |
| `policies` → `correction` | 2 Korrekturen, 3 Versuche, 2 Neuplanungen | Ausdauer bei Fehlschlägen |
| `policies` → `research.primary_domains` | offizielle Doku-Domains | eigene bevorzugte Quellen |
| `policies` → `wake_on_lan.idle_after_minutes` | 30 | Leerlaufzeit bis zum Suspend der Worker |
| `policies` → `events.retention_days` | 90 | Aufbewahrung der Events |
| `policies` → `git.workspace_retention_hours` | 72 | Aufbewahrung der Job-Workspaces |
| `models` → `model_host_capacity_gb` | 34 | RAM + VRAM von .224 abzüglich Reserve (32 GB RAM + 8 GB VRAM → 34) |
| `models` → `profiles[*].model` | feste Tags (Kapitel 1.2) | nur ein anderer Build desselben Modells, nie eine andere Rolle |
| `models` → Profil `planner-gemma-fallback` | aktiv | `enabled: false`, wenn `gemma4:12b` nicht geladen ist |
| `models` → Profil `embedding` | `embeddinggemma:300m` | `embeddinggemma-2:740m`, sobald Ollama es unter Linux/CUDA ausführt |
| `hosts` → je Worker `idle_sleep_command` | `sudo /usr/bin/systemctl suspend` | entfernen, wenn ein Worker nie schlafen soll |
| `databases` → `targets`, `sandbox` | keine | Projekt-Datenbanken für die DB-Tools (siehe 9.4) |

Die Modellrollen sind fest (Gemma = Planner, Qwen3-Coder 30B = Implementation Worker, Qwen3.8 27B = Heavy Reviewer,
Qwen3 8B = Fast Router, EmbeddingGemma). `hermclaw doctor` meldet einen Fehler, wenn einer Rolle ein aktives Profil fehlt.

### 9.4 Projekt-Datenbanken (optional)

Sollen Jobs mit den Datenbanken Ihrer Projekte arbeiten (Schema lesen, Read-only-Abfragen, Migrationen in einer
Wegwerf-Datenbank prüfen), tragen Sie sie in `databases.yaml` ein und machen die Datei per
`hermclaw_config_unmanaged: [databases]` zu Ihrer eigenen. Geheimnisse nur als `cred:`-Referenz:

```yaml
targets:
  - name: shop
    dsn_ref: cred:shop-db-dsn            # postgresql://reader@db.lan:5432/shop?sslmode=require
    kind: postgresql
    environment: production              # production = immer read-only
sandbox:                                 # Wegwerf-Datenbanken für Migrationstests: NICHT der Produktionsserver
  admin_dsn_ref: cred:sandbox-pg-admin   # Rolle mit CREATEDB + CREATEROLE
  client_host: 192.168.178.225           # wie der Execution Worker den Server erreicht
```

Credentials anlegen: `sudo /opt/hermclaw/bin/hermclaw-credential set shop-db-dsn` (Wert über stdin). Damit der
Scheduler sie laden darf, ergänzen Sie ihre Namen in `systemd_skeleton_credentials` im Inventar. Das Mapping wird als
Ganzes ersetzt, deshalb alle Einträge angeben:

```yaml
    systemd_skeleton_credentials:
      api: [api-admin-token]
      scheduler: [litellm-master-key, gitlab-token, shop-db-dsn, sandbox-pg-admin]
      litellm: [litellm-master-key]
      worker: [worker-token]
```

Danach Bootstrap und Deployment für `orchestrator` erneut ausführen.

---



## 10. Erste Inbetriebnahme

Alle Hermclaw-Befehle auf dem Orchestrator laufen über den **Operator-Wrapper** `sudo hermclaw …`. Er startet die
CLI als Benutzer `hermclaw` mit der Umgebung und den Credentials der Dienste (`/etc/hermclaw/hermclaw.env`,
`LoadCredentialEncrypted=`). Ein einfaches `sudo -u hermclaw /opt/hermclaw/venv/bin/hermclaw` hätte weder die
Datenbank-URL noch die Geheimnisse.

### 10.1 Gesamtzustand prüfen

```bash
sudo hermclaw doctor                  # jede Zeile ok/warn; Exit 1 bei einem fail
curl -s http://192.168.178.225:8000/api/health
curl -s http://192.168.178.223/api/health          # über nginx
```

`hermclaw doctor` prüft: Modellrollen, `hosts.yaml` (echte Dateien, keine Platzhalter-MACs), SSH-Hosts,
Sandbox-Trennung, Datenbank (Migrationsstand, pgvector ≥ 0.8.7), Datenpfade und freien Platz, API-Tokens,
LiteLLM (alle Aliase), beide Worker (Heartbeat, `/health`, Protokollversion), installierte Modelle auf .224,
Sandbox-Images und den Selbsttest auf .222 sowie SearXNG.

### 10.2 Zugangstoken für Menschen

```bash
sudo hermclaw token-create admin-<ihr-name> --scopes read,control,admin   # einmalig angezeigt – im Passwortmanager ablegen
sudo hermclaw token-create viewer-<name> --scopes read                    # nur lesen
```

| Scope | Darf |
|---|---|
| `read` | Jobs, Pläne, Events, Diffs, Artefakte ansehen |
| `control` | Jobs anlegen, abbrechen, wiederholen, Repositories (remote) registrieren |
| `admin` | SSH-Mutationen freigeben, Worker-Details/Drain, lokale Repositories, Token-Verwaltung |

Öffnen Sie `http://hermclaw.lan/` bzw. `http://192.168.178.223/` und melden Sie sich mit dem Token an. Das Token
liegt nur im `sessionStorage` des Tabs.

### 10.3 Repository registrieren

```bash
sudo hermclaw repo-add mein-projekt git@192.168.178.226:gruppe/mein-projekt.git \
     --provider gitlab --project-id gruppe/mein-projekt --default-branch main --protected 'release/*'
sudo hermclaw repo-list
```

Optional `--sandbox-image node` (bzw. `python`, `php`, `generic`), wenn das Projekt eine bestimmte Toolchain braucht.

### 10.4 Erster Job (Rauchtest)

```bash
sudo hermclaw job-submit "Füge einen /health-Endpunkt hinzu, der {\"status\": \"ok\"} liefert, mit Test." \
     --title "health endpoint" --repo mein-projekt
```

Beobachten Sie den Job in der UI: Triage (Fast Router) → Plan (Gemma) → Schritte (Qwen3-Coder, Werkzeuge, Tests in
der Sandbox auf .222) → Verifier → Heavy Review (Qwen3.8) → Commit, Push auf `hermclaw/<id>-…` (und ein Merge Request,
wenn `git.create_merge_request: true`, Kapitel 9.3). Die UI zeigt Status und Ergebnisse, **keine** Chain-of-Thought. Schritte, die einen Host ändern (SSH),
warten auf Ihre Freigabe im Tab „Approvals“.

Mit der GTX 1080 dauert ein solcher Job je nach Größe 10–60 Minuten: Die großen Modelle laufen größtenteils aus dem RAM
(Kapitel 2.1).

---

## 11. Backup und Restore-Test (Terra .60)

Hermclaw sichert jede Nacht um 02:30 seinen eigenen Zustand nach Terra/Unraid `.60` und beweist jeden Sonntag um
04:30, dass sich das neueste Backup tatsächlich wiederherstellen lässt (Restore in eine Wegwerf-Datenbank, Vergleich
aller Tabellen und Zeilenzahlen mit dem Manifest). Git-Repositories gehören nicht dazu, sie liegen in GitLab `.226`.
Details: `docs/operations/backup-restore.md`, Architektur: `docs/architecture/backup.md`.

| Inhalt eines Backup-Satzes | Herkunft |
|---|---|
| `db.dump`, `globals.sql` | PostgreSQL-Datenbank `hermclaw` (konsistenter Snapshot), Rollen ohne Passwörter |
| `config.tar.gz` | `/etc/hermclaw` **ohne** Klartext-Geheimnisse (Schlüssel, Tokens, `secrets/` werden ausgelassen und im Manifest aufgeführt) |
| `artifacts.tar.gz` | `/var/lib/hermclaw/artifacts` + Research- und Deployment-Datensätze |
| `MANIFEST.json` (+ `.sig`) | Prüfsummen, Zeilenzahlen, Alembic-Revision, Versionen; optional HMAC-signiert |
| `RESTORE_REPORT.json` | Ergebnis des letzten Restore-Tests dieses Satzes |

### 11.1 Was das Deployment bereits eingerichtet hat

* `/opt/hermclaw/bin/hermclaw-backup` und `/opt/hermclaw/bin/hermclaw-restore-test`;
* die Units `hermclaw-backup.service/.timer` und `hermclaw-restore-test.service/.timer` (beide Timer aktiv). Solange
  `/etc/hermclaw/backup.yaml` fehlt, wird jeder Lauf übersprungen – das ist kein Fehler;
* die Restore-Umgebung in PostgreSQL: Recht `CREATEDB` für die Rolle `hermclaw` und die Vorlage-Datenbank
  `hermclaw_restore_template` mit pgvector (gehört `postgres`, nimmt keine Verbindungen an);
* den Backup-Schlüssel `/etc/hermclaw/ssh/backup_ed25519` (Bootstrap) und den Host-Eintrag `terra-60` in
  `hosts.yaml`;
* die Referenzkonfiguration `/opt/hermclaw/current/backup/backup.example.yaml`.

Kopieren Sie die Units aus `infra/backup/` **nicht** nach `/etc/systemd/system/`: Das Deployment verwaltet diese Dateien
und verweigert jeden weiteren Lauf, wenn dort eine fremde Unit liegt.

### 11.2 Ziel auf Terra vorbereiten

Variante A (empfohlen) ist rsync über SSH mit dem eingeschränkten Schlüssel aus Kapitel 6.2
(`command="/usr/local/bin/rrsync /mnt/user/backups/hermclaw"`). Prüfen Sie den Zugang auf .225:

```bash
sudo -u hermclaw ssh -i /etc/hermclaw/ssh/backup_ed25519 -o UserKnownHostsFile=/etc/hermclaw/ssh/known_hosts \
     -o IdentitiesOnly=yes root@192.168.178.60 true; echo "exit $?"
# rrsync erlaubt nur rsync: eine Fehlermeldung von rrsync ("... is not rsync" o. Ä., exit ≠ 0) ist hier das ERWARTETE
# Ergebnis – der Schlüssel wurde angenommen. "Permission denied" bzw. "Host key verification failed" heißt:
# Kapitel 6.2 bzw. 4.3 wiederholen. Den eigentlichen rsync-Zugang prüft danach "sudo hermclaw backup list" (11.5).
```

Variante B ist die per NFS auf .225 eingebundene Freigabe (`infra/backup/unraid-share-setup.md`, Abschnitt 3). Dann
muss die Unit dorthin schreiben dürfen – im Inventar beim Orchestrator:

```yaml
    orchestrator:
      hosts:
        hermclaw-orchestrator:
          systemd_skeleton_backup_write_paths: [/mnt/terra-backup/hermclaw]
```

### 11.3 Schlüssel für die Manifest-Signatur (empfohlen)

```bash
# auf .225 – den ausgegebenen Wert zuerst im Passwortmanager ablegen ("Hermclaw backup-manifest-key")
KEY=$(openssl rand -hex 32); echo "$KEY"
printf '%s' "$KEY" | sudo systemd-creds encrypt --name=backup-manifest-key - /etc/credstore.encrypted/hermclaw-backup-manifest-key
unset KEY
```

Danach auf dem Admin-Rechner das Deployment des Orchestrators erneut ausführen, damit beide Units den Schlüssel laden:

```bash
cd infra/ansible && ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml --limit orchestrator -K
```

Ohne Schlüssel entfernen Sie in 11.4 die Zeile `manifest_signing_key_ref`. Bei der Wiederherstellung auf einem neuen
Host brauchen Sie den Schlüssel, um signierte Sätze zu prüfen – deshalb der Passwortmanager.

### 11.4 `backup.yaml` anlegen

```bash
sudo install -m 0640 -o root -g hermclaw /opt/hermclaw/current/backup/backup.example.yaml /etc/hermclaw/backup.yaml
sudoedit /etc/hermclaw/backup.yaml
```

| Schlüssel | Wert für diese Installation | Hinweis |
|---|---|---|
| `target.kind` | `rsync` (Variante A) bzw. `local` (Variante B) | |
| `target.host_id` | `terra-60` | Adresse, Benutzer und Schlüssel kommen aus `hosts.yaml` |
| `target.path` | `.` (Variante A mit `rrsync`) | ohne `rrsync`: `/mnt/user/backups/hermclaw`; Variante B: `/mnt/terra-backup/hermclaw` und `require_mount: true` |
| `target.remote_rsync_path` | Zeile löschen bei `rrsync` | sonst `/usr/bin/rsync` |
| `target.bwlimit_kbps` | `60000` | ~60 MB/s, hält das LAN während des Laufs nutzbar |
| `manifest_signing_key_ref` | `cred:backup-manifest-key` | Zeile löschen, wenn Sie 11.3 ausgelassen haben |
| `credstore_dirs` | `[]` | verschlüsselte Credentials sind an diesen Host gebunden; nur für eine Wiederherstellung auf derselben Maschine sinnvoll (`[/etc/credstore.encrypted]`) |
| `retention` | `keep_last: 2`, `keep_daily: 7`, `keep_weekly: 4`, `keep_monthly: 3` | nach Platz auf Terra anpassen |
| `restore_test.template` | `hermclaw_restore_template` | von der Rolle `postgresql` angelegt |

Variante B als vollständiger Block:

```yaml
backup:
  target: {kind: local, path: /mnt/terra-backup/hermclaw, require_mount: true}
```

### 11.5 Erster Lauf

```bash
sudo hermclaw backup list                              # {"sets": []} – Ziel erreichbar, Konfiguration gültig
sudo systemctl start hermclaw-backup.service           # dauert je nach Datenmenge Minuten
journalctl -u hermclaw-backup -n 30 -o cat             # letzte Zeile: JSON mit "ok": true und der Set-ID
sudo hermclaw backup verify --latest                   # Prüfsummen, Archive, Dump-Katalog
sudo systemctl start hermclaw-restore-test.service
journalctl -u hermclaw-restore-test -n 30 -o cat       # "ok": true; RESTORE_REPORT.json liegt neben dem Satz auf Terra
systemctl list-timers 'hermclaw-*'                     # nächste Läufe: täglich 02:30, sonntags 04:30
```

Exit-Codes der Befehle: `0` ok, `1` Prüfung/Restore-Test fehlgeschlagen, `2` Konfigurationsfehler, `3` Operation
fehlgeschlagen, `75` ein anderer Backup-Lauf hält die Sperre (zählt für die Timer als Erfolg, der nächste Lauf holt es
nach).

### 11.6 Überwachung

* täglich: `journalctl -u hermclaw-backup --since -26h` – ein erfolgreicher Lauf; Alarm, wenn 26 Stunden lang kein
  Event `backup.completed` kommt;
* wöchentlich: `journalctl -u hermclaw-restore-test --since -8d` – `"ok": true`; Alarm, wenn 8 Tage lang kein
  `backup.restore_test.passed` kommt;
* die Events `backup.*` erscheinen auch im Event-Stream der Oberfläche (`/api/events`).

### 11.7 Wiederherstellen

Eine Wiederherstellung (Totalverlust von .225 oder eine beschädigte Datenbank) beschreibt
`docs/runbooks/disaster-recovery.md` Schritt für Schritt: Host neu aufsetzen, Credentials aus dem Passwortmanager neu
anlegen, Satz wählen und testen (`sudo hermclaw backup restore-test <set-id>`), `restore-full` in eine leere
Datenbank, Konfiguration und Artefakte zurücklegen, `hermclaw migrate`, Dienste starten.

---

## 12. Live-Abnahme

Die Build-Umgebung konnte die Hosts nicht erreichen (BLOCKER-001). Diese Abnahme holt alle Prüfungen gegen die echte
Hardware nach. Sie braucht einen Quellcode-Checkout der installierten Version mit Test-Abhängigkeiten auf .225
(und für die Sandbox-Suite auf .222):

```bash
# auf .225 (und .222) als root
git clone https://github.com/MrHardstyle/Claude-Hermclaw.git /opt/hermclaw/src
cd /opt/hermclaw/src && git checkout v0.1.0rc1
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
chmod -R a+rX /opt/hermclaw/src                    # der Dienstbenutzer hermclaw muss lesen können

# einmalig auf .225: Testrolle + pgvector-Template für die Wegwerf-Testdatenbanken
sudo scripts/ops/live-acceptance.sh --prepare-db

# alle Suiten dieses Hosts (Orchestrator: workers models repo_intelligence research sshtools infratools wol gitops backup)
sudo scripts/ops/live-acceptance.sh
#   GitLab-Suite nur mit einem Wegwerf-Projekt:
sudo HERMCLAW_LIVE_GITLAB_PROJECT=hermclaw/sandbox HERMCLAW_LIVE_GITLAB_API=http://192.168.178.226 \
     HERMCLAW_LIVE_GITLAB_REPO_URL=git@192.168.178.226:hermclaw/sandbox.git scripts/ops/live-acceptance.sh --suite gitops
#   Wake-on-LAN vollständig (legt .224 schlafen und weckt ihn; kein Job darf laufen):
sudo scripts/ops/live-acceptance.sh --suite wol --full-wol

# auf .222: die Sandbox-Suite (rootless Podman, cgroup-v2-Limits, Netzwerk)
sudo scripts/ops/live-acceptance.sh

# danach aufräumen (.225)
sudo scripts/ops/live-acceptance.sh --cleanup-db
```

Ergebnis: `/var/lib/hermclaw/acceptance/<zeit>-summary.txt` plus JUnit-XML je Suite. Alles `PASS` (oder `SKIP` mit
Grund, z. B. GitLab ohne Wegwerf-Projekt) = abgenommen. Tragen Sie das Ergebnis in `STATUS.md` ein. Bei `FAIL`: das
Log der Suite lesen und Kapitel 14 prüfen.

---

## 13. Betrieb

| Aufgabe | Befehl / Ort |
|---|---|
| Status aller Dienste (.225) | `systemctl status hermclaw-api hermclaw-scheduler hermclaw-litellm postgresql@17-main` |
| SearXNG (rootless, Benutzer hermclaw) | `systemctl --user --machine=hermclaw@.host status hermclaw-searxng` |
| Worker | `.222`: `systemctl status hermclaw-exec-worker` · `.224`: `systemctl status hermclaw-model-worker ollama` |
| Logs (JSON) | `journalctl -u hermclaw-scheduler -f` (ebenso `-u hermclaw-api`, `-u hermclaw-litellm`) |
| Gesundheit | `sudo hermclaw doctor` · `curl -s http://192.168.178.225:8000/api/health?deep=true` |
| Geladene Modelle | auf .224: `ollama ps` |
| Speicherplatz | `df -h /var/lib/hermclaw /var/lib/ollama` (Hermclaw warnt unter 10 GB frei, stoppt neue Arbeit unter 2 GB) |
| Upgrade | `docs/runbooks/upgrade.md` (Backup → Scheduler stoppen → `playbooks/deploy.yml -e hermclaw_version=<neu>` → prüfen) |
| Rollback | `docs/runbooks/upgrade.md` → *Rollback* (vorheriger venv bleibt erhalten) |
| Ausfall eines Hosts | `docs/runbooks/disaster-recovery.md` |
| Umstieg vom alten System | `docs/runbooks/cutover.md` |
| Bekannte Einschränkungen | `docs/operations/known-limitations.md` |

Die Worker legen sich bei Leerlauf schlafen (`systemctl suspend`, Hosts mit `idle_sleep_command` in `hosts.yaml`).
Der Scheduler weckt sie vor jedem Schritt per Wake-on-LAN. Ein schlafender Worker ist also normal.

---

## 14. Fehlersuche

| Symptom | Ursache | Abhilfe |
|---|---|---|
| `doctor`: `database:pgvector` fail/warn | pgvector aus Debian (0.8.0) oder < 0.8.7 | `apt-cache policy postgresql-17-pgvector` (Quelle PGDG?), aktualisieren, `ALTER EXTENSION vector UPDATE;` in `hermclaw`, `systemctl restart postgresql@17-main` |
| `doctor`: `config:files` fail | `/etc/hermclaw/*.yaml` fehlen, nur `.example.yaml` vorhanden | Deployment erneut ausführen (Kapitel 8) |
| `doctor`: `config:wake_on_lan` warn | Platzhalter-MAC im Inventar | `wol_mac` setzen (Kapitel 4.2), Deployment erneut |
| `doctor`: `litellm` fail, Gateway meldet `MODEL_AUTH_FAILED` | Master-Key fehlt/falsch | `hermclaw-credential check litellm-master-key`; `systemctl restart hermclaw-litellm hermclaw-scheduler` |
| `doctor`: `models:model-224` fail | Modell nicht geladen | `hermclaw-model-pull` auf .224 (Kapitel 7) |
| Modelle laufen auf der CPU (`ollama ps`: `100% CPU`) | Treiber zu alt / nicht geladen, nach Suspend GPU verloren | `nvidia-smi`; `journalctl -u ollama -b \| grep 'inference compute'`; nach Suspend: `rmmod nvidia_uvm && modprobe nvidia_uvm`, `systemctl restart ollama` |
| `nvidia-smi`: „couldn't communicate with the NVIDIA driver“ nach Kernel-Update | DKMS-Modul fehlt | `dkms status`; `dkms autoinstall`; Neustart |
| Schritte scheitern mit `WORKER_UNREACHABLE` | Worker schläft und wacht nicht auf | WoL im BIOS/`ethtool … \| grep Wake-on: g`, MAC in `hosts.yaml`, `sudo hermclaw doctor` |
| Schritte scheitern mit `SANDBOX_IMAGE_UNSAFE` | Image mit gefährlichen Metadaten (Podman-CVEs) | nur die von `hermclaw-build-sandbox-images` gebauten Images verwenden |
| `SANDBOX_ENV_NOT_ALLOWED` | Projekt-Befehl setzt eine nicht erlaubte Umgebungsvariable | `policies.sandbox.env_allowlist` in `policies.yaml` ergänzen |
| SSH-Schritt wartet | Mutation braucht Freigabe | UI → Job → „Approvals“ (Admin-Token) |
| UI lädt, Live-Status bleibt stehen | nginx puffert SSE | `nginx -T \| grep -A3 events/stream` muss `proxy_buffering off` zeigen; Deployment erneut |
| Backup-Timer meldet Exit 75 | ein anderer Backup-Vorgang hält die Sperre | nichts tun, der nächste Lauf wiederholt |
| `hermclaw-searxng` startet nicht | Podman-Secret fehlt (gewollt: kein Start mit bekanntem Schlüssel) | Deployment erneut ausführen (legt das Secret an) |

Weitere Hinweise: `docs/operations/bootstrap.md` (Bootstrap-Fehler), `docs/operations/backup-restore.md`
(*Troubleshooting*), `docs/architecture/*.md` (Fehlercodes je Komponente).
