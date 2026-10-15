# Cutover plan: old Hermclaw → Hermclaw Next (P42)

Principles (Bauplan Phase 42): the old system is **frozen read-only, nothing is deleted**, the new system runs
side by side on its own paths/ports until it has been stable, and every step has a rollback.

## 0. Preconditions (go/no-go)

| Check | Command / evidence | Go when |
|---|---|---|
| RC tests green on the build host | `docs/operations/FINAL_BUILD_REPORT.md`, `docs/testing/test-matrix-results.md` (`scripts/dev/test_matrix.py`) | all suites green |
| Hosts bootstrapped | `ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --check` shows no changes | idempotent |
| Deployment healthy | `curl -s http://192.168.178.225:8000/api/health?deep=true` | `"status": "ok"` |
| Models present on `.224` | `sudo -u ollama /opt/hermclaw/bin/hermclaw-model-pull --dry-run` (INSTALLATION.md §7) | exit 0, nothing missing |
| Workers ready | `curl -s -H "Authorization: Bearer $TOKEN" http://192.168.178.225:8000/api/workers` | `exec-222`, `model-224` ready |
| Backup + restore test | `sudo systemctl start hermclaw-backup.service hermclaw-restore-test.service`, then `journalctl -u hermclaw-backup -u hermclaw-restore-test -n 40 -o cat` (INSTALLATION.md §11) | both JSON results `"ok": true` |
| Live smoke job | one real job against a test repository on `.226` (see INSTALLATION.md §12) | job succeeded, MR created |
| Port plan | `scripts/bootstrap/check-host.sh` on every host | no foreign owner of a Hermclaw port |

## 1. Freeze the old system read-only (42.1, 42.2)

The old components keep running in read-only mode; their data stays where it is.

```bash
# .223 – old WebUI (noobclaw) stays reachable, but read-only: keep the site, block mutating requests
sudo cp -a /etc/nginx/sites-available/noobclaw /etc/nginx/sites-available/noobclaw.pre-cutover
#   in the noobclaw server block add:   limit_except GET HEAD { deny all; }
sudo nginx -t && sudo systemctl reload nginx

# bridge host – stop the patch watcher (it is the only writer of the old system), do NOT remove it
sudo systemctl stop hermclaw-patch-watcher.service
sudo systemctl disable hermclaw-patch-watcher.service       # re-enable = rollback

# .226 GitLab – archive the old project local-ai/hermclaw (Settings → General → Advanced → Archive project).
#   Archived projects are read-only; un-archive is the rollback. Never delete branches or the project.

# snapshot of the old state for the record (read-only copies, kept on .60)
ssh root@192.168.178.60 'mkdir -p /mnt/user/backups/hermclaw-legacy/$(date +%F)'
rsync -a --info=stats1 /var/www/noobclaw/ root@192.168.178.60:/mnt/user/backups/hermclaw-legacy/$(date +%F)/webui/
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
curl -s http://192.168.178.223/api/health             # proxied to .225
```

Announce the new URL; the old UI stays reachable read-only on its previous server name/port if configured.

## 3. Monitoring during the stabilisation period (42.5)

* **Liveness**: `systemctl is-active hermclaw-api hermclaw-scheduler hermclaw-litellm postgresql` on `.225`,
  `hermclaw-exec-worker` on `.222`, `hermclaw-model-worker ollama` on `.224`, `nginx` on `.223`
  (systemd restarts on failure; `journalctl -u hermclaw-scheduler -f` for the runtime log, JSON lines).
* **Metrics**: Prometheus scrapes `GET /api/metrics` (bearer token with `read` scope):
  `hermclaw_jobs{status=…}`, `hermclaw_steps_in_flight`, `hermclaw_resource_leases`, `hermclaw_resource_requests_waiting`,
  `hermclaw_worker_up`, `hermclaw_worker_heartbeat_age_seconds`, `hermclaw_disk_free_mb`,
  `hermclaw_model_invocations_last_hour{status=…}`. Suggested alerts: worker heartbeat age > 120 s while not
  sleeping; `hermclaw_resource_leases` > 0 for > 2 h with `hermclaw_steps_in_flight` == 0 (stuck lease);
  `hermclaw_disk_free_mb{level="critical"}`; failed model invocations rising.
* **Health**: `GET /api/health?deep=true` (DB + LiteLLM), UI dashboard (workers, resources, live events).
* **Daily**: backup result (`journalctl -u hermclaw-backup --since -26h`), weekly the restore test
  (`journalctl -u hermclaw-restore-test --since -8d`), `BUGS.md` triage.

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

1. remove the old nginx site file and `/var/www/noobclaw` (after the copy on `.60` is verified);
2. remove the `hermclaw-patch-watcher.service` unit and its code from the bridge host;
3. keep the archived GitLab project `local-ai/hermclaw` (archived projects cost nothing; history stays available);
4. remove old Ollama models that are no longer referenced by `config/models.yaml` (`ollama rm <tag>`), never the
   six Hermclaw Next roles.
