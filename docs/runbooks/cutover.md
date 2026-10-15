# Cutover plan: old Hermclaw → Hermclaw Next (P42)

Principles (Bauplan Phase 42): the old system is **frozen read-only, nothing is deleted**, the new system runs
side by side on its own paths/ports until it has been stable, and every step has a rollback.

## 0. Preconditions (go/no-go)

Ansible commands run on the admin machine in `infra/ansible`. `$TOKEN` is a token with the `read` scope
([admin.md](admin.md)).

| Check | Command / evidence | Go when |
|---|---|---|
| RC tests green on the build host | `docs/testing/test-matrix-results.md` (written by `.venv/bin/python scripts/dev/test_matrix.py`) | all suites green |
| Hosts bootstrapped | `ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --check --diff -K` | `changed=0` on every host |
| Port plan | `ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --tags preflight -K` (runs `scripts/bootstrap/check-host.sh --role <group>` on every host) | exit 0; no `port:<n>` FAIL (`CONFLICT`: a foreign owner of a Hermclaw port) |
| Deployment healthy | `curl -s 'http://192.168.178.225:8000/api/health?deep=true'` | `"status":"ok"` |
| Models present on `.224` | `sudo -u ollama /opt/hermclaw/bin/hermclaw-model-pull` on `.224` (INSTALLATION.md §7: pulls a missing model, then checks each with `ollama show`; `--dry-run` only prints the pull commands and checks nothing), or `sudo hermclaw doctor` on `.225` while `.224` is awake | exit 0, no `MISSING role models`; doctor: `models:model-224` ok |
| Workers ready | `curl -s -H "Authorization: Bearer $TOKEN" http://192.168.178.225:8000/api/workers` | `exec-222` and `model-224` `ready` (or `sleeping`: the smoke job then shows that Wake-on-LAN wakes them) |
| Live acceptance | `sudo scripts/ops/live-acceptance.sh` in `/opt/hermclaw/src` on `.225` and `.222` (INSTALLATION.md §12) | `/var/lib/hermclaw/acceptance/<time>-summary.txt`: every suite `PASS`, or `SKIP` with a reason |
| Backup + restore test | `sudo systemctl start hermclaw-backup.service hermclaw-restore-test.service` (`After=` starts the restore test once the backup has finished), then `sudo journalctl -u hermclaw-backup -u hermclaw-restore-test --since -3h -o cat \| grep -E '^  "(set_id\|ok)"'` (INSTALLATION.md §11) | `systemctl` exits 0; backup: `"set_id": "<id>"` (a successful backup has no `ok` field); restore test: `"ok": true` with the same `set_id`; no `"ok": false` |
| Live smoke job | one real job against a test repository on `.226` (INSTALLATION.md §10.4) | job `succeeded`, branch `hermclaw/<id8>-<slug>` pushed; a merge request only with `git.create_merge_request: true` |

## 1. Freeze the old system read-only (42.1, 42.2)

The old components keep running in read-only mode; their data stays where it is.

```bash
# .223 – old WebUI (noobclaw) stays reachable, but read-only: keep the site, block mutating requests
sudo cp -a /etc/nginx/sites-available/noobclaw /etc/nginx/sites-available/noobclaw.pre-cutover
sudoedit /etc/nginx/sites-available/noobclaw
#   as the first line inside EVERY server { } block of the file (one per listen port, e.g. 80 and 443) add:
#       if ($request_method !~ ^(GET|HEAD)$) { return 405; }
#   valid in server context and independent of the site's location blocks (limit_except is only allowed inside a
#   location and would have to go into each of them)
sudo nginx -t && sudo systemctl reload nginx        # no reload when the test fails
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://<old UI name or address>/   # 405

# bridge host – stop the patch watcher (it is the only writer of the old system), do NOT remove it
sudo systemctl stop hermclaw-patch-watcher.service
sudo systemctl disable hermclaw-patch-watcher.service       # re-enable = rollback

# .226 GitLab – archive the old project local-ai/hermclaw (Settings → General → Advanced → Archive project).
#   Archived projects are read-only; un-archive is the rollback. Never delete branches or the project.

# .223 – snapshot of the old state for the record (read-only copies, kept on .60)
D=$(date +%F)
ssh root@192.168.178.60 "mkdir -p /mnt/user/backups/hermclaw-legacy/$D"
rsync -a --info=stats1 /var/www/noobclaw/ "root@192.168.178.60:/mnt/user/backups/hermclaw-legacy/$D/webui/"
```

Nothing of the old system is deleted in this phase: no `rm`, no `DROP`, no branch deletion.

## 2. Switch traffic to Hermclaw Next (42.3)

Hermclaw Next uses its own webroot (`/var/www/hermclaw-next`), its own units (`hermclaw-*`), its own database
(`hermclaw` on `.225`) and its own GitLab job branches (`hermclaw/<id8>-<slug>`), so the switch is a single nginx
site change on `.223`. The deployment already installed and enabled `/etc/nginx/sites-enabled/hermclaw-next.conf`
(server names `hermclaw_web_server_name`, never `default_server` while another site claims it); the switch removes
the old site from the request path:

```bash
# .223
test -L /etc/nginx/sites-enabled/hermclaw-next.conf   # enabled by playbooks/deploy.yml
sudo rm -f /etc/nginx/sites-enabled/noobclaw          # only the symlink; the site file stays (rollback)
sudo nginx -t && sudo systemctl reload nginx
curl -s http://192.168.178.223/api/health             # proxied to .225; with TLS on: curl -sk https://192.168.178.223/api/health
```

Announce the new URL. Removing the symlink takes the old UI offline; its site file and `/var/www/noobclaw` stay for
the rollback, and the copy on `.60` is the read-only record. To keep the old UI readable, move its site to its own
port or server name with the read-only block from step 1 instead of removing the symlink.

## 3. Monitoring during the stabilisation period (42.5)

* **Liveness**: `systemctl is-active hermclaw-api hermclaw-scheduler hermclaw-litellm postgresql` on `.225`,
  `hermclaw-exec-worker` on `.222`, `hermclaw-model-worker ollama` on `.224`, `nginx` on `.223`
  (systemd restarts on failure; `sudo journalctl -u hermclaw-scheduler -f` for the runtime log, JSON lines).
* **Metrics**: Prometheus scrapes `GET /api/metrics` (bearer token with `read` scope):
  `hermclaw_jobs{status=…}`, `hermclaw_steps_in_flight`, `hermclaw_resource_leases`, `hermclaw_resource_requests_waiting`,
  `hermclaw_worker_up`, `hermclaw_worker_heartbeat_age_seconds`, `hermclaw_disk_free_mb`,
  `hermclaw_model_invocations_last_hour{status=…}`. Suggested alerts: worker heartbeat age > 120 s while not
  sleeping; `hermclaw_resource_leases` > 0 for > 2 h with `hermclaw_steps_in_flight` == 0 (stuck lease);
  `hermclaw_disk_free_mb{level="critical"}`; failed model invocations rising.
* **Health**: `GET /api/health?deep=true` (DB + LiteLLM), UI dashboard (workers, resources, live events).
* **Daily**: backup result (`sudo journalctl -u hermclaw-backup --since -26h`), weekly the restore test
  (`sudo journalctl -u hermclaw-restore-test --since -8d`), `BUGS.md` triage.

## 4. Rollback (42.4)

Rollback is possible at any time in the stabilisation period and loses no data of either system:

```bash
# .223 – UI back to the old system
sudo ln -sf /etc/nginx/sites-available/noobclaw /etc/nginx/sites-enabled/noobclaw
sudo rm -f /etc/nginx/sites-enabled/hermclaw-next.conf   # a later deploy.yml run enables it again
sudo cp -a /etc/nginx/sites-available/noobclaw.pre-cutover /etc/nginx/sites-available/noobclaw   # remove read-only block
sudo nginx -t && sudo systemctl reload nginx
# bridge host – old writer back
sudo systemctl enable --now hermclaw-patch-watcher.service
# .226 – un-archive local-ai/hermclaw
# .225 – stop the new runtime (state stays in PostgreSQL; running steps are checkpointed)
sudo systemctl stop hermclaw-scheduler hermclaw-api
```

Hermclaw Next data (database, workspaces, artifacts, backups on `.60`) is kept for analysis; its job branches on
GitLab are separate from the old ones.

## 5. Decommission old components only after stable operation (42.6)

Criteria: ≥ 14 days without P0/P1 bug, ≥ 20 real jobs succeeded, backup restore test passed twice, no rollback.
Then, one component at a time, each with a fresh backup of what is removed:

1. remove the old nginx site files (`noobclaw`, `noobclaw.pre-cutover`) and `/var/www/noobclaw` (after the copy
   on `.60` is verified);
2. remove the `hermclaw-patch-watcher.service` unit and its code from the bridge host;
3. keep the archived GitLab project `local-ai/hermclaw` (archived projects cost nothing; history stays available);
4. remove old Ollama models on `.224` that `/etc/hermclaw/models.yaml` on `.225` no longer references
   (`ollama list`, then `ollama rm <tag>`), never one of the six Hermclaw Next models (INSTALLATION.md §7).
