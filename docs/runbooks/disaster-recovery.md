# Disaster Recovery (P41.5)

What to do when a host or a component of Hermclaw Next is lost. The state that matters lives **only on the
orchestrator `.225`** (PostgreSQL: jobs, plans, events, scope, reviews; `/etc/hermclaw`; artifacts; the job
repositories and workspaces). Every other host can be rebuilt from the repository and the inventory alone.

| Lost | State on it | Recovery | Expected time |
|---|---|---|---|
| `.225` orchestrator | database, config, artifacts, credentials | restore from the newest tested backup on Terra `.60` (§1) | 1–2 h |
| `.222` execution worker | none (sandbox images can be rebuilt; workspaces are copies) | re-bootstrap + deploy (§2) | 30–45 min |
| `.224` model worker | models (re-downloadable, ≈ 70 GB) | re-bootstrap (driver!) + deploy + model pull (§3) | 2–4 h |
| `.223` web UI | none (static build) | re-bootstrap + deploy (§4) | 15 min |
| `.226` GitLab | the projects (outside Hermclaw) | GitLab's own backup; then re-register remotes (§5) | – |
| `.60` Terra | the backup sets | new target, first backup + restore test (§6) | 30 min |
| a credential | – | rotate (§7) | 10 min |

Keep in the password manager (never in the repository): GitLab token, worker tokens, backup manifest key, API admin
token, the backup SSH key (or authorise a new one on `.60`). The encrypted credential blobs in
`/etc/credstore.encrypted` are bound to the host key / TPM of the installation that created them. The LiteLLM master
key and the runtime SSH key are generated again by the bootstrap (§1).

All Ansible commands run on the admin machine in `infra/ansible` (INSTALLATION.md, chapter 4).

## 1. Orchestrator `.225`

Follow `docs/operations/backup-restore.md` → *Disaster recovery* step by step; the step numbers below are its steps.
In short:

1. Reinstall Debian 13 (INSTALLATION.md, chapter 3), keep the IP. Bootstrap:
   `ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit orchestrator -K` (step 1). It generates
   new random credentials and a new runtime SSH key, but never `gitlab-token` or `backup-manifest-key`.
2. Re-create the credentials from the password manager **before** the deployment – its preflight stops without
   `gitlab-token`: `sudo /opt/hermclaw/bin/hermclaw-credential set <name> --force` for `gitlab-token`,
   `backup-manifest-key`, `worker-token-exec-222`, `worker-token-model-224` and, optionally, `api-admin-token`
   (step 2). Keeping the old worker tokens means the workers need no change. `litellm-master-key` keeps the
   bootstrap's value.
3. Deploy the **same** `hermclaw_version` as before:
   `ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml --limit orchestrator -K`. It migrates the new
   database and starts the services. Stop them again:
   `sudo systemctl stop hermclaw-scheduler hermclaw-api hermclaw-backup.timer hermclaw-restore-test.timer` (step 3).
4. Backup key and `backup.yaml` in place, `sudo hermclaw backup list`, then test the newest good set:
   `sudo hermclaw backup restore-test <set-id>` (steps 4–5).
5. Drop and re-create the database that the deployment migrated (`sudo -u postgres dropdb --force hermclaw`, then
   `createdb` and the extension `vector`), and `sudo hermclaw backup restore-full` the set into it (step 6).
6. Put config and artifacts into place (step 7), `sudo hermclaw migrate` (step 8).
7. Authorise the new runtime SSH key: re-run the bootstrap of `.222`, `.223` and `.224`
   (`--limit webui,execution_workers,model_workers`) and replace the GitLab deploy key (step 9).
8. Start the services and the timers, check `http://192.168.178.225:8000/api/health?deep=true` and run
   `sudo hermclaw doctor` (step 10). The scheduler recovers the jobs that were in flight at backup time.
9. New admin API token: the one the deployment stored in step 3 was in the replaced database. Delete
   `/etc/credstore.encrypted/hermclaw-api-operator-token`, re-run the orchestrator deployment, revoke the old host's
   token (step 11).
10. Repositories and workspaces are not in the backup: the Git engine re-clones a registered repository on first use.
    Job branches that were pushed are on GitLab. Work that was not pushed is lost and the affected steps re-run.
11. Fresh backup + restore test (step 12), then `sudo scripts/ops/live-acceptance.sh` in the source checkout
    (INSTALLATION.md, chapter 12).

Worker and UI deployments (sections 2–4) check first that the orchestrator `.225` already runs `hermclaw_version`
(`/api/health`). Only while `.225` itself is down or being rebuilt, add
`-e hermclaw_release_require_orchestrator_first=false` to those `deploy.yml` runs. Once `.225` runs the release again,
leave it out. `bootstrap.yml` does not check it.

## 2. Execution worker `.222`

```bash
# Debian 13 reinstalled (same IP, Wake-on-LAN in the firmware), then:
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit execution_workers -K
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml --limit execution_workers -K
```

The bootstrap copies the worker token from the orchestrator's credential store, the deployment rebuilds the
sandbox images and starts `hermclaw-exec-worker`. If the MAC address changed (new NIC): update `wol_mac` in the
inventory before the run, and afterwards also run `playbooks/deploy.yml --limit orchestrator -K`. `hosts.yaml`
(with `wake_on_lan.mac`) exists only on `.225` and is rendered from the inventory by the orchestrator's deployment,
not by the worker run. Steps that were running on the lost worker fail with `WORKER_UNREACHABLE` and are retried by
the scheduler.

## 3. Model worker `.224`

```bash
# Debian 13 reinstalled, Secure Boot off (or MOK, INSTALLATION.md 5.6), then (two reboots for the driver):
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit model_workers -K -e nvidia_allow_reboot=true
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml --limit model_workers -K    # also pulls the models
nvidia-smi --query-gpu=driver_version,name,compute_cap --format=csv,noheader             # 580.x, GTX 1080, 6.1
```

While `.224` is down, every model call fails with `MODEL_UNAVAILABLE`; jobs wait in their retry backoff and
continue when the worker is back (no fallback to another host – model roles are fixed). A replacement GPU must be
supported by the R580 driver (or, for a newer card, a newer branch: then reconsider D-011).

## 4. Web UI `.223`

```bash
ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --limit webui -K
ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml --limit webui -K
```

The UI is a static build; nothing is lost. TLS: restore the certificate/key you provided (or let the deployment
create a new self-signed one and re-trust it in the browsers).

## 5. GitLab `.226`

GitLab is outside Hermclaw. After GitLab is restored from its own backup: verify that the projects and the deploy
key still exist, refresh the host key if it changed (`ssh-keyscan`, compare the fingerprint, update
`common_known_hosts_extra` and re-run the orchestrator bootstrap), re-issue the access token if needed (§7). Job
branches that were pushed after the GitLab backup are gone: re-run the affected jobs' finalize step or the jobs.

## 6. Terra `.60` (backup target)

Set up the new share (`infra/backup/unraid-share-setup.md`), authorise the backup key again
(`restrict,from="192.168.178.225",command="rrsync …"`), put Terra's new host key into the inventory
(`common_known_hosts_extra`) and re-run the orchestrator bootstrap, which writes it to `/etc/hermclaw/ssh/known_hosts`,
then:

```bash
sudo systemctl start hermclaw-backup.service && sudo systemctl start hermclaw-restore-test.service
sudo journalctl -u hermclaw-backup -u hermclaw-restore-test --since -1h
```

Old sets are lost with the old target. Until the first new backup exists there is **no** restore point – do this
first.

## 7. Rotating a credential

| Credential | Rotate with |
|---|---|
| worker token | `hermclaw-credential generate worker-token-<id> --force` on `.225`, delete `hermclaw-worker-token` on the worker, re-run the worker bootstrap, restart `hermclaw-api hermclaw-scheduler` and the worker service |
| GitLab token | new token in GitLab → `hermclaw-credential set gitlab-token --force` → restart `hermclaw-scheduler` |
| LiteLLM master key | `hermclaw-credential generate litellm-master-key --force` → restart `hermclaw-litellm hermclaw-scheduler` |
| API admin token | `hermclaw-credential generate api-admin-token --force` → restart `hermclaw-api`; API tokens created with `hermclaw token-create` are revoked with `hermclaw token-revoke` |
| backup manifest key | new key into the password manager → `hermclaw-credential set backup-manifest-key --force`; sets signed with the old key verify only with the old key (keep it in the password manager) |
| SSH runtime key | `ssh-keygen` a new `/etc/hermclaw/ssh/id_ed25519` (or delete it and re-run the orchestrator bootstrap), re-run the bootstrap of `.222/.223/.224` (distributes the public key) and update the GitLab deploy key |

Update the password manager entry after every rotation.
