# Soak test (P40) – local run 2026-10-13

Harness: `scripts/soak/run_soak.py` (+ `scripts/soak/soak_scheduler.py`). Raw report:
[`soak-local-20261013.json`](soak-local-20261013.json). Ran from a frozen git worktree of commit `a343b03` so that
concurrent development could not change the code under test.

```bash
.venv/bin/python -m scripts.soak.run_soak --minutes 60 --jobs-per-minute 2 --sample-seconds 60 --drain-minutes 15 \
    --out docs/operations/soak-local-<date>.json
```

## Setup
Real processes: `hermclaw api` (uvicorn), the scheduler (`SchedulerProcess` with the full runtime), a real execution
worker daemon (`python -m worker.execution`) heartbeating to the API every 5 s; fresh PostgreSQL database with all
migrations. Jobs (two types: implement → test → review on a module constant, and a FastAPI endpoint with a real
pytest acceptance) were submitted every 30 s **through the HTTP API** against local `file://` repositories. Model
answers are scripted (the models of `.224` are not reachable from the build host, BLOCKER-001); everything else –
git, scope, tool engine, verifier, review pipeline, commits, pushes, reports, leases, events – is real.

Fault injection during the run: at 40 % the execution worker was **SIGKILLed** and restarted after the registry had
marked it offline; at 70 % the scheduler was **restarted gracefully** (SIGTERM, running steps checkpointed).

## Result: PASSED (60 min + drain)

| Check | Result |
|---|---|
| 40.1 system started | API, scheduler, execution worker up; workers registered from hosts.yaml |
| 40.2 jobs | 118 submitted, **118 succeeded**, 0 failed |
| 40.3 duration | 60 min continuous load (target-host 24 h soak pending, BLOCKER-001) |
| 40.4 resources | sampled every 60 s: 0 active leases between jobs, 0 steps in flight at the end |
| 40.5 leaks | RSS first → last quarter: API 95.4 → 95.8 MB (+0.5 %), scheduler 109.7 → 113.8 MB (+3.7 %, max 116.4), worker 61.1 → 62.7 MB (+2.7 %) |
| 40.6 stuck leases | 0 active/preempting leases, 0 waiting requests after drain |
| 40.7 worker reconnect | offline detected 51.5 s after SIGKILL (heartbeat timeout), ready again 2.2 s after restart |
| 40.8 event ordering | 12 589 events, 0 duplicate sequences, 0 ordering violations per job |
| 40.9 DB integrity | 0 unvalidated constraints, 0 orphan dependencies, 0 open attempts/in-flight steps of terminal jobs, every succeeded job has its final report |

The trial run before (3 min) found **BUG-043** (repository auto-registration by URL basename collides → HTTP 500).
