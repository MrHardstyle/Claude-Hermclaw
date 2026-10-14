# Upgrade path (P41.6)

Hermclaw Next is upgraded as one release across all hosts: the orchestrator (`.225`), the workers (`.222`, `.224`)
and the UI (`.223`) share the worker protocol version (`hermclaw.PROTOCOL_VERSION`, checked on every heartbeat –
an incompatible worker is set to `error` and never receives work) and the API contract (`docs/contracts/`).

## Versioning

* Release versions follow PEP 440 (`0.1.0rc1`, `0.1.0`, `0.2.0`) in `pyproject.toml` and `hermclaw/__init__.py`;
  git tags `v<version>`.
* Database schema changes only through Alembic revisions in `migrations/versions/` (upgrade **and** downgrade;
  `tests/integration/test_persistence*.py` check that models and migrations match and that downgrade/upgrade round-trips).
* Config files are additive: new keys get defaults in `hermclaw/core/config.py`; `hermclaw config-check` validates
  `/etc/hermclaw/*.yaml` before a restart.

## Standard upgrade (no downtime of data, short downtime of the runtime)

```bash
# 0. on the admin machine: new release checked out, tests green (scripts/dev/regression-matrix.sh), release built
git -C ~/hermclaw-next fetch --tags && git -C ~/hermclaw-next checkout v<new>
~/hermclaw-next/scripts/deploy/build-release.sh          # -> dist/release-<new>/ (wheelhouse, LiteLLM, UI, SHA256SUMS)

# 1. backup first (.225) – the restore test proves the backup is usable
sudo -u hermclaw /opt/hermclaw/venv/bin/python -m hermclaw.backup create
sudo -u hermclaw /opt/hermclaw/venv/bin/python -m hermclaw.backup restore-test --latest

# 2. dry run: what the deployment would change (venv switch, config, units)
cd ~/hermclaw-next/infra/ansible
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -e hermclaw_version=<new> --check --diff

# 3. roll out with the deployment playbook (idempotent). Per host it installs the release offline into a fresh venv
#    /opt/hermclaw/venv-<new> (hash-checked wheelhouse), renders and validates /etc/hermclaw/*.yaml with the new
#    release (`hermclaw config-check`), renders the units. On .225 it then drains (stops hermclaw-scheduler and
#    hermclaw-api), runs `hermclaw migrate` of the new release once, as hermclaw with the production environment,
#    switches the /opt/hermclaw/venv symlink atomically and restarts the services, then the workers and the UI.
#    It ends with `hermclaw doctor`.
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -e hermclaw_version=<new>

# 4. verify
curl -s http://192.168.178.225:8000/api/health?deep=true      # status ok, version = <new>
sudo hermclaw doctor                                           # on .225: config, DB head, workers, models, sandbox, research
sudo journalctl -u hermclaw-scheduler --since -5min | grep -E '"scheduler started"|recovered'
```

On start the scheduler recovers everything the stop left in flight (P34): checkpointed coder steps resume,
other in-flight steps re-run, preparation of jobs is re-entrant, leases of the previous incarnation are released.
If the new release fails before the switch (preflight, config validation, migration), the previous release keeps
running. A failed migration starts the old services again. A failure on `.225` stops the whole run, so workers and
UI never get ahead of the orchestrator.

## Rollback

Re-run the deployment with the previous version. Its venv, payload and UI build are still on the hosts
(`hermclaw_keep_releases`, default 3 previous releases), so nothing is downloaded or rebuilt. The switch back is the
same atomic symlink rename:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -e hermclaw_version=<old>
```

**Schema rule.** If the newer release added Alembic revisions, the database is ahead of `<old>`. The deployment
detects this before it stops anything: the new release's `hermclaw doctor --offline --json` reports a revision that
`<old>` does not know. The play then stops and nothing is switched. The schema has to go down with the **newer**
release's migrations, because only they contain the `downgrade()` of their own revisions:

```bash
# after a backup (step 1 above): let the deployment downgrade with the still-installed newer release, then switch
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -e hermclaw_version=<old> \
    -e hermclaw_orchestrator_allow_schema_downgrade=true
# the same by hand on .225 (alembic in the services' context: hermclaw, hermclaw.env, credentials)
sudo systemctl stop hermclaw-scheduler hermclaw-api
sudo hermclaw --venv /opt/hermclaw/venv-<new> --alembic downgrade <old-head>      # <old-head>: release.json alembic_head
sudo hermclaw --venv /opt/hermclaw/venv-<new> --alembic current
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -e hermclaw_version=<old>
```

If a migration cannot be downgraded safely (data transformation), the release notes say so and the rollback is a
restore of the pre-upgrade backup (`python -m hermclaw.backup restore-full --set <backup> --target-db-url …`,
see `docs/operations/backup-restore.md`).

## Component-specific notes

| Component | Upgrade rule |
|---|---|
| LiteLLM (`/opt/litellm/venv` → `venv-<version>-<lock>`, `systemd_skeleton_litellm_venv`) | pinned in the `litellm-proxy` extra of `pyproject.toml` (single source: `build-release.sh` resolves and hash-locks it into the release); bump only with the empirical gateway tests (`tests/integration/test_models_litellm_proxy.py`) green against the new version. The deployment installs the new venv next to the old one, regenerates `/etc/hermclaw/litellm.yaml` with `hermclaw litellm-config` and restarts `hermclaw-litellm` |
| Ollama (`.224`) | pinned version; upgrade only after the GPU smoke test (`hermclaw-model-worker selftest`); never above the last driver branch supporting Pascal (R580, research R-017) |
| Models | tags are fixed per role in `config/models.yaml`; a new tag is a config change (`hermclaw_config_overrides.models` in the inventory); the deployment then pulls every missing tag on `.224` with `/opt/hermclaw/bin/hermclaw-model-pull` (`hermclaw_worker_pull_models`, disk precheck); embedding model changes trigger a re-index (embedding_model per chunk) |
| UI (`.223`) | static build from the release (`hermclaw-ui-<version>.tar.gz`), unpacked to `/var/www/hermclaw-next-releases/<version>/`; the webroot `/var/www/hermclaw-next` is a symlink switched atomically (`docs/operations/ui.md`) |
| PostgreSQL | minor updates via Debian packages; major upgrades with `pg_upgradecluster` after a backup |
