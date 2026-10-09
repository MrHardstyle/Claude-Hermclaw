# Resource Manager (P09)

`hermclaw/resources/` – PostgreSQL-backed leases for the scarce resources of the cluster (Bauplan §4, §25, §32):
the exclusive large-model slot and the shared small-model memory on `.224`, the GPU, the video slot and the code
executor. All state lives in `resource_leases` / `resource_requests`, so leases survive process restarts and every
runtime instance sees the same picture. No benchmark- or task-specific logic: resources, priorities and budgets are
data (constants + `models.yaml` + `policies.yaml`).

## Modules

| module | content |
|---|---|
| `constants.py` | resource names (`GPU_224`, `LARGE_MODEL_224`, `SMALL_MODEL_224`, `VIDEO_224`, `CODE_EXECUTOR_222`, `MODEL_RESOURCES`), `OwnerKind`, `PRIORITIES`, `ROLE_OWNER_KIND`, lease/request states, validators |
| `budget.py` | `ResourceBudget`, `BudgetUsage`, `model_host_budgets(models)`, `model_host_budget(models, host)`, `validate_model_resources(models)`, `worst_case_resident_gb(profiles)` |
| `types.py` | `Lease`, `LeaseStatus`, `WaitingRequest`, `PreemptionResult`, `SweepReport`, `RecoveryReport`, `MediaLeases`, errors `LeaseLost` (`LEASE_LOST`), `LeaseNotOwned` (`LEASE_NOT_OWNED`) |
| `manager.py` | `ResourceManager` |
| `keeper.py` | `LeaseKeeper` – background heartbeat + preemption/loss callbacks |

## Priorities (Bauplan §4)

`PRIORITIES = {video: 100, image: 90, planner: 70, heavy: 60, coder: 50, fast: 30, embedding: 20}`.
`priority_for(kind, default=None)`; model leases take `profile.priority` from `models.yaml` (checked against the table
by `validate_model_resources`). `ROLE_OWNER_KIND` maps profile roles to owner kinds (`planner_fallback` → `planner`).

## Public interface

```python
ResourceManager(sessionmaker: async_sessionmaker[AsyncSession], policy: LeasePolicy, holder_id: str, *,
                budgets: Sequence[ResourceBudget] = (), poll_interval: float = 0.25,
                request_ttl_seconds: float | None = None)
ResourceManager.from_config(sessionmaker, config: HermclawConfig, holder_id, **kw)   # policies.leases + model host budgets

await m.acquire(resource, *, owner_kind, priority=None, job_id=None, step_id=None, ttl_seconds=None,
                exclusive=True, weight=0.0, preemptible=True, wait_timeout=None, poll=None,
                preempt=False, resource_group=None, metadata=None, reason=None) -> Lease
await m.try_acquire(resource, **same) -> Lease | None
await m.release(lease_id, reason="released", *, detail=None) -> bool          # idempotent; unknown id -> NotFoundError
await m.release_many(ids, reason) -> int
await m.heartbeat(lease_id, *, ttl_seconds=None) -> LeaseStatus               # LeaseLost / LeaseNotOwned
await m.status(lease_id) -> LeaseStatus ; await m.should_yield(lease_id) -> bool
await m.sweep_expired() -> SweepReport ; await m.run_maintenance(stop: asyncio.Event, *, interval=None)
await m.purge_finished_requests(*, older_than_seconds=86400) -> int    # granted/cancelled queue rows; lease rows are kept as history
await m.request_preemption(resource, requester_priority, reason, *, requester_kind=None, job_id=None, step_id=None) -> PreemptionResult
await m.withdraw_preemption(lease_id, reason="withdrawn") -> bool
await m.recover(holder_id=None) -> RecoveryReport
await m.acquire_model(profile: ModelProfileConfig, *, job_id, step_id, ttl_seconds, wait_timeout, preempt=False, preemptible=True, owner_kind=None) -> Lease
await m.acquire_gpu_for_media(kind: "video"|"image", *, job_id, step_id, ttl_seconds, wait_timeout,
                              gpu_resource="gpu-224", video_resource="video-224", drain=MODEL_RESOURCES) -> MediaLeases
await m.release_media(media, reason="completed") -> int
async with m.hold(resource, *, on_preempt=None, on_lost=None, keeper_interval=None, **acquire_kw) as keeper: ...
async with m.hold_model(profile, ...) as keeper: ...
async with m.hold_media(kind, ...) as (media, keeper): ...
await m.get_lease(id) ; await m.list_leases(resource=None, *, holder=None, states=HOLDING_STATES)
await m.waiting_requests(resource=None) ; await m.budget_usage(budget_name | ResourceBudget) -> BudgetUsage
```

`LeaseKeeper.yield_requested` / `.lost` are `asyncio.Event`s; `on_preempt(lease, status)` / `on_lost(lease)` may be
sync or async. On exit of `hold*()` the lease is released with `completed`, `preempted` (yield was requested),
`error` or `cancelled`.

## Data and decision flow

Lease states: `active → preempting → released|expired`, `active → released|expired`. Request states:
`waiting → granted|cancelled`. A lease *holds* its resource in `active` and `preempting`.

`acquire` registers a `resource_requests` row (`resource.requested`) and polls. Each attempt is one transaction:

1. `pg_advisory_xact_lock(hashtextextended('hermclaw.resource:<name>', 0))` on the **lock set** (resource + all
   members of its budgets) in sorted order – deadlock free; all deciding writers (acquire, abandon, preemption, sweep,
   recovery) use the same order. The partial unique index `uq_resource_leases_exclusive_active` is the backstop: an
   `IntegrityError` (or deadlock/serialization error) is treated as contention and retried.
2. `clock_timestamp()` of the database is the only clock.
3. expire overdue leases (`resource.expired`, `reason=ttl_expired`, or `preemption_grace_timeout` for a `preempting`
   lease) and cancel stale waiting requests of crashed waiters (`resource.expired`, `subject=request`,
   `reason=request_stale`) on the lock set – so nothing depends on a sweeper running.
4. refresh the own request (`expires_at = now + request_ttl`); re-entrancy check (the same step already holds the
   resource exclusively → `ConflictError RESOURCE_ALREADY_HELD`).
5. grant iff
   * no live waiting request for the resource is ahead (higher priority; same priority: older `created_at`, then id),
   * no higher-priority request on *another member of the same budget* waits while that member has no exclusive holder
     (i.e. it waits for capacity) – a lower-priority waiter cannot eat the capacity a higher one needs,
   * exclusive: no holder at all; shared: no exclusive holder,
   * every budget of the resource: `sum(weight of holding leases) + weight <= capacity`.
6. grant → lease row (`metadata`: `incarnation`, `request_id`, `ttl_seconds`, redacted caller `info`), request
   `granted`, `resource.acquired` (incl. `waited_seconds`). Otherwise, with `preempt=True` and blocked by holders or the
   budget, the manager requests preemption (below) in the same transaction.

Blocked reasons reported in `ResourceUnavailable(code=RESOURCE_WAIT_TIMEOUT).details["blocked"]`: `queued`,
`budget_queued`, `held`, `held_exclusive`, `budget`, `contention`. A weight larger than a budget's capacity fails at
once with `RESOURCE_OVER_CAPACITY`. On timeout, cancellation or error the request is cancelled
(`resource.expired`, `subject=request`, `reason=wait_timeout|cancelled|error`), preemptions it caused are withdrawn,
and a grant that committed unseen (cancellation right after `COMMIT`) is released (`acquire_abandoned`).

### Budgets / model host (9.7)

`model_host_budgets(models)` builds one budget per model host: members = resource groups of the host's enabled profiles
(`large-model-224`, `small-model-224`), capacity = `models.model_host_capacity_gb`. `acquire_model(profile)` leases
`profile.resource_group` with `exclusive=profile.exclusive`, `weight=profile.memory_gb`, `priority=profile.priority`;
`large-model-224` is exclusive ⇒ only one large model is leased at a time, and the large model's memory plus all shared
small models must fit. Accounting is lease-based: a model that stays resident without a lease is evictable and the
residency adapter (`hermclaw/models/residency.py`) unloads the other group members before loading (Bauplan §4 step 4).

### Safe preemption (9.6)

Only the manager changes other holders' leases. `request_preemption` (or `acquire(preempt=True)`, a runtime policy
decision – not exposed to worker daemons or model output) marks every preemptible, strictly lower-priority `active`
holder `preempting`: `metadata.preempt = {reason, requested_at, deadline, grace_seconds, requester_*, request_id}`,
`expires_at = min(expires_at, now + policies.leases.preemption_grace_seconds)`, event `resource.preempt.requested`
(`action=requested`, severity warning, on the holder's job/step). Budget-driven preemption picks the minimal set
(lowest priority first, then newest) that makes room. Holders notice it via `heartbeat()` / `should_yield()` or the
`LeaseKeeper` callback, checkpoint and release (`reason=preempted`). Heartbeats never extend a `preempting` lease
beyond its deadline; at the deadline the lease is force-expired with `reason=preemption_grace_timeout` and an event
that names the requester – work is never killed silently; the holder's next heartbeat raises `LeaseLost`.
Non-preemptible leases and equal/higher-priority holders are never touched (`PreemptionResult.refused_*`). If the
requester gives up, its preemptions are withdrawn (`resource.preempt.requested`, `action=withdrawn`; TTL restored).

### Media priority (9.8)

`acquire_gpu_for_media(kind)`: priority 100 (video) / 90 (image); exclusive, non-preemptible leases on `gpu-224`
(+ `video-224` for video) in sorted order (no deadlock between media jobs), then – concurrently – exclusive,
weightless, non-preemptible **drain leases** on `large-model-224` and `small-model-224` with `preempt=True`: the
waiting drain requests (priority 100/90) block new AI acquisitions through the queue rules, the AI holders get a
preemption request and the media job waits until they released. While the media job holds the drain leases no AI model
can be leased on `.224`; `release_media` releases GPU/video first, then the drain leases – AI workloads resume
(Bauplan §32 steps 1–7). Any failure releases what was acquired (`acquire_abandoned`).

### Crash recovery (9.9)

Leases are rows, so they survive a restart and keep blocking others until released or expired. `holder_id` is stable
per process instance (e.g. `runtime@webui-223`), each `ResourceManager` has a fresh `incarnation`. `recover()` at
startup releases all holding leases of this holder from other incarnations (`resource.released`,
`reason=holder_restarted`, detail = previous incarnation), cancels its leftover waiting requests (except requests of
the current incarnation) and runs `sweep_expired()` for everybody else's stale leases.

## Config keys

* `policies.leases.default_ttl_seconds` – lease TTL when `ttl_seconds` is not given (and request TTL lower bound).
* `policies.leases.heartbeat_seconds` – keeper interval upper bound, default request TTL, maintenance interval.
* `policies.leases.preemption_grace_seconds` – time a preempted holder has to checkpoint and release.
* `models.model_host_capacity_gb`, `models.profiles[].{resource_group, exclusive, memory_gb, priority, role, host}`.

## Events (all `source_type=resource_manager`, `source_id=<holder_id>`, on the owner's job/step)

`resource.requested` (also `requeued=true`), `resource.acquired`, `resource.released` (`reason`, `released_by`,
`was_preempting`, `held_seconds`, optional `detail`), `resource.preempt.requested` (`action=requested|withdrawn`),
`resource.expired` (`subject=lease|request`, `reason=ttl_expired|preemption_grace_timeout|request_stale|wait_timeout|
cancelled|error|holder_restarted`). Heartbeats are not evented (no state change). Payloads contain ids, names,
priorities and weights only; caller metadata is redacted before it is stored and again by the event store.

## Failure behaviour

* DB deadlock/serialization (`40P01`, `40001`, `55P03`): retried (`MAX_TX_RETRIES`); in `acquire` treated as contention.
* Unique-index violation: contention, never two exclusive holders.
* Keeper: transient heartbeat errors are counted (`heartbeat_errors`) and retried, the TTL is the safety net; a lost
  lease sets `lost` and calls `on_lost` – the holder must stop using the resource.
* Invalid input (`RESOURCE_NAME_INVALID`, `RESOURCE_PRIORITY_INVALID`, `RESOURCE_TTL_INVALID`, `RESOURCE_WEIGHT_INVALID`,
  `RESOURCE_REASON_INVALID`, `RESOURCE_HOLDER_INVALID`) → `ValidationFailed`; names are strict slugs, all SQL is
  parameterised.

## Operating on the real hosts

The manager needs only the runtime PostgreSQL on `webui-223`; it never talks to `.222`/`.224` itself.

1. Runtime startup: `m = ResourceManager.from_config(get_sessionmaker(), get_config(), f"runtime@{hostname}")`,
   `await m.recover()`, start `m.run_maintenance(stop)` as a background task.
2. Model steps: `async with m.hold_model(profile, job_id=..., step_id=..., on_preempt=checkpoint_cb)` around the
   residency switch (`ModelResidency.ensure_loaded(alias, lease_id=keeper.lease.id)`) and the model calls; on
   `yield_requested` bring the step to a checkpoint and leave the block.
3. Media steps (P29): `async with m.hold_media("video", job_id=..., step_id=...)`, then unload models, run, store
   artifacts.
4. Inspect: `GET /api/resources` (active leases + queue) or
   `select resource, owner_kind, priority, state, holder, expires_at from resource_leases where state in ('active','preempting');`.
   Stuck lease: `await m.release(lease_id, "admin_release")` or let it expire.

## Tests

* `tests/unit/test_resources_policy.py` – priorities, role mapping, example config budget (34 GB; worst case heavy +
  fast + embedding = 34), validation, lock sets, over-capacity without DB.
* `tests/integration/test_resources_manager.py` – acquire/release/events, heartbeat, expiry + sweep, inline expiry,
  wait timeout/cancel, shared vs exclusive, priority + FIFO queue, no queue jumping, budget accounting, budget queue
  across members, re-entrancy, `hold()` keeper, lost-lease callback, queries/redaction.
* `tests/integration/test_resources_concurrency.py` – 24 tasks × 3 grants over two engines (exclusivity checked in DB
  at every grant), budget never exceeded under concurrency, priority order of 14 concurrent waiters, unique index,
  IntegrityError race handled as contention.
* `tests/integration/test_resources_preemption.py` – happy path with checkpoint callback, grace cap, grace-timeout
  force expiry, non-preemptible and equal/higher priority untouched, withdrawal, minimal budget preemption,
  large-model exclusivity + model budget, model preemption, video drains AI and blocks it until release, video
  overtakes image, partial media acquisition rolled back.
* `tests/failure/test_resources_recovery.py` – crash (engine disposed) → leases survive → `recover()` releases previous
  incarnation and stale requests, current incarnation untouched, dead holders' stale leases expired, stale waiter
  request ignored, live waiter keeps its place, keeper survives transient errors, grant + cancellation does not leak,
  maintenance loop.

Run: `.venv/bin/pytest -q tests/unit/test_resources_*.py tests/integration/test_resources_*.py tests/failure/test_resources_*.py`.
