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
# 0. on the admin machine: new release checked out, tests green (scripts/dev/regression-matrix.sh)
git -C ~/hermclaw-next fetch --tags && git -C ~/hermclaw-next checkout v<new>

# 1. backup first (.225) – the restore test proves the backup is usable
sudo -u hermclaw /opt/hermclaw/venv/bin/python -m hermclaw.backup create
sudo -u hermclaw /opt/hermclaw/venv/bin/python -m hermclaw.backup restore-test --latest

# 2. drain: no new jobs, running steps checkpoint (scheduler SIGTERM checkpoints coder loops)
sudo systemctl stop hermclaw-scheduler

# 3. roll out with the deployment playbook (idempotent; installs into a fresh venv /opt/hermclaw/venv-<version>,
#    switches the /opt/hermclaw/venv symlink, runs `hermclaw migrate`, renders units/config, restarts services)
cd ~/hermclaw-next/infra/ansible
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -e hermclaw_version=<new>

# 4. verify
curl -s http://192.168.178.225:8000/api/health?deep=true      # status ok, version = <new>
curl -s -H "Authorization: Bearer $TOKEN" http://192.168.178.225:8000/api/workers   # protocol compatible, ready
sudo journalctl -u hermclaw-scheduler --since -5min | grep -E '"scheduler started"|recovered'
```

On start the scheduler recovers everything the stop left in flight (P34): checkpointed coder steps resume,
other in-flight steps re-run, preparation of jobs is re-entrant, leases of the previous incarnation are released.

## Rollback

```bash
sudo systemctl stop hermclaw-scheduler hermclaw-api
# schema back to the previous release (only if the new release added revisions)
sudo -u hermclaw /opt/hermclaw/venv-<old>/bin/alembic -c /opt/hermclaw/venv-<old>/share/hermclaw/alembic.ini downgrade <old-head>
sudo ln -sfn /opt/hermclaw/venv-<old> /opt/hermclaw/venv
sudo systemctl start hermclaw-api hermclaw-scheduler
# workers and UI: re-run the playbook with -e hermclaw_version=<old>
```

If a migration cannot be downgraded safely (data transformation), the release notes say so and the rollback is a
restore of the pre-upgrade backup (`python -m hermclaw.backup restore-full --set <backup> --target-db-url …`,
see `docs/operations/backup-restore.md`).

## Component-specific notes

| Component | Upgrade rule |
|---|---|
| LiteLLM (`/opt/litellm/venv`, `systemd_skeleton_litellm_venv`) | pinned (`litellm[proxy]==1.104.2`); bump only with the empirical gateway tests (`tests/integration/test_models_litellm_proxy.py`) green against the new version; regenerate config with `hermclaw litellm-config` |
| Ollama (`.224`) | pinned version; upgrade only after the GPU smoke test (`hermclaw-model-worker selftest`); never above the last driver branch supporting Pascal (R580, research R-017) |
| Models | tags are fixed per role in `config/models.yaml`; a new tag is a config change + `scripts/ops/hermclaw-model-pull`; embedding model changes trigger a re-index (embedding_model per chunk) |
| UI (`.223`) | static build; deploy playbook copies the new `ui/dist` into `/var/www/hermclaw-next` atomically (directory swap) |
| PostgreSQL | minor updates via Debian packages; major upgrades with `pg_upgradecluster` after a backup |
