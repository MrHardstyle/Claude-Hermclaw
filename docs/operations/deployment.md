# Runbook – Deployment of a release (P32)

Status: implemented and tested locally (`tests/integration/test_deploy_*.py`). The live run on the target hosts is
blocked by BLOCKER-001 (192.168.178.x is not reachable from the build environment). Design and internals:
[`docs/architecture/deployment.md`](../architecture/deployment.md). Bootstrap first:
[`docs/operations/bootstrap.md`](bootstrap.md).

The deployment puts one **release** of Hermclaw Next on all four Debian 13 hosts. A release is the hermclaw wheel
plus every runtime dependency for CPython 3.13, the LiteLLM proxy, the UI build, the configuration templates and the
operator scripts. It is built once on the control node and verified by checksum:

| Host | What the deployment does (`infra/ansible/playbooks/deploy.yml`) |
|---|---|
| `.225` orchestrator | release venv `/opt/hermclaw/venv-<v>` (offline, hash-checked) → `/etc/hermclaw/*.yaml` rendered + validated in a staging copy → LiteLLM venv installed → schema decision (nothing live has changed yet) → units enabled → drain → `hermclaw migrate` (once, before the restart) → live configuration + `litellm.yaml` → atomic switch of `/opt/litellm/venv` and `/opt/hermclaw/venv` → `hermclaw-litellm` (healthy and authenticated before anything else starts) → SearXNG, `hermclaw-api`, `hermclaw-scheduler`, `hermclaw-backup.timer`, `hermclaw-restore-test.timer` → health → admin API token (once) |
| `.222` execution worker | release venv → `capabilities.yaml`, `policies.yaml` validated → sandbox images (rootless, missing tags only) → configuration installed → switch → `hermclaw-exec-worker` → `/health` |
| `.224` model worker | release venv → `capabilities.yaml` validated → ffmpeg → configuration installed → switch → missing Ollama tags of `models.yaml` pulled with `hermclaw-model-worker` stopped (`hermclaw-model-pull`, then any other tag on its own; no heartbeat, so the scheduler cannot suspend the host mid-download) → `hermclaw-model-worker` → `/health` |
| `.223` web UI (shared with the legacy portal, D-013) | nginx site on 8080/8443 (headers, CSP, portal cookies dropped, optional TLS) → `nginx -t` + listener guard (never 80/443) → UI build into `/var/www/hermclaw-next-releases/<v>/` → atomic switch of `/var/www/hermclaw-next` → `nginx -t` → reload → checks through nginx → the portal's menu entry **KI-Kontrollraum** (only when the portal is found) |
| all | at the end: `hermclaw doctor` on `.225` in the services' context; any failed check stops the play |

## 1. Prerequisites

**Hosts**

* The bootstrap has run on every host (`ansible-playbook playbooks/bootstrap.yml`, or `site.yml`). The deployment
  checks `/opt/hermclaw`, `/etc/hermclaw`, `/opt/hermclaw/bin` and the unit files, and refuses to run otherwise.
* `.225`: PostgreSQL 17 + pgvector are running (role `postgresql`). `.224`: NVIDIA driver and Ollama are running
  (roles `nvidia` and `ollama`). The Ollama models need about 70 GB plus 10 GB headroom on the `OLLAMA_MODELS` file
  system; the deployment checks the free space before it pulls anything.
* Credentials (INSTALLATION.md chapter 6): the bootstrap generates `api-admin-token`, `litellm-master-key` and the
  worker tokens. **You** store the GitLab token on `.225` before the first deployment:

  ```bash
  sudo /opt/hermclaw/bin/hermclaw-credential set gitlab-token      # read from stdin, encrypted, never echoed
  sudo /opt/hermclaw/bin/hermclaw-credential check gitlab-token    # OK gitlab-token
  ```

  The preflight reads every `LoadCredentialEncrypted=` of the units it will enable. It also decrypts each credential
  once and only prints `OK`/`FAIL`, never the value. A missing or undecryptable credential stops the run before any
  host is changed, and the message says how to create it.

**Control node** (the admin machine; Debian 13 recommended)

* ansible-core 2.21 in its venv: `scripts/bootstrap/bootstrap-control-node.sh` (docs/operations/bootstrap.md §1).
* For `scripts/deploy/build-release.sh`: CPython **3.13** with `venv` (`python3`/`python3-venv` on Debian 13, or
  `--python /path/to/python3.13`), Node.js ≥ 22.12 with npm (NodeSource 22.x; ≥ 22.23.2 recommended, research 028),
  `tar`, `gzip`, `sha256sum`. The build needs PyPI and the npm registry. Offline alternatives are `--offline-from`
  (a wheel mirror) and `--ui-dist` (a prebuilt UI).
* The release must target the hosts' Python minor version. Debian 13 ships 3.13, and the preflight compares
  `release.json` with the hosts' `python3`.

**Inventory** (`infra/ansible/inventory/hosts.yml`, from `hosts.example.yml`)

| Variable | Where | Default / example | Meaning |
|---|---|---|---|
| `hermclaw_version` | `all.vars` or `-e` | `"0.1.0rc1"` | canonical PEP 440 version, the name of `dist/release-<version>/` (quote it) |
| `wol_mac` | each worker | **required** | real MAC address of the Wake-on-LAN interface (`ip -br link`). The deployment refuses `00:00:00:00:..` |
| `hermclaw_host_id` | each host | `orchestrator-225`, `webui-223`, `gitlab-226`, `terra-60` | id of the host's entry in `hosts.yaml` (workers: `hermclaw_worker_id`) |
| `hermclaw_web_server_name` | `.223` | `hermclaw.lan 192.168.178.223` | nginx `server_name`, also the SAN of a self-signed certificate |
| `hermclaw_webui_http_port` / `hermclaw_webui_https_port` | `.223` (host var or `-e`; playbook group_vars) | `8080` / `8443` | nginx's ports next to the legacy portal (D-013); port plan, `hosts.yaml` probe, health checks and the portal button follow them |
| `hermclaw_webui_shared_host` | `.223` (host var or `-e`; playbook group_vars) | `true` | the web host also runs the legacy portal: 80/443 and HSTS refused, Debian's nginx default site disabled, no NodeSource, time setup untouched, foreign accounts/directories with Hermclaw's names refused |
| `nginx_firewall_allow_from` | `.223` | shared host: its own IPv4 LAN (`<network>/<prefix>` of the default route, e.g. `[192.168.178.0/24]`); otherwise `[]` | sources for which an **active** ufw gets `allow` rules for nginx's ports; nothing else of ufw is touched. Empty while an active ufw allows neither port: the role stops on the shared host, warns on a dedicated one |
| `hermclaw_ui_portal_link` | `.223` | `true` | write the portal's menu entry `nav/work_ki_kontrollraum.php` (only when `hermclaw_ui_legacy_portal_root`, default `/var/www/noobclaw`, holds `index.php` and `nav/`); `false` removes Hermclaw's file again |
| `nginx_tls_enabled` / `nginx_tls_source` | `.223` | `false` / `self-signed` | HTTPS; `provided` + `nginx_tls_cert_src` / `nginx_tls_key_src` (files on the control node) |
| `nginx_hsts_max_age` | `.223` | `0` | send Strict-Transport-Security only once every client trusts the certificate; refused on the shared `.223` (it would cover the portal's host name) |
| `hermclaw_keep_releases` | `all.vars` | `3` | previous releases kept for a fast rollback (venvs, payloads, UI builds), 1–99: the most recently **active** ones, so the release active before the last switch is always kept |
| `hermclaw_release_root` | `-e` | `<repo>/dist` | where the release directories are on the control node; a playbook group_var (`playbooks/group_vars/all.yml`), so `all.vars` has no effect: pass `-e hermclaw_release_root=<dir>` |
| `hermclaw_config_overrides` | `all.vars` | `{}` | `{<file>: mapping}` deep-merged into `config/<file>.example.yaml` (section 5) |
| `hermclaw_config_host_overrides` | `all.vars` | `{}` | `{<hosts.yaml id>: mapping}` merged into that host entry; unknown ids are appended |
| `hermclaw_config_unmanaged` | `all.vars` | `[]` | files that are created once and never overwritten |
| `hermclaw_sandbox_image_version` | `all.vars` | `"1"` | tag of the sandbox toolchain images; bump it to rebuild them |
| `hermclaw_worker_pull_models` | `.224` | `true` | pull the Ollama tags of `models.yaml` (opt-out) |
| `hermclaw_orchestrator_allow_schema_downgrade` | `-e` | `false` | permits a rollback below the database's schema (section 7) |
| `hermclaw_verify_doctor_tolerate` | `all.vars` | `[]` | doctor checks whose failure is reported but tolerated, e.g. `["models:model-224"]` |
| `hermclaw_ui_validate_certs` | `.223` | `false` | the UI checks from the control node do not verify a self-signed certificate |
| `hermclaw_release_require_orchestrator_first` | `-e` | `true` | a run without the orchestrator requires it to run `hermclaw_version` already (section 3) |
| `systemd_skeleton_api_forwarded_allow_ips` | `.225` | the `ansible_host` of the `webui` hosts | proxies whose `X-Forwarded-For/-Proto` the API trusts (`HERMCLAW_API_FORWARDED_ALLOW_IPS`); IP addresses only, never `*` |

## 2. Build a release

On the control node, from the checkout of the version to deploy:

```bash
scripts/deploy/build-release.sh                       # -> dist/release-<version>/, version from pyproject.toml
scripts/deploy/build-release.sh --source /tmp/v0.2.0  # build another checkout (git worktree add /tmp/v0.2.0 v0.2.0)
scripts/deploy/build-release.sh --wheel-cache ~/hermclaw-wheels            # keep every wheel for later offline builds
scripts/deploy/build-release.sh --offline-from ~/hermclaw-wheels --ui-dist ui/dist   # air-gapped rebuild
scripts/deploy/build-release.sh --constraints dist/release-0.1.0rc1/constraints.txt  # the exact pins of a tested release
```

What it does and refuses:

* builds the wheel from a clean copy of the sources. It checks that `share/hermclaw/{alembic.ini,migrations}` are
  inside;
* resolves the runtime and the LiteLLM wheelhouses for CPython 3.13 / cp313 / manylinux x86_64 ≤ glibc 2.41,
  binary wheels only. The LiteLLM requirement is the `litellm-proxy` extra of `pyproject.toml` (single source of
  truth, recorded in `release.json`). It writes `requirements.lock` with the sha256 of every wheel
  (`pip --require-hashes`) and `constraints.txt`;
* self-test on the target interpreter: an offline install into a fresh venv, `pip check`, `hermclaw --version`,
  **every console entry point loads** (the units start them), the Alembic head is read, and `hermclaw config-check`
  validates the shipped examples. The LiteLLM lock must resolve offline, and its wheels must not contain the
  `litellm_init.pth` implant (GHSA-5mg7-485q-xm76);
* ships the tree-sitter grammars the structure index parses with (JavaScript, TypeScript, TSX, PHP) for the
  locked `tree-sitter-language-pack`, which would otherwise download them on first use (research 027 Y6). They are
  fetched online or taken from `--offline-from` (a `--wheel-cache` build stores them there). On the hosts,
  `TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=/opt/hermclaw/current/tree-sitter` (`hermclaw.env`) points at them;
* builds the UI (`npm ci && npm run build` from `ui/package-lock.json`). It refuses an `index.html` with inline
  script or style, which the CSP would block;
* writes `release.json`, the payloads `hermclaw-runtime-<v>.tar`, `hermclaw-litellm-<v>.tar` and
  `hermclaw-ui-<v>.tar.gz` (deterministic archives), `config/*.example.yaml`, `scripts/ops/*` and `SHA256SUMS`.
  The directory is published only after everything has succeeded. An existing release is replaced only with
  `--force`.

Exit codes: 0 ok, 1 build/self-test failure, 2 usage, 69 missing tool.

## 3. First deployment

```bash
cd infra/ansible                                                         # ansible.cfg: inventory/hosts.yml
ansible-playbook playbooks/deploy.yml --check --diff                     # dry run: nothing on disk changes
ansible-playbook playbooks/deploy.yml                                    # or: playbooks/site.yml (bootstrap + deploy)
ansible-playbook playbooks/deploy.yml --limit orchestrator               # one host group (its preflight runs too)
ansible-playbook playbooks/deploy.yml --tags hermclaw_ui                 # one role (here: only the UI build + switch)
```

Add `-K` if your admin login needs a sudo password. The dry run checks the release, the inventory and the
credentials (the preflight runs for real) and lists what would change. It does not unpack or install the new
release, so the parts that need it are left to the real run: `hermclaw config-check` of the new configuration, the
schema decision and the final `hermclaw doctor` (before the first deployment there is nothing to check yet).

Every role carries its name as a tag, and the preflight always runs. `--tags hermclaw_ui` (UI only) and
`--tags hermclaw_verify` (doctor only) work on their own. Workers and the UI never get ahead of the orchestrator: a
run that does not deploy the orchestrator (`--limit execution_workers`, `--tags hermclaw_ui`, ...) first asks the
orchestrator's `/api/health` (from the control node) and refuses unless it already runs `hermclaw_version`. When the
orchestrator is down on purpose (disaster recovery of a host with the release it ran before), add
`-e hermclaw_release_require_orchestrator_first=false`. The Python roles build on each other
(`hermclaw_release` → `hermclaw_config` → `hermclaw_orchestrator`/`hermclaw_worker`; `hermclaw_config` validates,
the host role installs the files at its switch), so run those together or run the whole playbook.

The first run on a host replaces the bootstrap's empty venv directory. It is kept as
`/opt/hermclaw/venv.pre-release-<time>` (no data loss; delete it once the deployment works), and
`/opt/hermclaw/venv` becomes the symlink to `venv-<version>`. On `.223` the placeholder webroot is replaced by the
symlink. A webroot with other content is kept as `hermclaw-next.pre-release-<time>`.

Expect: the preflight passes on all hosts, `.225` migrates the empty database to the release head, creates the admin
API token, and every health check is green. The final `hermclaw doctor` reports no failed check. Then:

```bash
ansible-playbook playbooks/deploy.yml               # second run: changed=0 on every host
ansible-playbook playbooks/deploy.yml --check --diff   # changed=0
```

The admin API token (credential `api-operator-token`, DB token `operator-admin-<time>`, scope admin) is never shown.
Read it for the first UI login:

```bash
sudo systemd-creds decrypt --name=api-operator-token /etc/credstore.encrypted/hermclaw-api-operator-token -
```

Personal tokens: `sudo hermclaw token-create alice --scopes read,control` (printed once).

## 4. The operator CLI on `.225`

`/usr/local/sbin/hermclaw` runs `/opt/hermclaw/venv/bin/hermclaw` exactly like the services do: as `hermclaw`, with
`EnvironmentFile=/etc/hermclaw/hermclaw.env` and the encrypted credentials of `hermclaw-scheduler.service`, in a
transient unit (`systemd-run --pipe --wait --collect`). Secrets exist only in that unit's `$CREDENTIALS_DIRECTORY`.

```bash
sudo hermclaw doctor                         sudo hermclaw repo-add myapp git@192.168.178.226:group/myapp.git --project-id 12
sudo hermclaw token-create alice --scopes read,control      sudo hermclaw job-submit "…" --repo myapp
sudo hermclaw --alembic current              sudo hermclaw --venv /opt/hermclaw/venv-0.2.0 migrate
sudo hermclaw backup create                  sudo hermclaw backup restore-test --latest
sudo hermclaw --config-dir /etc/hermclaw/deploy-staging config-check     # the configuration the last run validated
```

It must run as root (exit 77 otherwise). A credential that does not exist yet is reported and left out, and only
commands that need it fail. The backup's credential `backup-manifest-key` is added when it exists. `--config-dir`
must be an existing absolute directory (exit 64 otherwise). The exit status is the command's own.

## 5. Configuration (`/etc/hermclaw/*.yaml`)

**The deployment owns these files.** `hosts`, `models`, `policies`, `capabilities`, `logging` and `databases` are
generated from `config/<name>.example.yaml` **of the deployed release**, then merged:

1. `hermclaw_config_overrides.<name>` is deep-merged: mappings merge key by key, lists replace the example's list.
2. `policies.sandbox.image/images` point to the toolchain images built on `.222`
   (`localhost/hermclaw-sandbox-<toolchain>:<hermclaw_sandbox_image_version>`).
3. `hosts.yaml` gets the values only the inventory knows: `address` = `ansible_host`; for workers `worker_api` and
   `wake_on_lan.mac`/`broadcast`; the nginx probe becomes `/healthz`. Then `hermclaw_config_host_overrides` is merged
   by id.

The complete set is first written to `/etc/hermclaw/deploy-staging/` and validated with the **new release's**
`hermclaw config-check`. The live files are replaced only at the host's switch (`root:hermclaw 0640`, the previous
file kept as `<name>.yaml.<timestamp>~`): on `.225` after the schema decision, the drain and the migration, on the
workers right before the switch. The running release therefore never reads the new release's configuration, and a
change restarts the services that read it. `.225` gets all six files and `litellm.yaml` (`hermclaw litellm-config`
from the validated `models.yaml`). `.222` gets `capabilities.yaml` and `policies.yaml`, and `.224` gets
`capabilities.yaml`.

Manual edits on a host are overwritten by the next run, and `--check --diff` shows them as drift. Put the change into
the inventory:

```yaml
hermclaw_config_overrides:
  policies: {coder: {max_turns: 24}, git: {create_merge_request: true}}
  models: {litellm: {request_timeout_seconds: 900}}
hermclaw_config_host_overrides:
  model-224: {wake_on_lan: {service_timeout_seconds: 300}}
hermclaw_config_unmanaged: [databases]       # created once, then yours (validated as it is)
```

The model roles are fixed by the architecture: Gemma planner, Qwen3-Coder 30B coder, Qwen3.8 27B heavy reviewer,
Qwen3 8B fast router, EmbeddingGemma. `hermclaw doctor` fails when a role has no enabled profile. Change tags, not
roles.

## 6. Upgrade

See [`docs/runbooks/upgrade.md`](../runbooks/upgrade.md). In short: build the new release, back up, then:

```bash
ansible-playbook playbooks/deploy.yml -e hermclaw_version=<new> --check --diff
ansible-playbook playbooks/deploy.yml -e hermclaw_version=<new>
```

The new venv is installed next to the active one, so the old release keeps running until the switch. On `.225` the
scheduler drains on SIGTERM (`TimeoutStopSec=90s`, checkpoints running steps) together with the API. `hermclaw
migrate` of the new release runs once, then the configuration is installed, the symlinks are switched and the
services restart: LiteLLM first, and the API and the scheduler only once it answers `/health/liveliness` and refuses
requests without its master key. A failed migration starts the old services again with their own configuration
(PostgreSQL rolls the revision back). A failure on `.225` stops the whole run.

## 7. Rollback

```bash
ansible-playbook playbooks/deploy.yml -e hermclaw_version=<old>
```

The old venv, payload and UI build are still there (`hermclaw_keep_releases`: the release active before the last
switch is always kept). Nothing is reinstalled; the symlinks switch back. `dist/release-<old>/` must still exist on
the control node. **Schema rule:** if `<new>` added Alembic revisions, the deployment stops before anything is
stopped or switched and before any live file changes, and explains the options. After a backup, re-run with
`-e hermclaw_orchestrator_allow_schema_downgrade=true`. The schema is then downgraded with the migrations of the
active (newer) release, which contain the `downgrade()` of their revisions, to the head of `<old>`, and the old
release is switched in. The alternative is the restore of the pre-upgrade backup
(`docs/operations/backup-restore.md`).

## 8. Per host

**.225**

```bash
systemctl status hermclaw-litellm hermclaw-api hermclaw-scheduler hermclaw-backup.timer hermclaw-restore-test.timer
systemctl --user --machine=hermclaw@.host status hermclaw-searxng.service
readlink /opt/hermclaw/venv /opt/hermclaw/current /opt/litellm/venv
curl -s http://192.168.178.225:8000/api/health
curl -s http://127.0.0.1:4000/health/liveliness                       # "I'm alive!"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4000/v1/models   # 401 (200 would mean: no master key!)
curl -s http://127.0.0.1:8888/healthz                                  # OK
sudo hermclaw doctor
```

LiteLLM lives in `/opt/litellm/venv-<litellm version>-<lock sha>`; `/opt/litellm/venv` is the unit's path (symlink).
The venv is shared by every hermclaw release with the same LiteLLM lock. The backup timer (daily 02:30) and the
restore-test timer (Sunday 04:30) are enabled. Until `/etc/hermclaw/backup.yaml` exists (P33,
`docs/operations/backup-restore.md`; reference: `/opt/hermclaw/current/backup/backup.example.yaml`), each run is
skipped (`ConditionPathExists`). The deployed `hermclaw-backup.service` and `hermclaw-restore-test.service` are the
complete units of `infra/backup/` (environment `HERMCLAW_BACKUP_CONFIG`, exit code 75 = locked counts as success); they
load `backup-manifest-key` when that credential exists. After creating it (`… | sudo systemd-creds encrypt
--name=backup-manifest-key - /etc/credstore.encrypted/hermclaw-backup-manifest-key`, backup runbook) re-run
`ansible-playbook playbooks/deploy.yml --limit orchestrator`, so the units load it. The role `postgresql` prepares the
restore environment (`CREATEDB` for `hermclaw`, template database `hermclaw_restore_template` with `vector`). Do not
copy the units of `infra/backup/` over the deployed units.

**.222**

```bash
systemctl status hermclaw-exec-worker; curl -s http://192.168.178.222:8787/health
sudo -u hermclaw XDG_RUNTIME_DIR=/run/user/$(id -u hermclaw) podman images 'localhost/hermclaw-sandbox-*'
# rebuild: bump hermclaw_sandbox_image_version, or by hand:
sudo -u hermclaw XDG_RUNTIME_DIR=/run/user/$(id -u hermclaw) \
  HERMCLAW_SANDBOX_IMAGES_DIR=/opt/hermclaw/current/sandbox-images /opt/hermclaw/bin/hermclaw-build-sandbox-images \
  --only python --version 1                       # --version = hermclaw_sandbox_image_version (policies.yaml tags)
grep NoNewPrivs /proc/$(systemctl show -p MainPID --value hermclaw-exec-worker)/status   # must be 0 (rootless podman)
```

**.224**

```bash
systemctl status hermclaw-model-worker ollama; curl -s http://192.168.178.224:8787/health
OLLAMA_HOST=127.0.0.1:11434 /usr/local/bin/ollama list
journalctl -u hermclaw-model-pull.service       # the transient unit of the last model pull
```

The pull runs in the transient unit `hermclaw-model-pull.service`, and Ansible polls it, so a dropped SSH session
does not abort a multi-hour download (`hermclaw_worker_model_pull_timeout`, default 6 h). It only runs when a tag of
`models.yaml` is missing. `hermclaw-model-pull` covers the fixed role set; a tag the inventory changed
(`hermclaw_config_overrides.models`) is then pulled on its own in `hermclaw-model-pull-tag.service`
(`journalctl -u hermclaw-model-pull-tag`). The dry run only reports missing tags. The Ollama server settings come from the bootstrap role `ollama` (`ollama.service.d/hermclaw.conf`, research
026) and are not touched by the deployment.

**.223**

```bash
readlink /var/www/hermclaw-next; ls /var/www/hermclaw-next-releases
sudo nginx -t && curl -s http://192.168.178.223:8080/healthz
curl -sI http://192.168.178.223:8080/ | grep -i content-security-policy        # TLS: curl -skI https://192.168.178.223:8443/
sudo ss -Htlnp 'sport = :80'                                                   # apache2 (the portal), never nginx
grep -c 'Managed by Hermclaw' /var/www/noobclaw/nav/work_ki_kontrollraum.php   # 1: the portal's menu entry
```

## 9. Health checks (32.9)

After its restarts each host role checks:

* `.225`: LiteLLM `/health/liveliness` and `/v1/models` refusing requests without a key (before the API and the
  scheduler start), SearXNG `/healthz`, `/api/health` shows `ok` and the new version;
* the workers: `/health` shows `ok` and the new version;
* `.223`, from the control node: `/healthz`, `/` (UI build with CSP) and `/api/health` through nginx on its port
  (`http://192.168.178.223:8080`, with TLS `https://…:8443`). An active ufw without a rule for these ports makes
  them time out (`nginx_firewall_allow_from`). On the shared host the nginx role refuses that state before these
  checks. A rule for every port from `.225` alone would let them pass only when `.225` is the control node. The
  nginx role has also already checked the live sockets there: nginx holds its ports and none of 80/443.

The last play runs `hermclaw doctor` in the services' context, covering configuration, database head and pgvector,
data paths, admin token, LiteLLM aliases, worker registration and heartbeats, the models of `.224`, the sandbox
images of `.222`, and SearXNG. Any failed check stops the play and prints the report, unless the check is listed in
`hermclaw_verify_doctor_tolerate`.

## 10. Troubleshooting

| Symptom | Cause / action |
|---|---|
| `release.json does not exist` | Build the release (section 2) or pass `-e hermclaw_release_root=<dir>` |
| `sha256sum: WARNING: … did NOT match` | The release directory was modified or copied incompletely: rebuild it |
| `hermclaw_version '0.1.0-rc1' is not a canonical PEP 440 version` | Use the name the build printed (`0.1.0rc1`), quoted |
| `runs Python 3.12, the release was built for CPython 3.13` | Build with the hosts' interpreter (`--python`) |
| `credential(s) missing - nothing has been deployed` | Create the named credentials as the message says (`sudo /opt/hermclaw/bin/hermclaw-credential set/generate`) |
| `Wake-on-LAN is enabled for … placeholder MAC` | Put the real `wol_mac` into the inventory |
| `hermclaw config-check rejects the configuration` | An override does not match the schema (`hermclaw/core/config.py`); live files are unchanged |
| `console entry point … cannot be loaded` | The release is broken (the build's self-test should have caught it): fix `pyproject.toml` `[project.scripts]` and rebuild |
| `The database is at schema revision …, which release … does not know` | Rollback below the schema: section 7 |
| migration failed, old services started again | Fix the cause (`journalctl`, `sudo hermclaw --alembic current`), re-run |
| `LiteLLM serves /v1/models without a key` | `sudo /opt/hermclaw/bin/hermclaw-credential check litellm-master-key`; the unit must load it. The API and the scheduler stay stopped until a re-run passes this check |
| `/etc/systemd/system/hermclaw-backup.service exists and is not managed by Hermclaw` (or `hermclaw-restore-test.*`) | the unit was copied from `infra/backup/` by hand. The deployed units are the same (environment, `SuccessExitStatus=75`, `backup-manifest-key` when it exists): `sudo rm /etc/systemd/system/hermclaw-{backup,restore-test}.{service,timer}`, re-run |
| `models.yaml names invalid Ollama tags` | a tag in `hermclaw_config_overrides.models` is not a plain `name[:tag]` |
| `Workers and the UI must never run a newer release than the orchestrator` | deploy the orchestrator first (no `--limit`/`--tags`, or `--limit orchestrator`); disaster recovery with the orchestrator down: `-e hermclaw_release_require_orchestrator_first=false` |
| `invalid … systemd_skeleton_api_forwarded_allow_ips` | `ansible_host` of a webui host is a DNS name: set `systemd_skeleton_api_forwarded_allow_ips` to its IP address in the inventory |
| worker `/health` never `ok` | `journalctl -u hermclaw-exec-worker` / `-model-worker`; worker token (`sudo /opt/hermclaw/bin/hermclaw-credential check worker-token`), podman (`.222`), Ollama (`.224`) |
| `need about … GB … are free` | Free disk space on the models file system or move `OLLAMA_MODELS` (`ollama_models_dir`) |
| UI checks fail with `Debian default page` | The address is not in `hermclaw_web_server_name` |
| doctor fails `models:model-224` with `hermclaw_worker_pull_models: false` | Pull the models (`/opt/hermclaw/bin/hermclaw-model-pull`) or tolerate the check |

## 11. Testing without the hosts

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/integration/test_deploy_release.py    # build script + release
.venv/bin/python -m pytest -q -p no:cacheprovider tests/integration/test_deploy_infra.py      # roles, CLI, TLS, nginx
.venv/bin/python -m pytest -q -p no:cacheprovider tests/integration/test_deploy_converge.py   # site.yml/deploy.yml in trixie containers
```

The converge runs the real playbooks with the real release in `python:3.13-slim-trixie` containers. Services are not
started (no systemd PID 1). PostgreSQL is a private cluster on the test host, and `systemd-run`/`systemd-creds` are
test doubles. It checks: the dry run of the first deployment and of an upgrade (nothing on disk changes), the switch,
the migration of the plain venv directory, `changed=0` on the second run and in `--check`, pruning, a schema
rollback that is refused without touching a live file and then done, `systemd-analyze verify` of the deployed units,
and that no secret reaches any output.
