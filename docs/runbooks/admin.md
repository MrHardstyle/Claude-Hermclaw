# Admin runbook (P41.4)

Day-to-day operation of Hermclaw Next. Installation: `docs/operations/INSTALLATION.md`; upgrades:
[upgrade.md](upgrade.md); host loss: [disaster-recovery.md](disaster-recovery.md).

Conventions: commands on the orchestrator `.225` run through the operator wrapper `sudo hermclaw …` (the Hermclaw
CLI as user `hermclaw` with the services' environment and credentials). API calls use a personal token in
`$TOKEN` (`hermclaw token-create`) against `http://192.168.178.225:8000` or through nginx on `.223`.

```bash
export TOKEN=…   # read/control/admin token from the password manager
api() { curl -fsS -H "Authorization: Bearer $TOKEN" "http://192.168.178.225:8000/api$1" "${@:2}"; }
```

## 1. Daily / weekly checks

| When | Check | Expected |
|---|---|---|
| daily | `sudo hermclaw doctor` | no `fail`; `warn` only for known items (e.g. pgvector < 0.8.7 while D-012 applies) |
| daily | `journalctl -u hermclaw-backup --since -26h` | one successful backup (exit 0); alert if none within 26 h |
| weekly | `journalctl -u hermclaw-restore-test --since -8d` | restore test `"ok": true`; alert if none within 8 days |
| weekly | `df -h /var/lib/hermclaw /var/lib/ollama` (`.225`, `.224`) | > 10 GB free (warning below, no new jobs below 2 GB) |
| weekly | `apt list --upgradable` on every host | security updates applied (`unattended-upgrades`); after nginx/podman/pgvector updates re-run `doctor` |
| monthly | `scripts/dev/dependency-audit.sh /opt/hermclaw/venv /opt/litellm/venv` from the source checkout | no known vulnerabilities |
| after every driver/kernel update on `.224` | `nvidia-smi`, `ollama ps` while a model is loaded | driver 580.x, `100% GPU` for `qwen3:8b` |

`api /stats` and `api /metrics` (Prometheus text format) give job, step, lease and worker counters for a dashboard.

## 2. Jobs

| Task | UI | API (scope) |
|---|---|---|
| create | "New job" | `api /jobs -X POST -H 'Content-Type: application/json' -d '{"title":…,"prompt":…,"repository":{"name":"mein-projekt"}}'` (control) – or `sudo hermclaw job-submit "…" --repo mein-projekt` |
| follow | job page (plan DAG, live status, diffs, artifacts) | `api /jobs/<id>`, `api /jobs/<id>/steps`, `api /jobs/<id>/events`, SSE `…/events/stream` (read) |
| cancel | "Cancel" | `api /jobs/<id>/cancel -X POST` (control) |
| pause / resume | "Pause" / "Resume" | `api /jobs/<id>/pause -X POST`, `…/resume` (control) |
| retry a failed job | "Retry" | `api /jobs/<id>/retry -X POST` (control) |
| force a new plan | "Replan" | `api /jobs/<id>/replan -X POST` (control) – Gemma replans with the failure package |
| approve an SSH change | "Approvals" tab | `api /jobs/<id>/ssh-approvals -X POST -d '{"token":"…","note":"…"}'` (admin), revoke with `DELETE …/ssh-approvals/<token>` |

Every control is an event (`job.control.*`, `ssh.approval.*`) with the principal's name: the audit trail is the
event log of the job.

Before approving an SSH mutation, read the exact command and host in the approval entry. An approval covers exactly
that command on that host for that step, once.

## 3. Repositories

```bash
sudo hermclaw repo-add <name> git@192.168.178.226:<group>/<project>.git --provider gitlab --project-id <group>/<project>
sudo hermclaw repo-add <name> <url> --update --protected 'release/*'      # change protected branches / image
sudo hermclaw repo-list
```

Hermclaw pushes only `hermclaw/*` job branches and opens merge requests; merging is a human decision in GitLab.

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
| worker overview | `api /workers` (read) – state `ready`/`busy`/`draining`/`sleeping`/`waking`/`offline`/`error`, protocol version, last heartbeat |
| take a worker out of service (maintenance) | `api /workers/exec-222/drain -X POST -H 'Content-Type: application/json' -d '{"drain": true, "reason": "maintenance"}'` (admin) → running steps finish, no new work; `{"drain": false}` to return |
| wake a sleeping worker by hand | from `.225`: `wakeonlan -i 192.168.178.255 <mac>` (the scheduler does this before every step anyway) |
| models on `.224` | `ollama list`, `ollama ps`; pull again: `sudo -u ollama /opt/hermclaw/bin/hermclaw-model-pull` |
| model memory/residency | `api /models` and `api /resources` (leases: only one large model at a time) |
| rebuild sandbox images (`.222`) | bump `hermclaw_sandbox_image_version` in the inventory and re-run `playbooks/deploy.yml --limit execution_workers` |

Planner, coder, reviewer and router models are fixed by the architecture. Change tags only to another build of
the same model (e.g. a newer quantisation), in `models.yaml` through the inventory, and re-run the deployment.

## 6. Logs and evidence

* Services log JSON to the journal: `journalctl -u hermclaw-scheduler -o cat | jq .` (no secrets, no model
  reasoning – only results and short status lines are logged).
* Job artifacts (reports, ops outputs, rollback artifacts, media): `/var/lib/hermclaw/artifacts/<job>/…`, also
  `api /jobs/<id>/artifacts` and `…/artifacts/<id>/download`.
* Command audit (SSH, DB, container tools): `command_runs` table and `ssh.command` / `db.tool` /
  `deployment.started|finished` events.

## 7. Incidents

| Situation | First steps |
|---|---|
| jobs stay `queued` | `sudo hermclaw doctor`; disk space (no new jobs below 2 GB); `journalctl -u hermclaw-scheduler -n 200` |
| many steps fail with `MODEL_UNAVAILABLE` | `.224` reachable? `ollama ps`; LiteLLM: `curl -s 127.0.0.1:4000/health/liveliness` on `.225` |
| `MODEL_AUTH_FAILED` | LiteLLM master key differs between `hermclaw-litellm` and the scheduler → `hermclaw-credential check litellm-master-key`, restart both |
| `WORKER_UNREACHABLE` | worker asleep and not waking (WoL, MAC), or down → [disaster-recovery.md](disaster-recovery.md) §2/§3 |
| a job loops without progress | stagnation detection stops it (event `stagnation.*`); look at the failure package in the job report, then `replan` or cancel |
| PostgreSQL down | `systemctl status postgresql@17-main`, `journalctl -u postgresql@17-main`; the scheduler recovers in-flight work after the database is back |
| scheduler crashed / host rebooted | nothing to do: on start the scheduler recovers checkpointed coder steps, re-runs in-flight steps and releases stale leases (P34) |
