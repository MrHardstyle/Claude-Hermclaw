# Wake-on-LAN, worker readiness and idle sleep (P10)

`hermclaw/wol/` wakes sleeping worker hosts, confirms that they are ready to receive work, and puts
idle hosts back to sleep (Bauplan §2.7, §26, phase 10, steps 10.1–10.9; research
`docs/research/20261008-016-wake-on-lan.md`). The runtime calls `ensure_worker_ready(worker_id)` before it
dispatches to a worker. Work is dispatched only when the result is `ready=True`. Every stage is
recorded as a `wake_events` row and as an event.

## 1. Code map

| module | content |
|---|---|
| `errors.py` | `WakeFailureCode` (the six architecture codes) and `WolError(WorkerError)` |
| `magic.py` | `normalize_mac`, `build_magic_packet`, `validate_target`, `send_magic_packet` |
| `probes.py` | `Probes` (ping with ICMP and a TCP fallback, TCP connect, SSH banner, HTTP GET), `ProbeOutcome`, target validation |
| `readiness.py` | `WakeController`, `ReadyResult`, `StageResult`, `WakeStage`, `StageStatus`, `WakeSettings`, `RegistrySnapshot`, `RegistryLookup`, `default_registry_lookup`, `ensure_workers_ready` |
| `idle.py` | `IdleSleepPolicy`, `IdleSleepSettings`, `IdleDecision`, `IdleReason`, `IdleSleepOutcome`, `SleepResult`, `RemoteCommandRunner`, `worker_resource_names` |
| `status.py` | `WorkerUiStatus` (`STARTING`, `WAKING`, `READY`, `BUSY`, `ERROR`, `SLEEPING`), `ui_status_for` |
| `recording.py` | `record_wake_event`, `force_worker_state` (shared persistence helpers) |

The package has no external dependencies beyond the existing stack: asyncio UDP/TCP, `httpx`, SQLAlchemy and the
`ping` binary when it is installed.

## 2. Public interfaces

```python
# magic packet (10.2)
normalize_mac(mac: str) -> str                      # "aa:bb:cc:dd:ee:ff"; accepts ':'/'-', dotted, bare hex
build_magic_packet(mac: str) -> bytes               # b"\xff"*6 + mac*16 == 102 bytes
await send_magic_packet(mac, broadcast="255.255.255.255", port=9, *, copies=1,
                        source_address=None, timeout_seconds=2.0) -> int   # bytes sent; WolError WOL_SEND_FAILED

# readiness (10.3–10.8)
WakeController(sessionmaker, config: HermclawConfig | HostsConfig, registry_lookup: RegistryLookup | None = None, *,
               http_client: httpx.AsyncClient | None = None, probes: Probes | None = None,
               registry: WorkerRegistry | None = None, settings: WakeSettings | None = None,
               sender: MagicSender | None = None, clock=time.monotonic, sleep=asyncio.sleep, now=utcnow)
await wc.ensure_worker_ready(worker_id, job_id=None, *, required_capabilities=None,
                             deadline_seconds=None) -> ReadyResult
await wc.worker_ui_status(worker_id) -> WorkerUiStatus
await wc.ui_statuses() -> dict[str, WorkerUiStatus]          # every execution/model worker host
await wc.aclose()
await ensure_workers_ready(wc, worker_ids, job_id=None) -> dict[str, ReadyResult]

ReadyResult: worker_id, ready, status: WorkerUiStatus, error_code: WakeFailureCode | None, message, woke,
             packets_sent, worker_state, duration_ms, stages: list[StageResult]
             .stage(WakeStage) -> StageResult | None ; .raise_for_failure() -> self | raises WolError(code)
RegistryLookup = async (session, worker_id) -> RegistrySnapshot(state, capabilities, last_heartbeat_at, compatible) | None
default_registry_lookup(registry: WorkerRegistry | None = None) -> RegistryLookup   # reads WorkerRegistry.get_worker

# idle sleep (10.9)
class RemoteCommandRunner(Protocol):
    async def run(self, host_id: str, command: str, timeout: float) -> tuple[int, str, str]: ...
IdleSleepPolicy(sessionmaker, config, runner: RemoteCommandRunner, *, settings: IdleSleepSettings | None = None,
                registry: WorkerRegistry | None = None, resources: Mapping[str, Collection[str]] | None = None, now=utcnow)
await p.evaluate(worker_id, *, session=None) -> IdleDecision          # read-only decision
await p.maybe_sleep(worker_id) -> IdleSleepOutcome                   # decide + execute
await p.run_once() -> list[IdleSleepOutcome] ; await p.run_forever(stop: asyncio.Event, *, interval_seconds=None)
```

`ensure_worker_ready` does not raise when a wake fails; it returns a `ReadyResult` with `error_code` set. It does
raise in two cases, both before any stage runs:

* `WorkerNotFound` when the id is not a configured host.
* `ConfigError` when the host configuration cannot be used: the address, the worker API URL (for example one with
  credentials) or a service probe path such as `//other-host/x`.

## 3. Pipeline and data flow

```
detect ─▶ wol_send ─▶ ping ─▶ ssh ─▶ worker_api ─▶ services ─▶ capabilities ─▶ READY
  │  (host already reachable: wol_send + ping recorded as "skipped")
  └─ the registry row state and a quick TCP probe of the worker API port
```

| stage | check | failure code |
|---|---|---|
| `detect` | `workers.state` and a TCP connect to the worker API port (`quick_probe_timeout_seconds`) | – |
| `wol_send` | Sends the magic packet to `wake_on_lan.broadcast:port` and sets the registry state to `waking`. Socket errors are retried. An invalid MAC or broadcast address is not retried. Fails at once if the host is down and WOL is disabled or has no MAC. | `WOL_SEND_FAILED` |
| `ping` | `ping -n -c 1 -W <s> <addr>` when the binary exists. Otherwise, or when ICMP gets no reply, a TCP connect to the SSH port; a refused connection also proves that the host is up. While it waits, the packet is re-sent every `ping_timeout/max_attempts` (or `resend_interval_seconds`). Total packets ≤ `max_attempts`. | `PING_TIMEOUT` |
| `ssh` | The `SSH-…` identification line (RFC 4253 §4.2) on `ssh.port` (default 22) | `SSH_TIMEOUT` |
| `worker_api` | `GET <worker_api>/health` → 2xx. If the body is JSON, `status` must be `ok` or `degraded`. A `worker_id` that differs from the expected one fails at once. | `WORKER_API_TIMEOUT` |
| `services` | Every other `hosts.yaml` service, for example Ollama `/api/version`. A service with `http_path` needs an HTTP 2xx or 3xx answer; one without needs a TCP connect. Services on the worker API port or the SSH port are skipped because earlier stages already checked them. | `MODEL_SERVICE_TIMEOUT` |
| `capabilities` | The registry is current: the worker is compatible (otherwise the stage fails at once), its state is `ready` or `busy`, its heartbeat is newer than the wake start when the host was not demonstrably live, and `required_capabilities` ⊆ its reported capabilities | `CAPABILITY_MISSING` |

**Timeouts.**

* `ping` uses `ping_timeout_seconds`.
* `ssh` … `capabilities` share one budget of `service_timeout_seconds`, which starts when the host is reachable.
* An overall deadline caps every stage. It is `ping + service + overall_slack_seconds`, or less when the caller
  passes `deadline_seconds`.
* Each probe is bounded by `probe_timeout_seconds`. Failed probes are repeated after `probe_interval_seconds`.

**Persistence and events.** Every stage writes in its own short transaction, so the UI sees progress live
through NOTIFY:

* A `wake_events` row (`stage`, `status` = `ok|skipped|failed`, `error_code`, redacted `details`).
* A `worker.wake.stage` event with `stage`, `status`, `attempts`, `detail`, `data` and `ui_status`.

Further events:

* `worker.wake.sent` for each packet: `mac`, `broadcast`, `port`, `attempt`, `max_attempts`, `bytes`.
* `worker.ready`: `woke`, `packets_sent`, `worker_state`, `ui_status`.
* `worker.wake.failed`: `error_code`, `stage`, `message`, `worker_state`, `ui_status=ERROR`. Its severity is `error`.
* With a `job_id`, the controller also writes `status` lines for the job's live view. These are "Wake-on-LAN
  gesendet", "geweckt und bereit" and "nicht bereit (CODE)".

**Worker state.**

* Sending a packet sets the state to `waking` (`WorkerRegistry.set_state`, reason `wake_on_lan`).
* On success, the state is normally already `ready`/`busy` from the heartbeat. A row still in
  `offline|sleeping|waking|error` is promoted to `ready` (`worker.state`, reason `wake_ready`).
* On failure, a row that is not live becomes `error` (reason is the failure code in lower case). A live state
  that a heartbeat reported (`ready`, `busy`, `draining`, `starting`) is never overwritten. For example, a ready
  worker that lacks one capability stays ready for other work.

**UI status (10.8).** `ui_status_for(state, wakeable)` maps registry states to the six UI values:

| registry state | UI status |
|---|---|
| `starting` | `STARTING` |
| `waking` | `WAKING` |
| `ready` | `READY` |
| `busy`, `draining` | `BUSY` |
| `sleeping` | `SLEEPING` |
| `error` | `ERROR` |
| `offline` | `SLEEPING` if the host is wakeable, `ERROR` if not |

While the pipeline runs, stage events carry the progress status:

* `WAKING` once a packet has been sent.
* The live state for a worker that is only being probed, so a ready worker never flashes `STARTING`.
* `STARTING` for a host that is reachable but whose worker is not live yet.

**Concurrency.**

* Concurrent calls for the same worker and the same capability set share one in-flight run (single flight).
* A cancelled waiter does not cancel the shared run (`asyncio.shield`). The run is bounded by its deadline.
* `aclose()` cancels the in-flight runs. A run cancelled mid-way leaves `waking`, which the registry sweep turns
  into `offline` after `waking_timeout_seconds`.
* Across processes, a wake is idempotent: an extra magic packet is harmless.

## 4. Idle sleep (10.9)

A worker host is put to sleep only when all of these conditions hold:

1. `idle_sleep_command` is configured and is a single line (no NUL, CR or LF).
2. Wake-on-LAN is enabled and has a MAC, unless `require_wake_on_lan=False`. A host that cannot be woken is never
   put to sleep.
3. The registry state is `ready`, and the heartbeat reports no `active_job` or `active_step`.
4. No step in `leased|running|testing|verifying|reviewing` is assigned to the worker.
5. No `running` step attempt is on the worker.
6. No `active` or `preempting` lease, and no live `waiting` resource request, exists on the worker's resources.
7. The last activity is at least `idle_after_minutes` old.

The worker's resources come from `worker_resource_names` (or the `resources=` mapping):

* the `resource_group` of every model profile whose `host` is the worker;
* the host label `lease_resources` (comma separated);
* the worker id itself.

The last activity is the latest of these timestamps:

* the row's `state_changed_at`, or `created_at` when there is none;
* the `updated_at` of steps assigned to the worker;
* when step attempts on the worker started or finished;
* lease heartbeats and releases on the worker's resources;
* the latest `wake_events` row. A recent wake, or a failed sleep attempt, therefore postpones the next sleep.

**Execution (`maybe_sleep`).**

1. **Transaction A.** Lock the worker row (`FOR UPDATE`), decide, set the state to `sleeping` (`worker.state`,
   reason `idle_sleep`) and write the `wake_events` row `sleep/requested` with a `status` event. Then commit. From
   now on the scheduler does not select the worker, and a second policy process sees `sleeping` and skips it.
2. **Transaction B.** Check again for active work, because dispatch can race the decision. If there is any, set
   the state back to `ready` (reason `idle_sleep_aborted`) and record the result `aborted`. The command is not
   run.
3. Run the command through `RemoteCommandRunner.run(host_id, command, timeout)`, guarded by `wait_for`.
4. Record the result:

   | command result | outcome | worker state |
   |---|---|---|
   | exit 0 | `slept` | stays `sleeping` |
   | other exit code | `failed` | restored to `ready` (reason `idle_sleep_failed`) |
   | timeout, transport error or ssh exit 255 | `unknown` | stays `sleeping`; a host that is still awake corrects it with its next heartbeat |

   stderr and error text are redacted and clipped. The command string itself is never stored or logged.

## 5. Configuration

The policy reads these keys from `hosts.yaml` (`HostConfig`):

| key | used for |
|---|---|
| `id` | worker id |
| `address` | host address (validated) |
| `worker_api` | worker API URL, default `http://<address>:8787` |
| `ssh.port` | SSH port, default 22 |
| `services[].{name, port, http_path}` | service probes |
| `idle_sleep_command` | idle sleep |
| `labels.lease_resources` | idle sleep: extra resources of the worker |
| `wake_on_lan.enabled`, `mac`, `broadcast`, `port` (9) | magic packet |
| `wake_on_lan.ping_timeout_seconds` (90) | `ping` stage |
| `wake_on_lan.service_timeout_seconds` (180) | `ssh` … `capabilities` stages |
| `wake_on_lan.max_attempts` (3) | packets per wake |

The policy also reads `models.yaml` `profiles[].{host, resource_group}` to find each worker's resources. The
config is trusted operator input. URLs are still built only from validated parts: the scheme is http(s), there
are no credentials, query or fragment, and a path cannot switch the authority. Redirects are not followed, and
`trust_env=False` keeps LAN probes away from proxies.

Code-level tuning:

* `WakeSettings`: probe interval and timeout, quick-probe timeout, resend interval, send back-off, packet copies,
  `source_address` (to bind to one interface on a multi-homed orchestrator), heartbeat skew, overall slack and
  `accept_degraded_health`.
* `IdleSleepSettings`: `idle_after_minutes` (30), `command_timeout_seconds` (60), `require_wake_on_lan` (true),
  `check_interval_seconds` (60).

There is no `policies.yaml` section for these yet (see shared changes below).

## 6. Failure behaviour

* The only codes reported are `WOL_SEND_FAILED`, `PING_TIMEOUT`, `SSH_TIMEOUT`, `WORKER_API_TIMEOUT`,
  `MODEL_SERVICE_TIMEOUT` and `CAPABILITY_MISSING`. `ReadyResult.raise_for_failure()` raises
  `WolError(code=...)`, which is a `WorkerError` with `http_status` 503.
* All retries are bounded: packets ≤ `max_attempts`, every stage is bounded by its budget, and the whole run is
  bounded by the overall deadline.
* Three cases fail without waiting: a fatal probe (the health endpoint answers as another worker, or the target
  is invalid), an incompatible worker (protocol or kind mismatch in the registry), and WOL being disabled while
  the host is down.
* No probe raises for an unreachable target. Probes report the exception type only, never response bodies. Only
  selected health fields (`status`, `state`, `worker_id`, `worker_version`, `protocol_version`, each truncated)
  reach events.
* `wake_events.details` and every event payload pass through the redactor.

**Known limitation.** An idle sleep can race a concurrent wake that found the host still up and skipped the
magic packet. In that case the wake fails at a later stage, usually `SSH_TIMEOUT`, and the scheduler's retry
wakes the host properly. The window is small: the wake's `detect` row counts as activity, and the idle policy
checks active work again under the `sleeping` state.

## 7. Operating on the real hosts

1. Prepare the NIC on each worker host, `.222` and `.224`:
   * Check that `ethtool <if>` shows `Supports Wake-on: g`.
   * Make `ethtool -s <if> wol g` persistent with a systemd oneshot (Ansible role `wol_target`).
   * Enable WOL in the BIOS/UEFI. Use wired Ethernet only.
2. In `/etc/hermclaw/hosts.yaml`, replace the placeholder MACs with the real ones from the inventory. Use
   `broadcast: 192.168.178.255` and `port: 9`. Add `idle_sleep_command` (for example
   `sudo /usr/bin/systemctl suspend`, allowed in `ssh.allow_sudo_commands`) and, if needed,
   `labels.lease_resources: "code-executor-222"`.
3. Runtime wiring: `WakeController(get_sessionmaker(), get_config())` is shared by the scheduler. Call it with
   `ensure_worker_ready(step.assigned_worker_id, job.id, required_capabilities=[step.capability])` before
   dispatch. Run `IdleSleepPolicy(get_sessionmaker(), get_config(), ssh_runner).run_forever(stop)` as a
   background task once the SSH admin tool (P26) provides the runner.
4. Live check, from the orchestrator:

   ```
   HERMCLAW_CONFIG_DIR=/etc/hermclaw HERMCLAW_LIVE_WOL_WORKER=model-224 .venv/bin/pytest -m live tests/integration/test_wol_live.py
   ```

   Add `HERMCLAW_LIVE_WOL_DB=1` for the full pipeline against the production database. To verify a real wake,
   suspend the host first (`systemctl suspend`).
5. To diagnose a wake, use `SELECT stage, status, error_code, details FROM wake_events WHERE worker_id='model-224'
   ORDER BY created_at DESC LIMIT 20;` or the `worker.wake.*` events in the UI.

## 8. Tests

* `tests/unit/test_wol_magic.py`: packet layout, the MAC forms that are accepted and rejected, target validation,
  real UDP sends to a unicast listener (one and several copies), loopback broadcast with `SO_BROADCAST`, a real
  socket error mapped to `WOL_SEND_FAILED`.
* `tests/unit/test_wol_probes.py`:
  * TCP open, closed (counts as host alive) and unroutable (`240.0.0.1`).
  * Ping through a fake `ping` executable: the argument vector, exit 0, exit 1 with TCP fallback, refusal of
    option injection.
  * TCP fallback when no `ping` binary exists.
  * SSH banner, including lines before the banner, a wrong protocol and a silent server.
  * HTTP: status, JSON, no redirect following, body cap, timeout, an injected client.
  * Address, URL and path validation, and the UI status mapping.
* `tests/integration/test_wol_readiness.py`: a `FakeHost` whose UDP "NIC" boots the SSH, worker API and Ollama
  servers and sends a registry heartbeat when its magic packet arrives. Covers:
  * the full pipeline from `offline`, with `wake_events` rows, events, state transitions and status lines;
  * an already-ready worker (probe only, UI status) and a busy worker;
  * the fresh-heartbeat requirement;
  * single flight and cancelled waiters;
  * several hosts at once and an injected registry lookup.
* `tests/failure/test_wol_failures.py`:
  * Every failure code through real unreachable or broken services: WOL disabled, socket errors retried up to
    `max_attempts`, invalid broadcast not retried, `PING_TIMEOUT` with bounded re-sends, SSH closed, overall
    deadline, health 503, wrong `worker_id` (fails at once), unknown health status, Ollama 500, a closed TCP-only
    service, a missing capability (live state kept), no heartbeat after a wake, an incompatible worker.
  * Config errors before any stage runs, and an unregistered host.
* `tests/integration/test_wol_idle.py`:
  * Every `IdleReason`, resource names from `models.yaml` and labels.
  * Command outcomes: success, a definite failure (state restored, stderr redacted), ambiguous results (255,
    transport error, timeout), self-correction through a heartbeat.
  * Races: work arriving during the sleep request (aborted, runner not called), two concurrent policies (one
    command).
  * `run_once` and `run_forever`.
* `tests/integration/test_wol_live.py` (`-m live`, BLOCKER-001): a real magic packet and probes, and the full
  pipeline on the LAN.

```
.venv/bin/pytest -q tests/unit/test_wol_magic.py tests/unit/test_wol_probes.py tests/integration/test_wol_readiness.py \
    tests/integration/test_wol_idle.py tests/failure/test_wol_failures.py
```

## 9. Shared changes requested (outside `hermclaw/wol/`)

* `config/hosts.example.yaml`: add an example `idle_sleep_command` and `labels.lease_resources`
  (`code-executor-222` for `exec-222`, `gpu-224,video-224` for `model-224`).
* `PoliciesConfig`: an optional `wake_on_lan` section (`idle_after_minutes`, `idle_command_timeout_seconds`,
  `probe_interval_seconds`) so that operators can tune `WakeSettings` and `IdleSleepSettings` without code.
* `EventType`: optionally a dedicated `WORKER_SLEEP = "worker.sleep"`. Idle sleep currently uses `worker.state`
  plus `status` events with `action=idle_sleep`.
