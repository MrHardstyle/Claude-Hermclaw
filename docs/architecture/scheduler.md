# Scheduler (P25)

`hermclaw/scheduler/` – DAG scheduler with PostgreSQL as the queue.

## Interfaces

- `Scheduler(sessionmaker, config, handlers: list[StepHandler], driver: JobDriver, *, settings=SchedulerSettings(), instance_id=None, services=None)`
  - `run_forever()`, `stop()`, `shutdown(checkpoint=True)`, `tick() -> TickReport`, `startup_recovery()`, `recover_expired_leases()`, `wait_idle()`
- `StepHandler` protocol: `kinds: frozenset[str]`, `async run(ctx: StepRunContext) -> StepOutcome`
- `JobDriver` protocol: `prepare(job_id, token)`, `finalize(job_id, token)`, `replan(job_id, reason, evidence, token)` → `JobPhaseResult`
- `StepOutcome.outcome ∈ {completed, failed, blocked, checkpointed, replan, cancelled}`; `retryable`, `replan_reason`, `checkpoint`
- `CancelToken`: cooperative cancel (`cancelled`) and pause/checkpoint (`checkpoint_requested`)
- `step_to()` / `job_to()`: state-machine transitions that pass through `running` when a sub-state cannot reach the target directly.

## Tick

1. reap finished tasks → 2. recover expired step leases (`recovery_step_state`; attempts become `lost/WORKER_LOST`)
3. apply cancel/pause flags (tokens; jobs without running tasks are cancelled with all open steps)
4. resume orphaned job phases (`committing`/`deploying`/`replanning` without live lock → respawn finalize/replan)
5. start preparation of queued jobs (`JobDriver.prepare`; must leave the job in a state that can reach `running`, otherwise `PREPARE_INVALID_STATE`)
6. promote `pending` steps whose dependencies completed; block steps whose dependencies are cancelled/blocked/failed without scheduled retry (`DEPENDENCY_FAILED`)
7. dispatch: due retries (`failed` + `not_before`) and resumable `checkpointed` steps → `ready`; `ready` steps ordered by job priority, step priority, age; `FOR UPDATE SKIP LOCKED`; global concurrency limit; **at most one mutating step (implement/database/docker/deploy/documentation/ssh) per job at a time**; jobs with a pending manual replan are drained (no new dispatch)
8. evaluate jobs: all steps completed → `committing` + finalize; no work left and failed/blocked steps → `replanning` (bounded by `policies.correction.max_replans_per_job`, else `REPLAN_LIMIT`); manual replan waits for running steps; empty plan → `EMPTY_PLAN`.

Each attempt is a `step_attempts` row (`initial|retry|correction|resume`), with `attempt.started/finished` events. Running steps keep their lease alive with a heartbeat (`step_heartbeat_seconds`), leases expire after `step_lease_seconds`; any scheduler instance recovers expired leases. Step timeout (`step_timeout_seconds`) is a retryable failure `STEP_TIMEOUT`; handler crashes are retryable `HANDLER_CRASHED`. Retry backoff: `retry_backoff_seconds` per attempt.

## Tests

`tests/integration/test_scheduler.py` (17 tests, real PostgreSQL): dependency order + finalize, mutation serialisation, retries/backoff, dependency failure → replan with evidence, replan budget, cancel, pause/resume from checkpoint, crash recovery (running + testing sub-state), live foreign lease respected, orphaned finalize resumed, finalize failure, missing handler / empty plan, prepare crash / invalid state, priority + concurrency, manual replan, timeout.
