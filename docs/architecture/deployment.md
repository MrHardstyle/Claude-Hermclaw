# P32 – Deployment (infra)

Status: implemented and tested locally (`tests/integration/test_deploy_release.py`, `test_deploy_infra.py`,
`test_deploy_converge.py`). The live run on the target hosts is blocked by BLOCKER-001 (192.168.178.x is not
reachable from the build environment). Operator runbook: [`docs/operations/deployment.md`](../operations/deployment.md),
upgrade and rollback: [`docs/runbooks/upgrade.md`](../runbooks/upgrade.md), bootstrap (P01):
[`bootstrap.md`](bootstrap.md).

## Purpose

Put one **release** of Hermclaw Next onto the four bootstrapped Debian 13 hosts (Bauplan steps 32.1–32.10):

* **Reproducible.** The release is built once on the control node from one commit. It has a hash lock and per-file
  checksums, and it installs **offline**. No host resolves dependencies or downloads anything at deploy time.
* **Atomic and reversible.** Every host has versioned directories plus one symlink. A switch is a single
  `rename(2)`. A rollback is the same playbook run with the previous version, whose files are still on the host.
* **Ordered.** Nothing changes before every host's preflight has passed. The orchestrator goes first, and a failure
  there stops the run. The schema migrates exactly once, before the restart. The files the running release reads
  (`/etc/hermclaw/*.yaml`, `litellm.yaml`, the venv symlinks) change only after the drain and the migration.
* **Idempotent.** A second run and a `--check` run report `changed=0`. `--check` is also the dry run of a first
  deployment or an upgrade: nothing is unpacked or installed, nothing on disk changes. The converge test asserts both
  for the real playbook.

Model roles, the scope, Git mutations and the worker protocol are runtime concerns. The deployment only renders the
configuration that carries them (`models.yaml`, `policies.yaml`, `capabilities.yaml`) and never changes their
semantics. The roles stay fixed: Gemma is planner and replanner, Qwen3-Coder 30B is the implementation worker,
Qwen3.8 27B is the heavy reviewer, Qwen3 8B is the fast router, and `embeddinggemma:300m` does the embeddings.

## Release artifact (`scripts/deploy/build-release.sh`)

```
dist/release-<version>/
  release.json                         # version, python {version 3.13, abi cp313, platforms}, alembic_head,
                                       # entry_points, sandbox_toolchains, runtime {payload, lock_sha256},
                                       # litellm {version, requirements, venv_name, payload}, ui {payload, source},
                                       # tree_sitter {package_version, languages, cache_dir}
  hermclaw-runtime-<v>.tar             # wheelhouse/ (hermclaw wheel + every dependency, cp313/manylinux<=2_41),
                                       # requirements.lock (--require-hashes), constraints.txt, sandbox-images/,
                                       # tree-sitter/ (grammars), release.json, SHA256SUMS (plain tar: wheels
                                       # are compressed already)
  hermclaw-litellm-<v>.tar             # LiteLLM wheelhouse + lock, from the `litellm-proxy` extra of pyproject.toml
  hermclaw-ui-<v>.tar.gz               # static UI build (npm ci && npm run build, or --ui-dist), CSP-checked
  config/*.example.yaml                # what the configuration is rendered from (read on the control node)
  scripts/ops/*                        # operator scripts, installed to /opt/hermclaw/bin
  constraints.txt, litellm-constraints.txt   # exact pins, reusable with --constraints
  SHA256SUMS                           # of every file above
```

The build runs these steps:

1. Create a tools venv from the target interpreter (CPython 3.13 is required).
2. Build the hermclaw wheel with `pip wheel --no-deps` and check its data files
   (`share/hermclaw/alembic.ini`, migrations).
3. Download the dependencies for the target platform (`--python-version 3.13 --abi cp313`, manylinux tags up to
   2_41 = Debian 13 glibc). The LiteLLM requirements come from the `litellm-proxy` extra, the single source of the pin.
4. Write a lock with `sha256` hashes for every wheel.
5. Scan for a `litellm_init.pth` implant (the 1.82.7/1.82.8 supply-chain incident).
6. Run a self-test in a clean venv:
   * the offline, hash-checked install, then `pip check`
   * `hermclaw --version`
   * every `console_scripts` entry point is importable
   * the Alembic head
   * `hermclaw config-check` of the shipped examples
   * a dry-run install of the LiteLLM lock
7. Ship the tree-sitter grammars. `tree-sitter-language-pack` >= 1.0 downloads its grammars on first use
   (research 20261014-027 Y6), so the build fetches the languages the runtime parses with
   (`hermclaw.repo_intelligence.symbols`) for the locked package version. It copies only the `.so` files and the
   manifest into `tree-sitter/`, checks that this copy alone parses each language without writing anything, and
   stores the grammars in the `--wheel-cache` for offline builds.
8. Check the UI bundle: the HTML must have no inline `<script>`/`<style>`, no `style=` and no `on*=`, because the
   nginx CSP forbids them.
9. Pack reproducible tars (`--sort=name --owner=0 --mtime=@0`) and write the checksums.
10. Publish by renaming the staging directory.

`--offline-from DIR` (repeatable) builds without network access, from a wheel cache that `--wheel-cache DIR` filled.
The exit codes are 0 (ok), 1 (build or self-test failure), 2 (usage) and 69 (missing tool).

**UI build choice (32.2).** The UI is built on the control node, inside the release, and is not built on `.223`. Four
reasons:

* The artifact is then the same one the tests and the CSP check saw.
* `.223` needs neither npm registry access nor a Node toolchain at deploy time. Its Node from the bootstrap stays
  for local work.
* Every UI version stays on the host, so a rollback is exact.
* The checksum covers the UI the same way it covers the Python payload.

## Layout on the hosts

```
.225 / .222 / .224 (Python runtime hosts; hermclaw_install_dir=/opt/hermclaw)
  /opt/hermclaw/venv -> venv-<v>                       # active release (relative symlink, atomic switch)
  /opt/hermclaw/venv-<v>/                              # root-owned, world-readable, marker .hermclaw-release (JSON)
  /opt/hermclaw/releases/<v>/                          # unpacked runtime payload, marker .hermclaw-unpacked
  /opt/hermclaw/current -> releases/<v>                # payload of the active release (sandbox-images, tree-sitter
                                                       # grammars = TREE_SITTER_LANGUAGE_PACK_CACHE_DIR in hermclaw.env)
  /opt/hermclaw/bin/hermclaw-*                         # operator scripts of the release (scripts/ops)
  /etc/hermclaw/*.yaml                                 # rendered + validated configuration
  /etc/hermclaw/deploy-staging/                        # validation copy (config-check before install)
  /etc/ansible/facts.d/hermclaw.fact                   # {"deployed": true}, read by the bootstrap
.225 additionally
  /opt/litellm/venv -> venv-<litellm>-<lock12>         # LiteLLM, own venv (systemd_skeleton_litellm_venv)
  /etc/hermclaw/litellm.yaml                           # `hermclaw litellm-config` from the validated models.yaml
  /usr/local/sbin/hermclaw                             # operator CLI (systemd-run in the services' context)
.223
  /var/www/hermclaw-next -> ../hermclaw-next-releases/<v>     # webroot (hermclaw_webroot), atomic switch
  /var/www/hermclaw-next-releases/<v>/, <v>.release           # UI builds + completion markers
  /etc/nginx/sites-available/hermclaw-next.conf               # shared with the bootstrap (roles/nginx)
  /usr/local/bin/hermclaw-ssh-gate                            # SSH gate of the release (P29)
```

## Roles and plays (`infra/ansible/playbooks/deploy.yml`)

| Play | Hosts | Roles / tasks | Notes |
|---|---|---|---|
| 1 Preflight (read-only, `tags: always`, `any_errors_fatal`) | all four groups | `hermclaw_release/preflight.yml`, `hermclaw_config/data.yml`, `hermclaw_release/mark_deployed.yml` | release.json present and `sha256sum -c` clean (control node, `run_once`); a run without the orchestrator (`--limit`, `--tags`) requires it to run this release already (`orchestrator_first.yml`, its `/api/health`); bootstrap present; CPython minor = the release's; every `LoadCredentialEncrypted=` of this host's units exists and decrypts; inventory sane (MACs, overrides, host ids); only then, as the last task, the `hermclaw.fact` |
| 2 Orchestrator (`any_errors_fatal`) | `orchestrator` | `systemd_skeleton`, `hermclaw_release`, `hermclaw_config`, `hermclaw_orchestrator` | see the order below |
| 3 Execution worker | `execution_workers` | `systemd_skeleton`, `hermclaw_release`, `hermclaw_config`, `hermclaw_worker` | sandbox images (missing tags only), `/health` |
| 4 Model worker | `model_workers` | as 3 | ffmpeg, `/health`, Ollama pulls |
| 5 Web UI | `webui` | `nginx`, `hermclaw_ui`, `hermclaw_release/scripts.yml` | UI switch, `nginx -t`, reload, checks through nginx |
| 6 Verify | `orchestrator` | `hermclaw_verify` | `hermclaw doctor --json` in the services' context |

`playbooks/site.yml` imports `bootstrap.yml` and then `deploy.yml`. Every role entry carries its name as a tag
and the dynamic includes pass it on (`apply.tags`); the preflight play is tagged `always`. The roles are generic: they contain no host
names, no benchmark special cases and no project data. Host-specific values come from the inventory and
`group_vars/all.yml`.

| Role | Responsibility |
|---|---|
| `hermclaw_release` | `validate.yml`, `preflight.yml`, `credentials.yml`; payload unpack and verify; venv install (`pip --no-index --find-links --only-binary=:all: --require-hashes -r requirements.lock`, `pip check`); entry-point verification; `switch.yml` (symlink, activation order, `current`, prune); `enable.yml` (systemd or offline `WantedBy` links); `scripts.yml` (operator scripts, SSH-gate allowlist) |
| `hermclaw_config` | `data.yml` computes the configuration (pure, no changes). `main.yml` stages it and validates it with the new release's `hermclaw config-check`. `install.yml` installs it with `backup: true`; `deploy.yml` sets `hermclaw_config_install_at_switch: true`, so the host role calls it at its switch |
| `hermclaw_orchestrator` | operator CLI wrapper, LiteLLM venv (`litellm.yml`, not active) and its switch + config (`litellm_activate.yml`), schema decision, drain, migration, live configuration, restart, LiteLLM gate (`health_litellm.yml`), SearXNG, backup timer, API health, admin token |
| `hermclaw_worker` | sandbox images (`.222`), media packages and Ollama models of `models.yaml` (`.224`: `hermclaw-model-pull` for the fixed role set, `pull_tag.yml` for a tag the inventory changed), live configuration right before the switch, worker daemon, `/health` |
| `hermclaw_ui` | UI build directories, webroot migration, symlink switch, prune, `nginx -t` then reload, health checks |
| `hermclaw_verify` | final `hermclaw doctor` with `hermclaw_verify_doctor_tolerate` for checks expected to warn |
| `nginx` (bootstrap role, extended) | one site template for bootstrap and deployment: SSE location, CSP and security headers, optional TLS (provided or self-signed ECDSA P-256 with SAN, renewed 30 days before expiry), HTTP→HTTPS |

## Ordering on the orchestrator

```
hermclaw_release   unpack payload -> venv-<new> (offline, hashes) -> verify entry points      (old release still active)
hermclaw_config    render -> stage (deploy-staging/) -> config-check with venv-<new>           (live files untouched)
hermclaw_orch.     CLI wrapper -> LiteLLM venv-<lock> installed (not active) -> credentials
                   doctor --offline --json with venv-<new> and the staged config: DB revision known? migrate?
                     downgrade?   (read-only; a refusal leaves every live file and service as it was)
                   enable units
                   drain: stop hermclaw-scheduler, hermclaw-api        (only if the release switches or the schema changes)
                   [downgrade with the newer release's migrations] -> hermclaw migrate (venv-<new>, staged config)
                     -> DB check; rescue: start the old services again with their own config, fail (nothing switched)
                   install /etc/hermclaw/*.yaml (backup) -> /opt/litellm/venv + litellm.yaml -> /opt/hermclaw/venv,
                     current; activation order; prune
                   restart LiteLLM (new venv/config/unit) -> LiteLLM /health/liveliness, /v1/models refuses without
                     a key -> SearXNG -> api + scheduler -> backup.timer
                   health: /api/health (ok, version)
                   admin API credential (once): token-create piped into hermclaw-credential set api-operator-token (no_log)
```

Restart decisions read `hermclaw_release_switched`, `hermclaw_config_changed`, the migration and the flags
`systemd_skeleton_units_changed` / `systemd_skeleton_env_changed`. `systemd_skeleton` sets these flags cumulatively,
so a unit or environment file that the bootstrap part of `site.yml` changed still restarts the services in its
deployment part (where `systemd_skeleton` runs again and reports no change).

The scheduler drains gracefully: on SIGTERM it checkpoints running steps and releases its leases
(`TimeoutStopSec=90s`). The new incarnation recovers them (P34). Workers and the UI come after the orchestrator, so
they never run a newer protocol than the orchestrator. Heartbeats carry `PROTOCOL_VERSION`, and an incompatible
worker is set to `error`.

Migration and doctor run through the operator CLI `/usr/local/sbin/hermclaw`. It calls `systemd-run --pipe --wait
--collect --uid=hermclaw` with:

* the scheduler's `EnvironmentFile=`, working directory and `UMask=0027`
* `LoadCredentialEncrypted=` for every credential the scheduler loads (plus the worker tokens)
* `--venv DIR`, which selects a not-yet-active release
* `--config-dir DIR`, which points the command at another configuration directory. The deployment uses it for the
  staging copy. `EnvironmentFile=` wins over `--setenv`, so the wrapper sets it with `env(1)` inside the unit
* `--alembic`, which runs Alembic of that release
* the backup's optional credentials (`backup-manifest-key`) when they exist, for `sudo hermclaw backup …`

So the schema change runs as the same user, with the same environment and the same secrets as the services, and
never as root with ad-hoc variables.

**Schema rule (rollback).** The deployment refuses a release whose migrations do not contain the database's
current revision, before anything stops. With `-e hermclaw_orchestrator_allow_schema_downgrade=true` it downgrades
with the **newer** release's migrations, taken from the venv the active symlink points to. Only those migrations
contain `downgrade()` for their own revisions. The downgrade target is the older release's `alembic_head` from its
`release.json`.

## Idempotency mechanisms

| Mechanism | Where |
|---|---|
| Completion markers: `.hermclaw-unpacked` (payload), `.hermclaw-release` (venv JSON with the lock hash), `<v>.release` (UI), LiteLLM marker. Work is skipped when the marker exists, and an incomplete directory without a marker is removed and redone | `hermclaw_release/main.yml`, `hermclaw_ui/main.yml`, `hermclaw_orchestrator/litellm.yml` |
| `ansible.builtin.file state=link` with a relative `src` (the `relpath` filter): unchanged when the link already points there | `switch.yml`, UI, LiteLLM |
| Pruning keeps the active release and the `hermclaw_keep_releases` (1–99) **most recently active** others. Every switch touches the release it leaves and the one it activates (venv directory, UI marker, LiteLLM venv), so the previous release survives a rollback followed by an upgrade. Only entries named after a release version (`hermclaw_version_pattern`) are pruned | `switch.yml`/`prune.yml`, `hermclaw_ui`, `litellm_activate.yml` |
| Configuration rendered deterministically (`to_nice_yaml(sort_keys=false)`, fixed header), staged with `copy`, validated, installed with `copy` (changed only on a content difference) | `hermclaw_config` |
| Read-only commands report `changed_when: false`. Mutating commands are guarded by `creates`, a marker or an explicit decision (`hermclaw_orchestrator_migrate` from the DB revision, a token only without its credential, models only when missing from `ollama list`, images only when `podman image exists` fails) | all roles |
| Restarts only on a switch, a configuration or unit change, or a migration. Otherwise `state: started` | orchestrator, worker |
| Bootstrap and deployment render identical files: the `hermclaw.fact` local fact makes the bootstrap's Quadlet `[Install]` match the deployed one, both use the same nginx template, and `common/python.yml` accepts the venv symlink | `systemd_skeleton`, `nginx`, `common` |
| `--check`: the config validation is skipped only when the staged files would change (the staging copy does not exist in check mode), and the switch is not simulated over the bootstrap's plain directory | `hermclaw_config/main.yml`, `switch.yml` |
| `--check` as a dry run: `unarchive` and the payload checksum run only for real (their directories do not exist in check mode), the symlink switches use `force: "{{ ansible_check_mode }}"` (the target is not installed), the schema decision and the final doctor need the new venv / operator CLI and are skipped until they exist, and the model check only reports | release, LiteLLM, UI, orchestrator, verify, worker |

The converge test runs `bootstrap.yml`, a `deploy.yml --check` dry run (nothing on disk changes), `deploy.yml`, then
`site.yml` and `deploy.yml --check` (`changed=0`), and the dry run of the upgrade before it. It also asserts that
`migrate` and `token-create` ran exactly once (fake `systemd-run` log).

## Failure behaviour

| Failure | Effect |
|---|---|
| Preflight (checksum, orchestrator not on the release of a worker/UI-only run, missing bootstrap, Python minor, missing or undecryptable credential, bad inventory) | nothing has changed on any host (`any_errors_fatal`). The message names the fix, e.g. `sudo /opt/hermclaw/bin/hermclaw-credential set gitlab-token` |
| Install of `venv-<new>` (hash mismatch, `pip check`, entry point) | old release active and running; the incomplete venv has no marker and is redone on the next run |
| `hermclaw config-check` of the staged config | the live `/etc/hermclaw/*.yaml` are unchanged; the error output is shown |
| Database newer than the release | stop before the drain and before any live file changes, with the exact command for the allowed downgrade |
| Migration fails | rescue: API and scheduler of the old release start again with their own configuration (`/etc/hermclaw`, LiteLLM and the venv are not switched yet); PostgreSQL rolls the failed revision's transaction back |
| LiteLLM not alive, or answering without its master key, after its restart | the run stops before the API and the scheduler start (drained services stay stopped): fix the credential or `litellm.yaml`, re-run |
| Health check after the switch (orchestrator) | the run stops (`any_errors_fatal`): workers and UI stay on the previous release. Recovery: re-run with the previous version (rollback) |
| Worker `/health`, model pull, sandbox build | that host fails; the orchestrator is already on the new release and stays compatible (protocol check) |
| `nginx -t` fails | no reload; the running nginx keeps the previous configuration |

## Security

* **Secrets.** Secrets exist only as systemd credentials (`/etc/credstore.encrypted/hermclaw-<name>`, research
  20261014-029):
  * The operator supplies `gitlab-token`. The deployment never writes it; it only checks that it exists and decrypts.
  * Every task that touches a secret uses `no_log: true`.
  * The admin API token is created once with `token-create`, piped straight into `hermclaw-credential set` (it never
    reaches Ansible output or a file), and is never printed.
* **Release files.** Venvs and payloads are owned by `root` and read-only for the service users (`u=rwX,go=rX`), so
  a compromised service cannot modify its own code. Only `hermclaw.env` and the credentials are private.
* **Supply chain.** Installs are hash-locked and offline (`--require-hashes --only-binary=:all:`), and both the
  release and the payloads carry per-file `SHA256SUMS`. The tree-sitter grammars (native code) are part of the
  checksummed payload, so no host downloads code at run time. The LiteLLM lock is checked for `litellm_init.pth`, and the
  LiteLLM unit sets `LITELLM_LOCAL_MODEL_COST_MAP=True` (no fetch at start) and `DISABLE_ADMIN_UI=True`.
* **Trusted proxy.** `hermclaw.env` sets `HERMCLAW_API_FORWARDED_ALLOW_IPS` to the web UI host's address
  (`systemd_skeleton_api_forwarded_allow_ips`), so the API takes the client address and scheme from nginx's
  `X-Forwarded-For`/`-Proto` and from nobody else (never `*`).
* **nginx.** The CSP is `default-src 'none'` with only `'self'` (plus `blob:` for images and media) allowed per
  type, `frame-ancestors 'none'` and no inline script or style (enforced at build time). The site also sends
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Cross-Origin-Opener-Policy`,
  `Cross-Origin-Resource-Policy` and `Permissions-Policy` in every location. With TLS it adds
  `upgrade-insecure-requests` and an optional HSTS. The access log has no query strings. The SSE location has
  buffering off and long timeouts.
* **Exec worker unit.** The unit drops the directives that imply `NoNewPrivileges` (they break rootless podman's
  newuidmap). Isolation comes from the sandbox (P13) and `ProtectSystem=full`.
* **Chain-of-thought.** No private chain-of-thought is stored or shown; the deployment does not change the runtime's
  storage rules.

## Configuration strategy (32.1)

1. Start from the release's `config/*.example.yaml`, so new keys arrive with the release.
2. Deep-merge `hermclaw_config_overrides` per file.
3. Patch from the inventory:
   * host addresses, worker APIs, WoL MACs and broadcast addresses
   * the web UI health probe `/healthz`
   * the sandbox images as `localhost/hermclaw-sandbox-<toolchain>:<hermclaw_sandbox_image_version>`
4. Merge `hermclaw_config_host_overrides` by host `id`. A new id is appended.
5. Files listed in `hermclaw_config_unmanaged` are never written. They are still staged as they are and validated
   together with the managed ones.

Each host gets only the files it needs (`hermclaw_config_install`). Every managed file starts with a "Managed by
Hermclaw" header. A manual edit is overwritten by the next run and kept as an Ansible `backup` copy.

## Tests

| Test | Proves |
|---|---|
| `test_deploy_release.py` | real release builds (online with npm UI, offline with `--ui-dist`, a third with a new migration): layout, checksums, hash locks, CSP check, offline install in `python:3.13-slim-trixie`, migrations up/down on PostgreSQL, input refusals, broken entry points refused. The canary `test_this_checkout_builds_a_deployable_release` builds the checkout as it is |
| `test_deploy_infra.py` | syntax checks, play structure, idempotency lint of the roles, `ansible-lint --profile=production`, shellcheck, validation refusals, config rendering and refusals, credential check, offline enable, switch + prune on a real directory tree (rollback followed by an upgrade keeps the previous release, dry run), the UI role end to end on a directory tree (placeholder migration, rollback, prune, dry run), the dry run of a new release (nothing unpacked or installed), the order of the orchestrator role (nothing live before the schema decision), the model pull against test doubles of `ollama`/`systemd-run`/`systemctl` (fixed roles, a changed tag, dry run, refusals), operator CLI with a fake `systemd-run` (argv, `--config-dir`, optional credentials, exit codes, refusals), unit tweaks, self-signed certificate and renewal, nginx with TLS in `nginx:1.28` (headers, SSE, redirect) |
| `test_deploy_converge.py` | the real `bootstrap.yml`, `deploy.yml` and `site.yml` in throw-away `python:3.13-slim-trixie` containers (network none, local connection, `hermclaw_manage_services=false`, private PostgreSQL), with these results: the first dry run changes nothing; `venv-<v>` and symlink; migration of the bootstrap's plain venv; config passes `hermclaw config-check`; units render and pass `systemd-analyze verify --root`; UI release dir and symlink; second run and `--check` report `changed=0`; the dry run of an upgrade changes nothing; upgrade with prune (keep N); rollback refused without touching a live file, then allowed with downgrade, then idempotent; model worker converge; no secret in any output |

Services do not run in the containers. The worker `/health`, the API health, `nginx -t`/reload against the live
service, the SearXNG start and the Ollama pulls are covered by the code paths and the individual tests. Their live
execution waits for BLOCKER-001.

## Open items

* Live run on `.222`–`.225` (BLOCKER-001).
* Runtime changes are listed in the P32 result (`shared_changes_needed`). They include:
  * a configurable `forwarded_allow_ips` in the API
  * a fallback build-context path in `hermclaw-build-sandbox-images`
  * explicit tags for `scripts/ops/hermclaw-model-pull` (today the deployment pulls a tag outside its fixed role set
    itself, `pull_tag.yml`)
* `infra/systemd/hermclaw-backup.service.j2` is now the deployed form of `infra/backup/hermclaw-backup.service`
  (`HERMCLAW_BACKUP_CONFIG`, `SuccessExitStatus=75`, `backup-manifest-key` when it exists). The backup runbook should
  stop telling operators to copy `infra/backup/hermclaw-backup.{service,timer}` over the deployed units: those files
  lack the "Managed by Hermclaw" marker, so the next bootstrap or deployment refuses to overwrite them.
