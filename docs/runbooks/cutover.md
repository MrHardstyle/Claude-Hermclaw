# Cutover plan: old Hermclaw → Hermclaw Next (P42)

Principles (Bauplan Phase 42):

- The old **agent system** is frozen read-only and nothing is deleted.
- The new system runs side by side on its own paths and ports until it has been stable.
- Every step has a rollback.

Operator decision **D-013** (DECISIONS.md) narrows this for the web host `.223`. The legacy portal
**Noob2Claw/Kowacore** is **not** part of the cutover. It keeps running unchanged on `192.168.178.223`:

- Apache 2.4 + mod_php on port 80, MariaDB, webroot `/var/www/noobclaw`;
- login/2FA and wiki;
- reached from outside through Nginx Proxy Manager on `.2` as `app.kowacore.com` / `wiki.kowacore.com`.

`.223` is never reinstalled. Hermclaw Next adds its own nginx next to Apache: the **Kontrollraum** on
`http://192.168.178.223:8080/`, or `https://192.168.178.223:8443/` with TLS. Nginx serves the React UI there and
proxies `/api` (incl. SSE) to `.225`. The portal itself gets exactly one new file, the menu entry
`nav/work_ki_kontrollraum.php` (**Arbeit → KI-Kontrollraum**), whose button opens the Kontrollraum
([integrations/kowacore-portal](../../integrations/kowacore-portal/README.md)). There is therefore **no UI switch
on `.223`**: the cutover only moves the *agent work* from the old pipeline to Hermclaw Next.

## 0. Preconditions (go/no-go)

Ansible commands run on the admin machine in `infra/ansible`. `$TOKEN` is a token with the `read` scope
([admin.md](admin.md)).

| Check | Command / evidence | Go when |
|---|---|---|
| RC tests green on the build host | `docs/testing/test-matrix-results.md` (written by `.venv/bin/python scripts/dev/test_matrix.py`) | all suites green |
| Hosts bootstrapped | `ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --check --diff -K` | `changed=0` on every host |
| Port plan | `ansible-playbook -i inventory/hosts.yml playbooks/bootstrap.yml --tags preflight -K` (runs `scripts/bootstrap/check-host.sh --role <group>` on every host; on `.223` the plan holds 8080/8443, never 80/443) | exit 0; no `port:<n>` FAIL (`CONFLICT`: a foreign owner of a Hermclaw port) |
| Portal untouched on `.223` | `ssh admin@192.168.178.223 "sudo ss -Htlnp 'sport = :80'"` and `curl -s -o /dev/null -w '%{http_code}\n' http://192.168.178.223/login.php` | `apache2` owns port 80 (never `nginx`); `200` |
| Kontrollraum on `.223` | `curl -s http://192.168.178.223:8080/healthz` and `curl -s http://192.168.178.223:8080/api/health` (TLS: `curl -sk https://192.168.178.223:8443/api/health`) | `ok`; `"status":"ok"` with the deployed version |
| Deployment healthy | `curl -s 'http://192.168.178.225:8000/api/health?deep=true'` | `"status":"ok"` |
| Models present on `.224` | `sudo hermclaw doctor` on `.225` while `.224` is awake, and `ollama list` on `.224` (the six tags of INSTALLATION.md §7). Missing models: wake `.224` and re-run `playbooks/deploy.yml --limit model_workers -K`. That stops `hermclaw-model-worker` during the download. Never pull in the foreground with the daemon running: the scheduler would suspend `.224` mid-download. | doctor: `models:model-224` ok; all six tags listed |
| Workers ready | `curl -s -H "Authorization: Bearer $TOKEN" http://192.168.178.225:8000/api/workers` | `exec-222` and `model-224` `ready` (or `sleeping`: the smoke job then shows that Wake-on-LAN wakes them) |
| Live acceptance | `sudo scripts/ops/live-acceptance.sh` in `/opt/hermclaw/src` on `.225` and `.222` (INSTALLATION.md §12) | `/var/lib/hermclaw/acceptance/<time>-summary.txt`: every suite `PASS`, or `SKIP` with a reason |
| Backup + restore test | `sudo systemctl start hermclaw-backup.service hermclaw-restore-test.service` (`After=` starts the restore test once the backup has finished), then `sudo journalctl -u hermclaw-backup -u hermclaw-restore-test --since -3h -o cat \| grep -E '^  "(set_id\|ok)"'` (INSTALLATION.md §11) | `systemctl` exits 0; backup: `"set_id": "<id>"` (a successful backup has no `ok` field); restore test: `"ok": true` with the same `set_id`; no `"ok": false` |
| Live smoke job | one real job against a test repository on `.226` (INSTALLATION.md §10.4) | job `succeeded`, branch `hermclaw/<id8>-<slug>` pushed; a merge request only with `git.create_merge_request: true` |

## 1. Freeze the old agent system read-only (42.1, 42.2)

The old agent components keep their data where it is. The portal on `.223` is **not** frozen: users keep logging
in and working there.

```bash
# bridge host – stop the patch watcher (it is the only writer of the old agent system), do NOT remove it
sudo systemctl stop hermclaw-patch-watcher.service
sudo systemctl disable hermclaw-patch-watcher.service       # re-enable = rollback

# .226 GitLab – archive the old project local-ai/hermclaw (Settings → General → Advanced → Archive project).
#   Archived projects are read-only; un-archive is the rollback. Never delete branches or the project.

# .223 – snapshot of the portal for the record and as the backup before the menu entry is added
#   (read-only copy on .60: webroot, MariaDB dump, Apache vhost; the portal keeps running, nothing on .223 changes)
D=$(date +%F)
B=/mnt/user/backups/hermclaw-legacy/$D
ssh root@192.168.178.60 "mkdir -p $B/webui"
sudo tar -C /var/www/noobclaw -cf - . | ssh root@192.168.178.60 "tar -C $B/webui -xf -"
sudo mariadb-dump --single-transaction --routines --events --all-databases \
  | ssh root@192.168.178.60 "umask 077; cat > $B/portal-mariadb.sql"
sudo cat /etc/apache2/sites-available/hermclaw.conf \
  | ssh root@192.168.178.60 "umask 077; cat > $B/portal-apache-vhost.conf"

# .225, .222, .224 – before the reinstall: stop the old agent/worker units and copy their state to .60
D=$(date +%F)                                                    # on another day: the date of the .223 snapshot
B=/mnt/user/backups/hermclaw-legacy/$D/$(hostname)
systemctl list-units --type=service --state=running --no-pager    # identify the old agent/worker units
sudo systemctl disable --now <old units>                          # the units found above, nothing else
ssh root@192.168.178.60 "mkdir -p $B"
sudo tar -C / -cf - opt etc home var/lib | ssh root@192.168.178.60 "tar -C $B -xf -"
```

`sudo` only reads (files that are not world-readable, the whole database); `ssh` runs as your own login with your
key for `.60`. `sudo rsync` would run `ssh` as root, with root's key and `known_hosts`. The `.223` snapshot is a
one-time copy from the cutover day, not a backup of the running portal. `/var/lib` of the old hosts may hold large
model or image caches; leave them out (`--exclude`) only when you know you do not need them.

The portal's multi-worker pages ("Auftragszentrale", "Agenten") lose their old worker VMs once `.225`, `.222` and
`.224` are reinstalled for Hermclaw Next. Run the `.225, .222, .224` block on each of them before INSTALLATION.md
chapter 3. Their job state stays in the portal's MariaDB, read-only for the record.

Nothing is deleted in this phase: no `rm`, no `DROP`, no branch deletion.

## 2. Put Hermclaw Next to work (42.3)

Hermclaw Next has everything of its own:

- the nginx site `/etc/nginx/sites-enabled/hermclaw-next.conf` on ports 8080/8443 of `.223`;
- the webroot `/var/www/hermclaw-next`;
- the units `hermclaw-*`;
- the database `hermclaw` on `.225`;
- the GitLab job branches `hermclaw/<id8>-<slug>`.

Nothing of the portal has to move. `playbooks/deploy.yml` already did the only `.223`-side step: after the health
checks of the Kontrollraum it wrote the portal's menu entry. It does so only when it finds the portal under
`/var/www/noobclaw`, gives the file the owner of `nav/` and never overwrites a page it did not write.

```bash
# .223 – the portal still on Apache, the Kontrollraum on its own port, the button in place
sudo ss -Htlnp '( sport = :80 or sport = :8080 or sport = :8443 )'   # 80: apache2, 8080 (8443): nginx
curl -s http://192.168.178.223:8080/api/health                         # proxied to .225
grep -c 'Managed by Hermclaw' /var/www/noobclaw/nav/work_ki_kontrollraum.php   # 1
curl -s -o /dev/null -w '%{http_code}\n' 'http://192.168.178.223/index.php?seite=work_ki_kontrollraum'  # 302 (login)
```

In the browser:

1. Log in to the portal.
2. Open **Arbeit → KI-Kontrollraum** and press **KI-Kontrollraum öffnen**.
3. In the new tab, log in with a personal Hermclaw token ([admin.md](admin.md)).

Announce the button and the URL. The Kontrollraum is a LAN service. Publishing it through Nginx Proxy Manager
would be a separate decision: a proxy host to `.223:8080` with SSE buffering off and an access list, see
RISK_REGISTER R-036.

## 3. Monitoring during the stabilisation period (42.5)

* **Liveness:**
  * `.225`: `systemctl is-active hermclaw-api hermclaw-scheduler hermclaw-litellm postgresql`.
  * `.222`: `hermclaw-exec-worker`.
  * `.224`: `hermclaw-model-worker ollama`.
  * `.223`: `nginx`, plus `apache2` for the portal.

  systemd restarts the services on failure. `sudo journalctl -u hermclaw-scheduler -f` shows the runtime log as
  JSON lines.
* **Metrics:** Prometheus scrapes `GET /api/metrics` (bearer token with `read` scope):
  * `hermclaw_jobs{status=…}`, `hermclaw_steps_in_flight`;
  * `hermclaw_resource_leases`, `hermclaw_resource_requests_waiting`;
  * `hermclaw_worker_up`, `hermclaw_worker_heartbeat_age_seconds`;
  * `hermclaw_disk_free_mb`, `hermclaw_model_invocations_last_hour{status=…}`.

  Suggested alerts:
  * worker heartbeat age > 120 s while not sleeping;
  * `hermclaw_resource_leases` > 0 for > 2 h with `hermclaw_steps_in_flight` == 0 (stuck lease);
  * `hermclaw_disk_free_mb{level="critical"}`;
  * failed model invocations rising.
* **Health:** `GET /api/health?deep=true` (DB + LiteLLM), and the UI dashboard (workers, resources, live events).
  On `.223`, `ss -Htlnp 'sport = :80'` must keep showing `apache2`.
* **Daily:**
  * the backup result: `sudo journalctl -u hermclaw-backup --since -26h`;
  * weekly, the restore test: `sudo journalctl -u hermclaw-restore-test --since -8d`;
  * `BUGS.md` triage.

## 4. Rollback (42.4)

Rollback is possible at any time in the stabilisation period and loses no data of either system. The portal is never
part of it: it ran unchanged the whole time.

```bash
# .223 – remove the button (only Hermclaw's marked file; the portal is then exactly as before) and stop the Kontrollraum
cd infra/ansible && ansible-playbook playbooks/deploy.yml -e hermclaw_version=<version> --limit webui --tags portal_link \
    -e hermclaw_ui_portal_link=false -K          # or on .223: sudo rm /var/www/noobclaw/nav/work_ki_kontrollraum.php
sudo systemctl disable --now nginx              # on .223; Apache keeps port 80 either way
# bridge host – old writer back
sudo systemctl enable --now hermclaw-patch-watcher.service
# .226 – un-archive local-ai/hermclaw
# .225 – stop the new runtime (state stays in PostgreSQL; running steps are checkpointed)
sudo systemctl stop hermclaw-scheduler hermclaw-api
```

Hermclaw Next data (database, workspaces, artifacts, backups on `.60`) is kept for analysis; its job branches on
GitLab are separate from the old ones.

## 5. Decommission old components only after stable operation (42.6)

Criteria:

- ≥ 14 days without a P0/P1 bug;
- ≥ 20 real jobs succeeded;
- the backup restore test passed twice;
- no rollback.

Then, one component at a time, each with a fresh backup of what is removed:

1. Remove the `hermclaw-patch-watcher.service` unit and its code from the bridge host.
2. Keep the archived GitLab project `local-ai/hermclaw`. Archived projects cost nothing, and the history stays
   available.
3. Remove old Ollama models on `.224` that `/etc/hermclaw/models.yaml` on `.225` no longer references
   (`ollama list`, then `ollama rm <tag>`). Never remove one of the six Hermclaw Next models (INSTALLATION.md §7).
4. The portal on `.223` (`/var/www/noobclaw`, Apache, MariaDB, its users and cron) stays. It is not an old component
   to decommission (D-013). Its pages about the old worker VMs can be hidden in the portal itself (`RECHT:
   __ausgeblendet__` in their `nav/` headers); that is a change to the portal, outside Hermclaw.
