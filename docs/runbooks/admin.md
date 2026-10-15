# Admin runbook (P41.4)

Day-to-day operation of Hermclaw Next. Installation: `docs/operations/INSTALLATION.md`; upgrades:
[upgrade.md](upgrade.md); host loss: [disaster-recovery.md](disaster-recovery.md).

Conventions: commands on the orchestrator `.225` run through the operator wrapper `sudo hermclaw …` (the Hermclaw
CLI as user `hermclaw` with the services' environment and credentials). API calls use a personal token in
`$TOKEN` (`sudo hermclaw token-create`, §4) against `http://192.168.178.225:8000` or through nginx on `.223`.
Ansible commands run on the admin machine in `infra/ansible` of the checkout. `journalctl` runs with `sudo`: a login
outside the groups `adm` and `systemd-journal` sees no lines of system units.

```bash
export TOKEN=…   # read/control/admin token from the password manager
api() { curl -fsS -H "Authorization: Bearer $TOKEN" "http://192.168.178.225:8000/api$1" "${@:2}"; }
```

## 1. Daily / weekly checks

| When | Check | Expected |
|---|---|---|
| daily | `sudo hermclaw doctor` | no `fail`; `warn` only for known items (e.g. pgvector < 0.8.7 while D-012 applies) |
| daily | `sudo journalctl -u hermclaw-backup --since -26h -o cat \| grep -E '^  "(set_id\|ok)"'` | one backup: `"set_id": "…"` and no `"ok": false` (the result of a successful backup has no `ok` field); alert if none within 26 h |
| weekly | `sudo journalctl -u hermclaw-restore-test --since -8d -o cat \| grep -E '^  "(set_id\|ok)"'` | restore test `"ok": true`; alert if none within 8 days |
| weekly | `.225`: `df -h /var/lib/hermclaw`; `.224`: `df -h /var/lib/ollama` | > 10 GB free (warning below, no new jobs below 2 GB) |
| weekly | `apt list --upgradable` on every host | security updates applied (`unattended-upgrades`); after nginx/podman/pgvector updates re-run `sudo hermclaw doctor` |
| monthly | on `.225`: `sudo /opt/hermclaw/src/scripts/dev/dependency-audit.sh /opt/hermclaw/venv /opt/litellm/venv` (source checkout of INSTALLATION.md §12; installs pip-audit from PyPI into a temporary venv) | exit 0, no known vulnerabilities (reports in `/opt/hermclaw/src/audit-reports/`) |
| after every driver/kernel update on `.224` | `nvidia-smi`, `ollama ps` while a model is loaded | driver 580.x, `100% GPU` for `qwen3:8b` |

`api /stats` (jobs by status) and `api /metrics` (Prometheus text format: jobs, steps in flight, leases, workers,
free disk, model calls) feed a dashboard.

## 2. Jobs

| Task | UI | API (scope) |
|---|---|---|
| create | "Neuer Job" | `api /jobs -X POST -H 'Content-Type: application/json' -d '{"title":…,"prompt":…,"repository":{"name":"mein-projekt"}}'` (control) – or `sudo hermclaw job-submit "…" --repo mein-projekt` |
| follow | job page (plan DAG, live status, diffs, artifacts) | `api /jobs/<id>`, `api /jobs/<id>/steps`, `api /jobs/<id>/events`, SSE `api /jobs/<id>/events/stream -N` (read) |
| cancel | "Abbrechen" | `api /jobs/<id>/cancel -X POST` (control) |
| pause / resume | "Pausieren" / "Fortsetzen" | `api /jobs/<id>/pause -X POST`, `api /jobs/<id>/resume -X POST` (control) |
| retry a failed job | "Erneut versuchen" | `api /jobs/<id>/retry -X POST` (control) |
| force a new plan | "Neu planen" | `api /jobs/<id>/replan -X POST` (control) – Gemma replans with the failure package |
| approve an SSH change | tab "Freigaben" | pending: `api /jobs/<id>/ssh-approvals` (read); approve: `api /jobs/<id>/ssh-approvals -X POST -H 'Content-Type: application/json' -d '{"token":"…","note":"…"}'` (admin); revoke an unused approval: `api /jobs/<id>/ssh-approvals/<token> -X DELETE` (admin) |

Every control is an event (`job.control.*`, `ssh.approval.*`) with the principal's name: the audit trail is the
event log of the job.

Before approving an SSH mutation, read the exact command and host in the pending entry. An approval covers exactly
that command on that host for that step, once.

## 3. Repositories

```bash
sudo hermclaw repo-add <name> git@192.168.178.226:<group>/<project>.git --provider gitlab --project-id <group>/<project>
sudo hermclaw repo-add <name> <url> --update --default-branch main --protected 'release/*' --sandbox-image node
#   changes URL, default branch, protected branches or image; --default-branch falls back to main when left out
sudo hermclaw repo-list
```

Git over SSH (`git@192.168.178.226:…`) uses the key and known_hosts of the `gitlab` host in `hosts.yaml`
(`gitlab-226`: `/etc/hermclaw/ssh/id_ed25519`, `/etc/hermclaw/ssh/known_hosts`). Hermclaw pushes only `hermclaw/*`
job branches. It opens a merge request only with `git.create_merge_request: true` in `policies.yaml` (default
`false`; inventory: `hermclaw_config_overrides: {policies: {git: {create_merge_request: true}}}`, then re-run
`playbooks/deploy.yml`). Merging is a human decision in GitLab.

## 4. Tokens and access

```bash
sudo hermclaw token-create alice --scopes read,control           # shown once
sudo hermclaw token-revoke alice
```

Give each person their own token; `admin` only to people who may approve host changes. Rotate service credentials
as in [disaster-recovery.md](disaster-recovery.md) §7.

## 5. Workers and models

| Task | Command |
|---|---|
| worker overview | `api /workers` (read) – state `starting`/`ready`/`busy`/`draining`/`sleeping`/`waking`/`offline`/`error`, protocol version, last heartbeat |
| take a worker out of service (maintenance) | `api /workers/exec-222/drain -X POST -H 'Content-Type: application/json' -d '{"drain": true, "reason": "maintenance"}'` (admin) → running steps finish, no new work; the same call with `-d '{"drain": false}'` to return |
| wake a sleeping worker by hand | the scheduler does this before every step anyway. By hand from `.225`: `sudo apt install wakeonlan` once (not installed by the deployment), then `wakeonlan -i 192.168.178.255 <mac>` (MAC: `wol_mac` of the host in the inventory) |
| models on `.224` | `ollama list`, `ollama ps`; pull again: `sudo -u ollama /opt/hermclaw/bin/hermclaw-model-pull` |
| model memory/residency | `api /models` and `api /resources` (leases: only one large model at a time) |
| rebuild sandbox images (`.222`) | set a new tag in `inventory/hosts.yml`, `all.vars`: `hermclaw_sandbox_image_version: "2"` (an existing tag is never rebuilt), then `ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml --limit orchestrator,execution_workers -K` – `.222` builds the images, `policies.yaml` on `.225` and `.222` gets the new tag |

Planner, coder, reviewer and router models are fixed by the architecture. Change tags only to another build of
the same model (e.g. a newer quantisation), in the inventory: `hermclaw_config_overrides: {models: {profiles: […]}}`
(the list replaces the example's `profiles` as a whole). Then re-run
`ansible-playbook -i inventory/hosts.yml playbooks/deploy.yml -K`: it renders `/etc/hermclaw/models.yaml` on `.225`
and pulls the new tag on `.224`.

## 6. Logs and evidence

* Services log JSON to the journal: `sudo journalctl -u hermclaw-scheduler -n 200 -o cat | jq -R 'fromjson? // .'`
  (systemd's own lines are not JSON; no secrets, no model reasoning – only results and short status lines are
  logged).
* Job artifacts (reports, ops outputs, rollback artifacts, media): `/var/lib/hermclaw/artifacts/<job>/…`, also
  `api /jobs/<id>/artifacts` (list with ids) and `api /artifacts/<artifact-id>/download -o <file>`.
* Command audit (SSH, DB, container tools): `command_runs` table and `ssh.command` / `db.tool` /
  `deployment.started|finished` events.

## 7. Incidents

| Situation | First steps |
|---|---|
| jobs stay `queued` | `sudo hermclaw doctor`; disk space (no new jobs below 2 GB); `sudo journalctl -u hermclaw-scheduler -n 200` |
| many steps fail with `MODEL_UNAVAILABLE` | `.224` reachable? `ollama ps`; LiteLLM: `curl -s 127.0.0.1:4000/health/liveliness` on `.225` |
| `MODEL_AUTH_FAILED` | LiteLLM master key differs between `hermclaw-litellm` and the scheduler → `sudo /opt/hermclaw/bin/hermclaw-credential check litellm-master-key`, then `sudo systemctl restart hermclaw-litellm hermclaw-scheduler` |
| `WORKER_UNREACHABLE` | worker asleep and not waking (WoL, MAC), or down → [disaster-recovery.md](disaster-recovery.md) §2/§3 |
| a job loops without progress | stagnation detection stops it (event `stagnation.*`); look at the failure package in the job report, then `replan` or cancel |
| PostgreSQL down | `systemctl status postgresql@17-main`, `sudo journalctl -u postgresql@17-main`; the scheduler recovers in-flight work after the database is back |
| scheduler crashed / host rebooted | nothing to do: on start the scheduler recovers checkpointed coder steps, re-runs in-flight steps and releases stale leases (P34) |
