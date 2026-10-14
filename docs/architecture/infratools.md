# Container- und Proxy-Tools (`hermclaw.infratools`, P28)

Bauplan §31 „Docker / Reverse Proxy“, §6 Portplan, Phase 28 (28.1–28.6).

| Tool (§31) | Implementierung | Schritt |
|---|---|---|
| `container_list` / `container_inspect` | `containers.ContainerInventory` | 28.1 |
| `compose_validate` | `compose.ComposeValidator` (+ `validate_structure`) | 28.2 |
| `compose_up_sandbox` | `compose_sandbox.ComposeSandbox` | 28.3 |
| Port-Kollision | `ports.PortChecker` (Portplan + `ss`) | 28.4 |
| `nginx_test` / `proxy_config_validate` | `nginx.NginxTester` / `nginx.ProxyConfigValidator` (Parser: `nginxconf`) | 28.5 |
| Rollback-Config | `rollback.RollbackStore`, `rollback.ConfigDeployer` | 28.6 |
| Tool-Engine-Adapter | `provider.InfraOpsProvider` / `build_provider` | – |

`hermclaw.infratools.InfraTools` bündelt alles über **einen** `HostExecutor` und bietet zusätzlich
`predeploy_check(kind, text, host, owner)` – das §31-Gate „syntax, port collision, existing service inventory“ – und
`deploy_config(...)`, das die „rollback config“ ergänzt.

## Einbindung in die Runtime (`provider.py`)

`hermclaw.runtime.ops.build_ops` lädt `hermclaw.infratools.provider.build_provider(config, sessionmaker, settings,
ssh_runner=…)`. `InfraOpsProvider` bedient die sechs §31-Tools der Tool-Registry (`ContainerQueryArgs`,
`ContainerInspectArgs`, `ComposeArgs`, `ConfigSourceArgs`), prüft das Capability-Gate erneut (`docker`, `deploy`),
beschränkt Dateiargumente auf den Job-Workspace (keine absoluten Pfade, kein `..`, keine Symlink-Ausbrüche) und
liefert gekürzte, redigierte `OpsResult`s. Container und Compose-Sandbox laufen auf dem Ausführungshost: dem
`execution_worker` aus `hosts.yaml`, wenn die Runtime remote ausführt (Produktion, über den SSH-Runner), sonst lokal.
Der Start-Hook `cleanup_orphan_sandboxes` räumt Sandbox-Container abgestürzter Läufe beim Scheduler-Start.
Rollback/Deploy (`deploy_config`, `rollback_restore`) und `predeploy_check` sind Runtime-APIs von `InfraTools`
(noch keine Modell-Tools in der Registry).

## Ausführung (`process.py`)

* Alle Kommandos sind **argv-Listen** aus validierten Teilen (Containernamen/Filter per Regex, Pfade normalisiert);
  nie ein Shell-String des Modells.
* `host=None|"local"`: Subprozess ohne Shell, minimale Umgebung (PATH/Locale + Engine-Variablen wie `DOCKER_HOST`,
  `CONTAINER_HOST`, Proxy), eigene Prozessgruppe; Timeout/Cancel tötet die Gruppe.
* Andere Hosts müssen in `hosts.yaml` stehen; der argv wird mit `shlex.join` zu **einer** Zeile quotiert und an den
  injizierten `RemoteRunner` (`async run(host_id, command, timeout) -> (rc, out, err)`, Produktion:
  `hermclaw.sshtools.client.SshRunner` mit Host-Allowlist, Host-Key-Pinning, Klassifikation/Approval) übergeben.
  Runner mit `stdin`-Keyword können Dateien übertragen (Sandbox-Staging, Rollback-Restore).
* Jede Ausführung wird optional als `command_runs`-Zeile auditiert (redigiert). `raw_stdout=True` liefert
  unredigierten stdout (JSON der Engines, Dateiinhalte für Checksummen) – solche Ausgaben landen **nie** im Audit.
* `LocalRunner` erfüllt das RemoteRunner-Protokoll auf dieser Maschine; die Tests treiben damit alle Remote-Pfade
  (Quoting, Staging, `stat`/`cat`/`base64`/`sha256sum`/`install`) gegen echte Prozesse.

## 28.1 Inventur

* Podman `ps --format json` (Array) / Docker `ps --no-trunc --format '{{json .}}'` (JSON pro Zeile), `inspect --type
  container`; normalisiert zu `ContainerInfo` (engine, host, id, name, image, state, status, health, ports, labels,
  networks, mounts, restart_count, exit_code, privileged, network_mode, restart_policy, `env_keys`).
* `engine="auto"` fragt beide Engines; eine fehlende Engine oder ein gestoppter Daemon steht in `engines[]`, ohne den
  Aufruf scheitern zu lassen. **Umgebungswerte werden nie zurückgegeben**, nur die Variablennamen; Labels und Command
  werden nach dem Parsen redigiert.
* JSON wird *vor* der Redaction geparst: die Redaction des SSH-Transports macht aus `"Passwd": true` sonst
  `"Passwd": ***REDACTED***` (ungültiges JSON) – `loads_lenient` quotet nackte Marker (Regressionstest).

## 28.2 `compose_validate`

1. **Engine-Check** mit der echten Compose-Implementierung: `docker compose -f … config --quiet`
   (Compose-Spec-Schema, funktioniert ohne Daemon), sonst `docker-compose`, `podman-compose -f … config`,
   `podman compose`. Keine Implementierung → `skipped` mit Grund. Remote-`include`/`extends` (git/OCI/http) werden
   nie an die Engine gegeben (SSRF-Schutz, Fehler `COMPOSE_REMOTE_REFERENCE`).
2. **Strukturprüfung** (PyYAML mit YAML-1.2-Core-Schema wie Compose: `yes` bleibt String, `!reset`/`!override`
   akzeptiert; Alias-Limit 200 und Expansionslimit 200 000 Knoten gegen „billion laughs“):
   Services mit `image` oder `build` (Kontext/Dockerfile vorhanden), Portsyntax (short/long, IPv6, Ranges,
   Variablen), doppelte Host-Ports (familienbewusst: `0.0.0.0:80` und `[::]:80` koexistieren), Volumes
   (Ziel absolut, benannte Volumes deklariert, Bind relativ/absolut, außerhalb des Projekts), `env_file`-Existenz,
   `depends_on`/`networks`-Referenzen, `configs`/`secrets`-Dateien, unquotierte `a:b`-Ports (YAML-1.1-Basis-60-Falle).
   Sicherheitsregeln (Fehler, per `ComposePolicy`-Flag erlaubbar): `privileged: true`, `network_mode: host`,
   Host-Namespaces (`pid/ipc/uts/userns_mode/cgroup: host`), Engine-Sockets (`docker.sock`, `podman.sock`,
   `containerd.sock`, `/run/docker…`), sensible Host-Pfade (`/`, `/etc`, `/root`, `/var/lib/docker` …),
   `cap_add ALL/SYS_ADMIN/SYS_MODULE`, `security_opt …unconfined`.

## 28.3 `compose_up_sandbox`

1. Strikte Validierung (absolute Binds sind Fehler) plus Sandbox-Regeln: alles, was die Engine *außerhalb* des
   Projekts lesen oder anhängen würde, wird abgelehnt – `env_file`, Build-Kontext/Dockerfile, `configs/secrets`-Dateien
   außerhalb, `secrets.environment`, externe Netzwerke/Volumes, `include`, `extends` aus anderen Verzeichnissen,
   `volumes_from`/`external_links`/`container:<x>`-Namespaces, `devices`, `cap_add`, Symlink-Ausbrüche.
2. Engine: Podman bevorzugt, Docker als Adapter (`SandboxOptions.engine_preference`); keine nutzbare Engine →
   `skipped` mit Gründen (z. B. „Docker-Daemon läuft nicht“).
3. **Staging**: Kopie des Projekts (ohne `.git`, Caches) – lokal in ein `0700`-Tempverzeichnis, remote als tar-Stream
   per stdin in `mktemp -d /tmp/hermclaw-sbx-<id>-XXXXXXXX`. Container, die in Bind-Mounts schreiben, verändern den
   Job-Workspace nicht.
4. **Sandbox-Compose-Datei** (vollständige Datei statt Merge-Override, siehe Entscheidung unten): Projektname
   `hermclaw-sbx-<id>`, jeder veröffentlichte/ephemere Port → `127.0.0.1:<freier Port>:<container>/<proto>`
   (lokal per bind(0), remote zufällig in 20000–32000 und nicht in `ss -ltnH/-lunH`), `container_name` entfernt,
   `restart: "no"`, Label `hermclaw.sandbox=<id>`, keine Host-Netzwerke.
5. `up -d`, Polling per `inspect` bis alle erwarteten Container `running` (und ggf. `healthy`) sind und
   `settle_seconds` stabil bleiben – oder einer mit Exit≠0 endet / `unhealthy` wird / der Timeout abläuft.
   Podman plant Healthchecks über systemd-Timer; ohne systemd (Container, Minimalhosts) bleiben sie `starting` –
   die Sandbox stößt sie deshalb mit `podman healthcheck run` an.
6. Logs (tail, redigiert) aller Container, Prüfung der tatsächlichen Bindings (nur zugewiesene Loopback-Ports),
   TCP-Probe der lokalen Ports.
7. **Teardown immer** (auch bei Fehler/Timeout/Cancel, eigenes Zeitbudget): `down -v --remove-orphans`, Label-Sweep
   über Container/Netzwerke/Volumes, Entfernen des Stagings; Reste stehen in `teardown.leftovers`.
   `cleanup_stale_sandboxes` räumt Reste abgestürzter Läufe (Label `hermclaw.sandbox`).

**Entscheidung „volle Datei statt Override“:** Compose merged `ports` additiv – ein Override kann die Original-Host-Ports
nicht entfernen; `!reset` versteht podman-compose 1.0.x nicht. Die generierte Datei ist ein vollständiges Modell
(inkl. inline aufgelöster `extends`) und wird allein mit `-f` übergeben. Unit-Tests prüfen die Generierung
(`tests/unit/test_infratools_sandbox_model.py`).

## 28.4 Port-Kollisionen

* Angefragte Host-Ports aus Compose (`ports`), nginx (`listen`, implizit 80, mehrere Server teilen sich einen Socket)
  oder systemd (`ListenStream/ListenDatagram`, heuristisch `--port`, `--bind host:port`, `*_PORT=`).
* Vergleich mit (a) dem **Portplan §6** (`BAUPLAN_PORT_PLAN`, Owner pro Port; Adresse aus `hosts.yaml`) – nur der
  Owner (`owner=`) darf den Port nehmen – und (b) den **Live-Sockets** des Zielhosts (`ss -ltnpH`/`-lunpH`, lokal
  oder remote); ein vom Owner-Prozess gehaltener Port ist erwartet. Kollisionen tragen Owner, Prozess und PID.
* `predeploy_check` ergänzt die Container-Inventur: rootful Podman veröffentlicht per netavark-DNAT ohne
  lauschenden Socket – `PORT_USED_BY_CONTAINER` fängt diese Fälle.

## 28.5 nginx

* `nginx_test`: echtes `nginx -t -q -p <prefix>/ -c <conf> -e stderr` in einem privaten Prefix; Site-Snippets werden in
  einen minimalen Wrapper (`events {}` + `http { include site.conf; }`) eingebunden, Haupt-Configs direkt getestet,
  Zusatzdateien (`extra_files`, nur relative Pfade) daneben gelegt. **`nginx -t` bindet die listen-Sockets** (nur
  `EADDRINUSE` wird im Testmodus toleriert – fremde Adressen scheitern mit `EADDRNOTAVAIL`, Ports < 1024 ohne Root mit
  `EACCES`) und öffnet/erzeugt Log-, PID-, Temp- und Cache-Pfade. Daher schreibt `rewrite_for_test` für den Test
  `listen` auf freie Loopback-Ports um (geteilte Sockets bleiben geteilt, `ipv6only` entfällt, Unix-Sockets in den
  Prefix) und verlegt `access_log/error_log/pid/lock_file/*_temp_path/*_cache_path` in den Prefix. Zeilennummern
  bleiben erhalten, Meldungen werden auf Originaldatei und Original-`listen` zurückgemappt. Kein nginx installiert →
  `skipped`. `test_host()` prüft die installierte Config eines Hosts (`nginx -t` über den Runner) nach dem Upload.
* `proxy_config_validate` (eigener Tokenizer nach `ngx_conf_read_token`, Zeilen + Offsets):
  doppelte `server_name`/`listen`-Paare und `default_server`; Event-Stream-Locations (`events/stream`,
  `event-stream`, `/sse`, auch Regex-Locations; zusätzlich Probe-Pfade über nginx' Location-Auswahl) brauchen
  `proxy_buffering off`, `proxy_http_version 1.1`, `proxy_read_timeout ≥ 60s` (Vererbung aus server/http zählt;
  `Connection ""`, Cache, gzip, `chunked_transfer_encoding off` als Warnung, Research 20261008-014);
  `proxy_pass`-Ziele nur Loopback, Adressen aus `hosts.yaml` oder Allowlist (Namen werden aufgelöst, Upstream-Server
  einzeln geprüft, Request-Variablen im Host = Open Proxy/SSRF-Fehler); kein `autoindex on`; TLS bei 443/`ssl`
  (ssl-Flag, Zertifikat+Key, kein SSLv2/3, TLSv1/1.1 und schwache Cipher als Warnung).
  Das Repo-Template `infra/ansible/roles/nginx/templates/hermclaw-next.conf.j2` besteht beide Prüfungen (Test).

## 28.6 Rollback-Artefakte und Deployments

* `RollbackStore.create(target, host)` **vor** jeder Änderung: vorheriger Inhalt (byte-genau) als `0600`-Datei unter
  `settings.artifacts_dir/<job|adhoc>/rollback/<artifact-id>/<name>.orig` (bzw. `.absent`, wenn die Datei noch nicht
  existiert) + `manifest.json`; `artifacts`-Zeile `kind=rollback` mit SHA-256, Größe und `metadata.infratools`
  (Host, Zielpfad, Mode/Owner, Restore-Kommando, Reload-argv).
* Remote-Lesen: `stat` → `sha256sum` → `cat` (unredigiert) mit Checksummenvergleich; verändert der Transport die
  Bytes (Redaction, Nicht-UTF-8), Fallback auf `base64 -w 0`. Zielpfade: absolut, normalisiert, unter erlaubten
  Wurzeln (`DEFAULT_DEPLOY_ROOTS`), keine Symlinks (Datei und – lokal per `realpath`, remote per `readlink -f` –
  Elternverzeichnis).
* `restore(id)`: Zeile laden, Pfad im Artefaktverzeichnis erzwingen, **Checksumme von Datei, Zeile und Manifest prüfen,
  bevor das Ziel angefasst wird**, Inhalt atomar zurückschreiben (lokal tmp+fsync+rename, remote `install -m`),
  Ziel-Checksumme verifizieren, optional Reload; `deployments`-Zeile `kind=restore:<kind>` + Event.
* `ConfigDeployer.deploy`: `deployments`-Zeile `started` per Advisory-Lock (ein Deployment je Ziel), Rollback-Artefakt
  anlegen und als `rollback_artifact_id` eintragen, schreiben, Checksumme prüfen, Post-Check (z. B. `nginx -t`),
  Reload; jeder Fehler und jeder Abbruch (Cancel) stellt den alten Zustand automatisch her
  (`rolled_back`, oder `rollback_failed`, wenn die Wiederherstellung nicht verifiziert werden konnte). Events
  `deployment.started` / `deployment.finished`.

## Verifizierte Fakten (Research, in dieser Umgebung geprüft)

| Fakt | Quelle / Nachweis |
|---|---|
| `docker compose config --quiet` validiert gegen das Compose-Schema ohne laufenden Daemon | Docker-Compose-Doku „compose config“; Test `test_engine_detection_and_schema_validation` (Compose v5.6.0) |
| Compose merged `ports` additiv; `!reset` erst in Compose ≥ 2.24, nicht in podman-compose 1.0.6 | Compose-Spec „merge“; podman-compose 1.0.6 lädt mit PyYAML ohne Tags |
| `nginx -t` öffnet listen-Sockets, toleriert im Testmodus nur `EADDRINUSE` | nginx `ngx_open_listening_sockets`; Test `test_listen_on_foreign_address_and_busy_port_is_rewritten` |
| Podman-Healthchecks laufen über systemd-Timer; `podman healthcheck run` führt sie manuell aus | podman-healthcheck-run(1); Sandbox-Test „healthy“ ohne systemd |
| Rootful Podman (netavark) veröffentlicht Ports per DNAT ohne lauschenden Socket | Test `test_predeploy_finds_ports_published_by_containers` |
| SSE hinter nginx: `proxy_buffering off`, `proxy_http_version 1.1`, `Connection ""`, langer `proxy_read_timeout` | Research 20261008-014, nginx proxy-Modul |

Installiert wurde für die Tests nichts zusätzlich: nginx 1.24, Podman 4.9.3, podman-compose 1.0.6 und das
Docker-Compose-Plugin v5.6.0 waren im Build-Container vorhanden; der Docker-Daemon läuft dort nicht (die Sandbox
fällt auf Podman zurück, eine reine Docker-Präferenz meldet `skipped`).

## Grenzen / offene Punkte

* **BLOCKER-001:** keine Läufe gegen `.222/.223/.225`; alle Remote-Pfade sind über `LocalRunner` gegen echte Prozesse
  getestet. Live-Abnahme: `container_list`/`predeploy_check`/`deploy_config` gegen `.223` (nginx) und `.222`
  (Sandbox) mit dem `SshRunner`.
* Unter der Standard-SSH-Policy gelten `podman/docker inspect|logs`, `sha256sum`, `base64`, `readlink` als „mutate“
  (nicht in `read_patterns`) und brauchen eine Freigabe – Policy-Ergänzung siehe Shared Changes.
* `nginx_test` prüft Zertifikatsdateien am Orchestrator; liegen sie nur auf `.223`, ist `test_host()` nach dem Upload
  maßgeblich. Nur die Haupt-/Site-Datei wird umgeschrieben, nicht per absolutem `include` eingebundene Dateien.
* Die Sandbox begrenzt CPU/RAM der Compose-Container nicht (die Compose-Datei bestimmt das); Images, die `build:`
  erzeugt, bleiben nach dem Teardown im Image-Store.
