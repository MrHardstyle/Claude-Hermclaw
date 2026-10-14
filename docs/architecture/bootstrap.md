# P01 – Clean System Bootstrap (infra)

Status: implemented and tested locally; live execution on the target hosts is blocked by
BLOCKER-001 (192.168.178.x not reachable from the build environment). Operator runbook:
[`docs/operations/bootstrap.md`](../operations/bootstrap.md).

## Purpose

Bring four fresh Debian 13 "trixie" hosts into the state the Hermclaw Next runtime expects
(Bauplan §2, §5, §6, Phase 1 steps 1.1–1.15), reproducibly and idempotently, without ever
taking over services that already run there:

| Host | Group | Roles (in order) |
|---|---|---|
| `.225` Orchestrator | `orchestrator` | common, postgresql, podman, systemd_skeleton |
| `.223` WebUI | `webui` | common, nodejs, nginx |
| `.222` Execution worker | `execution_workers` | common, podman, wol_target, systemd_skeleton |
| `.224` Model / media worker | `model_workers` | common, nvidia, ollama, wol_target, systemd_skeleton |

GitLab `.226` and Terra/Unraid `.60` stay in the inventory (for `playbooks/inventory.yml` and
known_hosts) but are never bootstrapped. Host roles are fixed by the architecture.

The bootstrap installs and configures; it does **not** deploy Hermclaw code and does **not**
enable Hermclaw units (`systemd_skeleton_enable_units: false`). The deployment phase (P32)
extends `playbooks/site.yml`.

## Layout

```
infra/ansible/
  ansible.cfg                       # ansible-core only (no Galaxy collections), strict host keys
  inventory/hosts.example.yml       # copied to hosts.yml by bootstrap-control-node.sh
  playbooks/bootstrap.yml           # P01, one play per group
  playbooks/site.yml                # imports bootstrap.yml (P32 appends deployment plays)
  playbooks/inventory.yml           # P00 read-only inventory (pre-existing)
  playbooks/group_vars/all.yml      # shared variables: paths, users, port plan, switches
  roles/<role>/{defaults,tasks,templates,handlers,meta}
infra/systemd/                      # unit/env templates, rendered by the roles
scripts/bootstrap/
  bootstrap-control-node.sh         # admin machine: ansible-core venv, inventory, syntax check
  check-host.sh                     # read-only host preflight (Debian, CPU/RAM/disk, port plan)
  hermclaw-credential.sh            # encrypted systemd credentials (installed as /opt/hermclaw/bin/hermclaw-credential)
```

## Roles

### common (1.1–1.6, 1.9)
* **1.1** asserts Debian 13 / trixie / x86_64 (`hermclaw_debian_major`, `hermclaw_debian_codename`)
  and runs `check-host.sh --json --role <groups>` (read-only) before anything changes. A FAIL
  (wrong OS, too little CPU/RAM/disk, a port of the §6 plan held by a foreign process, a
  foreign unit file with a Hermclaw name) stops the play for that host.
* base packages (`common_base_packages`), time zone + systemd-timesyncd (an existing chrony/ntpsec is kept).
* **1.2** system group/user `hermclaw` (home `/var/lib/hermclaw`, `/usr/sbin/nologin`, locked password).
* **1.3** directories with owners/modes from `common_directories`: `/opt/hermclaw` (0755 root:hermclaw),
  `/opt/hermclaw/bin`, `/etc/hermclaw` (0750), `/etc/hermclaw/{secrets,ssh}` (0750),
  `/var/lib/hermclaw/{workspaces,artifacts,repos}` (0750 hermclaw), `/var/log/hermclaw` (0750),
  `/etc/credstore.encrypted` (0700 root), workers additionally `/var/lib/hermclaw/worker`.
* **1.4** SSH keys (research 20261008-021): `id_ed25519` (runtime) and `backup_ed25519` are generated
  on the orchestrator only (`/etc/hermclaw/ssh`, private 0600 hermclaw). On `.222/.223/.224` the
  restricted login `hermclaw-admin` gets the runtime key as
  `restrict,from="<orchestrator ip>"[,command="…"] ssh-ed25519 … hermclaw-runtime@orchestrator`;
  its sudo rights are only `/etc/sudoers.d/hermclaw-runtime` (workers: `systemctl suspend`,
  validated with `visudo -cf`). The play refuses a runtime user in sudo/wheel/admin/root and an
  Ansible login equal to the runtime user. Target host keys are read over the trusted Ansible
  connection into `/etc/hermclaw/ssh/known_hosts` (no TOFU); GitLab/Terra keys come
  pre-verified from `common_known_hosts_extra`.
* **1.5/1.6** python3 (Debian 13: 3.13, gate `>= 3.12`), venv, pip, pipx, build tooling, libpq-dev;
  empty application venv `/opt/hermclaw/venv` (root-owned, filled by P32).
* **1.9** Git (`>= 2.39`). Git mutations remain runtime-controlled; the bootstrap never touches repositories.

### postgresql (1.7, 1.8) – orchestrator
PGDG apt repository (`deb822`, `trixie-pgdg`, signed-by key URL) with an apt pin for pgvector,
`postgresql-17`, `postgresql-17-pgvector`. **pgvector >= 0.8.2 is asserted twice** (CVE-2026-3172,
Debian's 0.8.0 is affected): the dpkg package version (epoch/revision aware) and the active
`extversion` in the database. Cluster `17/main` must use port 5432 (a foreign cluster is never
taken over). `conf.d/hermclaw.conf` (`listen_addresses = 'localhost'`, values quoted by a macro,
unknown keys dropped) and `pg_hba.conf` (peer for local, scram-sha-256 for 127.0.0.1/::1 only,
operator rules validated: no trust/md5/password) are validated **before** the reload/restart
handlers run (`tasks/validate_config.yml`):
`postgres -C data_directory` parses every config file like a server start (catches invalid
values of restart-only parameters too), and `pg_hba_file_rules`/`pg_file_settings` report
line-level errors. A restart-only value that is merely pending (e.g. `shared_buffers` on the
first run) is not an error. On errors the previous files are restored (a new broken drop-in is
removed) and the play stops; the running server is never reloaded with a broken file.
Role `hermclaw` (LOGIN, no other privileges, no password – peer only), database `hermclaw`
(UTF8, template0), extension `vector`: idempotent DDL via psql variables and `format(%I)`.

### podman (1.10) – orchestrator (SearXNG) and execution worker
podman (>= 5.0), uidmap, slirp4netns, passt, fuse-overlayfs, catatonit, crun, dbus-user-session.
Sub-UID/GID ranges for `podman_users` (first free multiple of 65536 at or above 100000, existing
ranges are never moved), `/etc/containers/containers.conf.d/50-hermclaw.conf` (pasta),
cgroup v2 delegation drop-in `/etc/systemd/system/user@.service.d/delegate.conf`
(`Delegate=cpu cpuset io memory pids`), lingering, and a final `podman info` as the user that
asserts rootless mode, cgroup v2 and the delegated controllers.

### nodejs (1.11) – WebUI
NodeSource `node_22.x` (`nodistro`) with an apt pin; gate: major 22 and `>= 22.12.0`
(Vite: research 20261008-018); npm version recorded.

### nginx (1.12) – WebUI
nginx, webroot `/var/www/hermclaw-next` (placeholder page, never overwrites a deployed UI),
site `sites-available/hermclaw-next.conf` + link. Baseline: SPA fallback, `/api/` proxy with
upstream keepalive and `X-Forwarded-*`, unbuffered SSE locations
(`^/api/(jobs/[^/]+/)?events/stream$`: `proxy_buffering off`, `proxy_cache off`, `gzip off`, 1 h
read timeout), security headers, `server_tokens off`. `listen [::]` only when the host has an
IPv6 address (`nginx_listen_ipv6`; nginx does not start with it on a kernel without IPv6 – found
by the real-nginx test). It refuses a foreign site with the same name and a `nginx.conf` that does
not include `sites-enabled/`; never claims `default_server` unless asked. `nginx -t` runs before
the reload; on failure the previous site is restored / the new link removed and nginx is not
reloaded. The TLS/rate-limit site is P32 (`infra/nginx/`).

### nvidia (.224) – research 20261008-017, recheck 20261014-026
GTX 1080 (Pascal, CC 6.1): the **580 branch is the last with Pascal support; 590+ dropped it**.
Debian 13 ships 550 and NVIDIA's debian13 CUDA repository only 590+, and Ollama >= 0.30 needs a
driver >= 570 to use CC 6.1 GPUs, so the default source is NVIDIA's official `.run` installer of a
pinned 580 release (`nvidia_runfile_version`, SHA-256 verified, `--dkms
--kernel-module-type=proprietary`). `/etc/apt/preferences.d/hermclaw-nvidia.pref` blocks every apt
NVIDIA driver package (runfile) or prefers 580 and blocks 590+ incl. epoch forms (sources
`cuda`/`debian`, usable once a repository ships 580 for trixie – candidates are checked first).
nouveau is blacklisted, conflicting driver packages are refused, `nvidia-persistenced` gets a
unit, and `nvidia-smi` must report a `GTX 1080` with >= 8000 MiB (never the 1080 Ti of the gaming PC).
Reboot only with `nvidia_allow_reboot: true`, otherwise the play stops with a hint.

### ollama (.224)
Pinned release tarball (`ollama_version`, `>= 0.32.12`, checksum from the release's
`sha256sum.txt`) under `/opt/ollama/<version>` with a `current` link, or the official script with
`OLLAMA_VERSION` (refused without a working NVIDIA driver – the script would install 590+).
Drop-in `ollama.service.d/hermclaw.conf`: `OLLAMA_HOST=0.0.0.0:11434`, `OLLAMA_KEEP_ALIVE`,
`OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_NUM_PARALLEL=1`, flash attention/KV cache settings,
`OLLAMA_NO_CLOUD=1`, `OLLAMA_VULKAN=0`, and **`UnsetEnvironment=OLLAMA_CONTEXT_LENGTH`** (Hermclaw
sends `num_ctx`). `ollama_extra_env` is allowlisted (no managed keys, no newlines). Because Ollama
has no authentication, `hermclaw-ollama-firewall.service` loads a dedicated nftables table that
drops connections to 11434 except from loopback and `ollama_firewall_allowed_sources`
(orchestrator); other tables are never touched. After start: API version must equal the pin,
and the journal must show an inference compute device of library CUDA.

### wol_target (.222/.224)
`hermclaw-wol@<if>.service` (oneshot, `ethtool -s <if> wol g`, after the device unit) enabled
persistently; checks magic-packet support (`Supports Wake-on` contains `g`), compares the
inventory MAC with the interface (placeholder MAC only warns) and verifies `Wake-on: g`.

### systemd_skeleton (1.13)
Installs (not enables) `hermclaw-api`, `hermclaw-scheduler`, `hermclaw-litellm`,
`hermclaw-backup.{service,timer}` (orchestrator), `hermclaw-exec-worker` / `hermclaw-model-worker`
(workers), the SearXNG Quadlet (`/etc/containers/systemd/users/<uid>/hermclaw-searxng.container`,
rootless, `127.0.0.1:8888`, pinned image, podman secret), environment files
`/etc/hermclaw/{hermclaw,worker}.env` (0640, references only – `cred:<name>`), and
`/opt/hermclaw/bin/{hermclaw-cred-env,hermclaw-credential}`. Foreign unit files with the same name
are never overwritten (marker `Managed by Hermclaw`). Units are hardened (`ProtectSystem=strict`,
`NoNewPrivileges`, empty capability sets, …) and load secrets with
`LoadCredentialEncrypted=<name>:/etc/credstore.encrypted/hermclaw-<name>` (research 20261008-020).
Hermclaw-defined secrets (`api-admin-token`, `litellm-master-key`, `worker-token-<id>`) are
generated on the orchestrator with `hermclaw-credential generate`; each worker receives its
token re-encrypted with its own host key (decrypted on the orchestrator with `no_log`, passed
via stdin). `gitlab-token` is issued by GitLab and set manually.

## Public interfaces

```
ansible-playbook playbooks/bootstrap.yml [--limit GROUP] [--tags TAG] [--check --diff]
ansible-playbook playbooks/site.yml                 # == bootstrap until P32
scripts/bootstrap/bootstrap-control-node.sh [--venv DIR] [--inventory FILE] [--ansible-bin-dir DIR]
                                            [--python PYTHON] [--skip-syntax-check]
scripts/bootstrap/check-host.sh --role GROUP[,GROUP] [--json] [--min-ram-mb N] [--min-cpus N]
                                [--min-disk-gb N] [--allow-conflicts] [--port-plan FILE] [--os-release FILE]
    exit 0 = ok (warnings possible), 1 = at least one FAIL, 64 = usage error
/opt/hermclaw/bin/hermclaw-credential set NAME [--force] | generate NAME [--bytes N] [--force] | check NAME | list
    secrets only via stdin; never printed; exit 64 usage, 65 empty/undecryptable, 66 missing, 69 no systemd-creds, 73 exists
/opt/hermclaw/bin/hermclaw-cred-env VAR=credential [...] -- command [args...]
    exports $CREDENTIALS_DIRECTORY/<credential> (first line) as VAR, then execs; 78 missing/empty, 64 usage
```

Tags (`ansible-playbook playbooks/bootstrap.yml --list-tags`): `preflight`, `common`, `packages`, `time`,
`users`, `directories`, `python`, `git`, `ssh_keys`, `postgresql`, `podman`, `nodejs`, `nginx`, `nvidia`,
`ollama`, `firewall`, `wol`, `systemd`, `credentials`, `searxng`.

## Config keys (group_vars/all.yml, inventory)

`hermclaw_debian_major/codename`, `hermclaw_user/group/home`, `hermclaw_ssh_runtime_user`,
`hermclaw_install_dir/venv/bin_dir/config_dir/ssh_dir/data_dir/log_dir/credstore_dir/webroot`,
ports (`hermclaw_api_port` 8000, `_litellm_port` 4000, `_postgres_port` 5432, `_metrics_port` 9100,
`_worker_port` 8787, `_ollama_port` 11434, `_searxng_port` 8888), `hermclaw_port_plan` (kept in
sync with the table in `check-host.sh` by a test), `hermclaw_install_packages`,
`hermclaw_manage_services` (false only for image/chroot builds), `hermclaw_timezone`.
Inventory: `ansible_host`, `ansible_user` (own sudo admin, not `hermclaw-admin`),
`hermclaw_orchestrator_ip`, `hermclaw_lan_broadcast`, `hermclaw_web_server_name`,
`hermclaw_worker_id`, `wol_mac`, `wol_interface`. Role keys are documented in each
`roles/<role>/defaults/main.yml` and validated in `roles/<role>/tasks/validate.yml`
(identifiers, URLs https-only, no newline/field injection into unit, nginx, pg_hba or sudoers files).

## Data / event flow

The bootstrap writes no Hermclaw events (the runtime and its event store do not exist yet).
Observable output: the Ansible recap per host (`changed=0` on reruns), `check-host.sh` JSON
reports, the files listed above. Secrets flow only orchestrator → `systemd-creds encrypt` →
`/etc/credstore.encrypted` → `LoadCredentialEncrypted` → `$CREDENTIALS_DIRECTORY`; they never
appear in env files, unit files, command lines or Ansible output (`no_log` on every secret task,
enforced by `test_secret_handling_tasks_are_no_log`).

## Failure behaviour

* Preflight FAIL (wrong OS, resources, port conflicts, foreign units) → that host stops before
  any change (`--allow-conflicts` / `common_preflight_allow_conflicts` only deliberately).
* Validation asserts in every role stop on bad input before files are written.
* nginx and PostgreSQL configs are validated with the real binaries before reload/restart, with
  rollback; sudoers with `visudo -cf`; systemd units are loaded with `daemon-reload` only.
* Workers fail with a clear message when the orchestrator was not bootstrapped first (worker token).
* nvidia: missing/old driver, wrong GPU or VRAM → fail; reboot only when allowed.
* Every task is idempotent (`creates:`/`changed_when:` on all command tasks, enforced by a test);
  a rerun converges after any partial failure.

## Tests (1.15)

| File | What it proves |
|---|---|
| `tests/integration/test_bootstrap_infra.py` | YAML parse, `--syntax-check` of every playbook, structure (roles per group), port/unit plan sync, FQCN + idempotency guards on command tasks, `no_log` on secrets, every template renders, rendered content (PostgreSQL local only + quoting, pg_hba ordering, SSE site, credentials not secrets, env files accepted by `Settings` / `WorkerDaemonSettings`, Ollama/NVIDIA policy, Quadlet dry-run), `systemd-analyze verify` of all units (+ negative control), shellcheck, script behaviour (`hermclaw-cred-env`, `hermclaw-credential` with real `systemd-creds`, `check-host.sh` incl. real port conflicts, `bootstrap-control-node.sh`), role input validation matrix, ansible-lint production profile (when `HERMCLAW_ANSIBLE_LINT`/`ansible-lint` is available) |
| `tests/integration/test_bootstrap_roles.py` | task files against fake host tools (ethtool, nginx.conf include, apt candidates, runfile conflicts) and the real kernel (nftables Ollama firewall in a Podman network namespace) |
| `tests/integration/test_bootstrap_converge.py` | the unmodified `bootstrap.yml` converges on a Debian 13 userland container for orchestrator + WebUI + execution worker; resulting users, modes, keys, authorized_keys/known_hosts, venv, sub-IDs, delegation, units; second run and `--check` report `changed=0` |
| `tests/integration/test_bootstrap_postgresql.py` | real PostgreSQL cluster with the rendered config: localhost only, no hba/config errors, `database.yml` idempotent, pgvector minimum enforced (local 0.6.0 refused), identifier quoting, `validate_config.yml` catches broken values/syntax/hba incl. restart-only parameters and rolls back |
| `tests/integration/test_bootstrap_nginx.py` | real nginx: `nginx -t` (+ negative control), SSE delivered while the upstream holds the stream (vs. buffered `/api/`), no gzip on SSE, proxy headers/keepalive, SPA fallback + headers, IPv6 listener toggle |
| `tests/integration/test_bootstrap_tooling.py` | Python/Git/Node.js version gates with real and fake binaries, real venv creation |

Run: `.venv/bin/pytest -q tests/integration/test_bootstrap_*.py` (about 15 min, mostly the
converge test; needs Podman, PostgreSQL binaries, systemd-analyze, shellcheck).
`HERMCLAW_ANSIBLE_LINT=/path/to/ansible-lint` enables the lint test.

## Operating on the real hosts

See the runbook [`docs/operations/bootstrap.md`](../operations/bootstrap.md). Live verification
items (BLOCKER-001): PGDG install + pgvector version on `.225`, NodeSource on `.223`, nginx
reload on `.223`, rootless `podman info` with delegation on `.222/.225`, 580 runfile + `nvidia-smi`
+ Ollama GPU discovery on `.224`, `Wake-on: g` after reboot on `.222/.224`, SSH from the
orchestrator with the runtime key.
