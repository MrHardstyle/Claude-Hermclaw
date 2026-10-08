# Worker Protocol and Worker Daemons (P07)

Bauplan §2.3, §2.4, §24 (Worker Registry), §35 (Worker Authentication), Phase 7 (steps 7.1–7.9);
DECISIONS D-006 (worker auth), D-007 (media backends), D-009 (live steps blocked by BLOCKER-001).
Research: R-007 Ollama (`docs/research/20261008-007-ollama.md`), R-017 NVIDIA GTX 1080
(`20261008-017-nvidia-gtx1080.md`), R-020 systemd credentials (`20261008-020-systemd-credentials.md`).

## 1. Purpose

The orchestrator (`.225`) dispatches work to two kinds of worker hosts. The protocol between them covers:

* **Registry.** PostgreSQL holds which workers exist, their state, what they can do, and how healthy they are.
* **Heartbeats.** Each daemon pushes a signed snapshot every 15 s. If heartbeats stop, the sweep marks the worker offline.
* **Daemons.** `.222` runs the execution worker (workspaces, sandboxed commands, container recovery). `.224` runs the model worker (Ollama residency, GPU telemetry, selftest, and the extension point for media).
* **Authentication.** Each worker has its own token, and every request in either direction carries an HMAC-SHA256 signature.
* **Compatibility.** Both sides must speak the same `WORKER_PROTOCOL_VERSION`. A worker that does not is fenced off in state `error`.

Worker daemons have **no planning authority and make no scope decisions**. They only carry out signed requests.
Residency policy (what model is loaded, leases such as `large-model-224`) is decided on the orchestrator.
Git mutations remain runtime-controlled. The execution worker only receives a tar of the workspace and
returns one.

## 2. Code map

| Path | Role |
|---|---|
| `hermclaw/contracts/worker.py` | request contracts: `WorkerHeartbeat`, `CommandRequest/Result`, `ModelLoadRequest`, `WORKER_PROTOCOL_VERSION` (shared foundation) |
| `hermclaw/workers/schemas.py` | response/registry schemas (`HeartbeatAck`, `WorkerInfo`, `DaemonHealth`, `WorkspaceInfo`, …) + `worker_api_schemas()` (7.1) |
| `hermclaw/workers/auth.py` | tokens, HMAC signing/verification, replay cache, `WorkerRequestSigner` (7.5). Import-light, used by both sides |
| `hermclaw/workers/errors.py` | typed errors with stable codes |
| `hermclaw/workers/registry.py` | `WorkerRegistry`: registration, heartbeat ingest, capabilities, health, offline sweep, selection (7.2–7.4, 7.8, 7.9) |
| `hermclaw/workers/monitor.py` | `OfflineMonitor` background loop (7.8) |
| `hermclaw/workers/client.py` | `ExecutionWorkerClient`, `ModelWorkerClient`, `probe_health` |
| `hermclaw/workers/api.py` | FastAPI router `/api/workers` (heartbeat ingest + registry read/admin) |
| `worker/common/*` | daemon building blocks: env settings, signed-request middleware, heartbeat sender, `/proc` metrics, `nvidia-smi`, base app |
| `worker/execution/app.py`, `__main__.py` | `.222` daemon (7.6); tar helpers and sandbox come from `worker/execution/{workspaces,sandbox}.py` (sandbox component) |
| `worker/model/app.py`, `ollama.py`, `__main__.py` | `.224` daemon (7.7) |

## 3. Public interfaces

### 3.1 Authentication (`hermclaw.workers.auth`)

Headers of a signed request:

```text
X-Hermclaw-Worker:    <worker id whose credential signs the request>
X-Hermclaw-Timestamp: <unix seconds>
X-Hermclaw-Nonce:     <32 hex chars, random per request>
X-Hermclaw-Signature: v1=<hex HMAC-SHA256(token, canonical)>
Authorization:        Bearer <token>        (only over https by default, see below)

canonical = "hermclaw-worker-v1|METHOD|/path[?raw-query]|timestamp|nonce|sha256_hex(body)"
```

The spec's `method|path|timestamp|sha256(body)` scheme is extended in two ways. A version prefix is
added. A nonce is added so that two identical requests in the same second are both accepted while an
exact replay is still rejected.

Verification order:

1. Header format.
2. Expected worker.
3. A credential is known for that worker.
4. Clock skew is at most **120 s**.
5. Optional bearer token, compared in constant time.

These five checks look only at headers and run *before the body is read*. Then come:

6. The HMAC check. It uses `hmac.compare_digest` against every valid token (current and previous, for rotation) and never exits early.
7. The replay cache. A nonce is stored only after the signature is valid, so unauthenticated requests cannot poison the cache.

```python
generate_worker_token() -> str                                   # 384 bit, url-safe
load_token_file(path: Path) -> list[str]                         # first line = current token
resolve_token_ref(ref: str, *, environ=None) -> list[str]        # cred:<name> | file:<path> | env:<VAR>
class StaticTokenStore(tokens: Mapping[str, str | Sequence[str]])
class RefTokenStore(refs: Mapping[str, str], *, ttl_seconds=30.0)   # Settings.worker_token_refs, re-read for rotation
class TokenFile(path: Path, *, ttl_seconds=30.0)                     # daemon side
sign_headers(*, worker_id, token, method, path, query="", body=b"", now=None, nonce=None, include_bearer=True) -> dict[str, str]
precheck_signed_request(*, headers, tokens_for, expected_worker_id=None, max_skew_seconds=120, require_bearer=False, now=None) -> PrecheckedRequest
verify_prechecked(pre, *, method, path, query, body_digest, replay_cache=None) -> VerifiedRequest
verify_signed_request(*, method, path, query, headers, body=b"", tokens_for, body_digest=None, expected_worker_id=None,
                      max_skew_seconds=120, replay_cache=None, require_bearer=False, now=None) -> VerifiedRequest
class ReplayCache(*, window_seconds=240, max_entries=100_000)
class WorkerRequestSigner(httpx.Auth)(worker_id, token: str | Callable[[], str], *, include_bearer: bool | None = None)
```

Error codes (`WorkerAuthError`, HTTP 401): `WORKER_AUTH_MISSING`, `WORKER_AUTH_WRONG_WORKER`,
`WORKER_AUTH_UNKNOWN`, `WORKER_AUTH_SKEW`, `WORKER_AUTH_BAD_TOKEN`, `WORKER_AUTH_BAD_SIGNATURE`,
`WORKER_AUTH_REPLAY`. Token problems raise `WORKER_TOKEN_MISSING`, `WORKER_TOKEN_TOO_SHORT` (shorter than 32
characters) or `WORKER_TOKEN_REF_INVALID`.

**Bearer over plaintext (hardening of D-006).** `WorkerRequestSigner(include_bearer=None)`, the default, sends
`Authorization: Bearer` only when the URL scheme is `https`. Over plain HTTP the HMAC signature alone
authenticates the request, so the shared secret never crosses the LAN in clear text. A sniffed bearer
token would let an attacker forge signatures. Verifiers still accept and check a bearer token when one is
present. `True` and `False` force the header on or off.

Every token that is loaded is registered with `hermclaw.core.redaction.DEFAULT_REDACTOR`. It therefore
cannot show up in logs, events or command output.

### 3.2 Registry (`hermclaw.workers.registry`)

```python
@dataclass(frozen=True)
class RegistrySettings:
    heartbeat_interval_seconds: float = 15.0
    offline_after_missed: int = 3            # offline after 3 x interval without heartbeat
    waking_timeout_seconds: float = 600.0
    auto_register: bool = True               # authenticated heartbeat of an unknown id creates the row
    health_retention_days: int = 7

class WorkerRegistry(settings: RegistrySettings | None = None, *, clock=utcnow):
    async def register_from_config(session, hosts: HostsConfig, capabilities: CapabilitiesConfig | None = None) -> list[Worker]
    async def ingest_heartbeat(session, hb: WorkerHeartbeat, *, remote_addr=None, received_at=None) -> HeartbeatOutcome
    async def set_state(session, worker_id, state, *, reason) -> bool          # offline|sleeping|waking|error only
    async def set_drain(session, worker_id, drain: bool, *, reason="operator") -> WorkerState
    async def sweep_offline(session, *, now=None) -> list[str]                 # 7.8
    async def prune_health(session, *, older_than_days=None, now=None) -> int
    async def list_workers(session, *, kind=None, state=None) -> list[WorkerInfo]
    async def get_worker(session, worker_id, *, health_limit=0) -> WorkerDetail
    async def select_worker(session, capability, *, kind=None, include_busy=False, exclude=(), now=None) -> WorkerInfo | None
    async def workers_for_capability(session, capability, *, kind=None) -> list[WorkerInfo]   # incl. sleeping/declared (WOL)

class OfflineMonitor(sessionmaker, registry, *, interval_seconds=None, prune_every_seconds=3600.0):
    start() / await stop() / await run_once() -> list[str]
```

The caller owns the transaction. Registry methods only `flush`, and every event is appended in the same
transaction as the row change.

### 3.3 Orchestrator API (`hermclaw.workers.api`)

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /api/workers/heartbeat` | worker HMAC | ingest a `WorkerHeartbeat`, answer `HeartbeatAck` |
| `GET /api/workers?kind=&state=` | `admin_dependencies` | registry list (`WorkerInfo[]`) |
| `GET /api/workers/{worker_id}?health_limit=20` | `admin_dependencies` | detail with recent `worker_health` samples |
| `POST /api/workers/{worker_id}/drain` | `admin_dependencies` | sticky operator drain on or off (`DrainRequest`) |

```python
create_workers_router(*, admin_dependencies: Sequence[Depends] = ()) -> APIRouter
install_workers_api(app, ctx: WorkerApiContext | None = None, *, admin_dependencies=()) -> None
@dataclass class WorkerApiContext(sessionmaker, token_store: TokenStore, registry=WorkerRegistry(), replay_cache=ReplayCache(),
                                  max_skew_seconds=120, max_body_bytes=256 KiB)
```

If `app.state.workers_api` is not set, the context is built from process settings
(`get_sessionmaker()`, `Settings.worker_token_refs`). The API phase mounts the router and passes its
user-auth dependency as `admin_dependencies`.

Heartbeat answers:

* `200 HeartbeatAck` (`accepted`, `state`, `compatible`, `expected_protocol_version`, `heartbeat_interval_seconds`, `stale`).
* `401` auth failure.
* `403 WORKER_ID_MISMATCH`: the body's `worker_id` is not the signing worker.
* `413 PAYLOAD_TOO_LARGE`: only after the headers have been verified.
* `422 VALIDATION_FAILED`.

### 3.4 Worker clients (`hermclaw.workers.client`)

```python
class WorkerClient(base_url, *, worker_id, token: str | Callable[[], str], timeout_seconds=30, connect_timeout_seconds=5,
                   get_retries=2, retry_backoff_seconds=0.5, transport=None, include_bearer: bool | None = None)
    for_worker(info: WorkerInfo, token_store: TokenStore, **kw) -> Self        # token looked up per request
    health() -> DaemonHealth;  ensure_compatible() -> DaemonHealth;  selftest(SelftestRequest | None) -> SelftestResult
class ExecutionWorkerClient(WorkerClient)
    upload_workspace(ws, tar_bytes, *, mode="replace"|"merge") -> WorkspaceInfo
    download_workspace(ws, *, paths=None) -> bytes;  workspace_manifest(ws) -> WorkspaceSyncManifest
    workspace_info(ws) -> WorkspaceInfo;  delete_paths(ws, paths) -> DeletePathsResult;  delete_workspace(ws) -> WorkspaceInfo
    run_command(CommandRequest) -> CommandResult;  command_status(request_id) -> CommandStatus | None
    recover_containers(*, force=False) -> RecoverResult
class ModelWorkerClient(WorkerClient)
    models() -> ModelsResponse;  loaded_models() -> list[LoadedModel]
    load_model(ModelLoadRequest, *, exclusive=False, keep=()) -> ModelLoadResult;  unload_model(model) -> ModelUnloadResult
    gpu_info() -> GpuResponse;  gpus() -> list[GpuInfo]
probe_health(base_url, *, timeout_seconds=5.0) -> DaemonHealth                    # unauthenticated readiness probe
```

* Every request is signed, and each retry gets a fresh nonce.
* **Only `GET` requests are retried**, on transport errors and on 502, 503 and 504. `POST`, `PUT` and `DELETE` are never retried. `POST /v1/commands` is idempotent per `request_id` on the daemon, so a caller can resubmit deliberately.
* The HTTP timeout of `run_command` is the command timeout plus 30 s.
* Workspace ids and request ids are validated before they become URL segments. httpx would otherwise normalize `..` and quietly address another endpoint. Invalid ids raise `ValueError`.

Error mapping:

| Situation | Error |
|---|---|
| connection failed or connect timeout | `WorkerUnreachable` |
| read timeout | `WorkerTimeout` |
| HTTP 401 or 403 | `WorkerAuthFailed` |
| HTTP 503 with `WORKER_BUSY` | `WorkerBusy` |
| other HTTP error | `WorkerRemoteError`, carrying `status_code` and `remote_code` |
| response does not match the schema | `WorkerProtocolError` |
| `ensure_compatible()` mismatch | `WorkerProtocolError`, code `WORKER_INCOMPATIBLE` or `WORKER_IDENTITY_MISMATCH` |

### 3.5 Daemon endpoints

Every endpoint except `GET /health` must carry a signature from the orchestrator made with *this worker's*
credential. Websocket handshakes are always refused (fail closed). Every error has the form
`{"error": {"code", "message", "details"}}`.

Execution worker `.222` (`worker.execution.app.create_app(settings, *, runner=None, workspace_ops=None, token_file=None, heartbeat=True)`):

| Endpoint | Behaviour |
|---|---|
| `GET /health` | `DaemonHealth` with checks `workspaces_writable` and `sandbox_engine` |
| `PUT /v1/workspaces/{ws}?mode=replace\|merge` | tar upload. `replace` extracts into `.incoming-*` and then swaps atomically under the workspace write lock |
| `GET /v1/workspaces/{ws}` / `/manifest` / `/archive?paths=` | info, `path -> sha256` manifest, tar download (read lock) |
| `POST /v1/workspaces/{ws}/delete` | delete relative paths. No `..`, no absolute paths, never follows a symlinked parent |
| `DELETE /v1/workspaces/{ws}` | remove the workspace |
| `POST /v1/commands` | run through `SandboxRunner.run(req, workspace_dir)` (read lock). Idempotent per `request_id`; reusing an id for a different command returns `409 REQUEST_ID_CONFLICT`. No free slot returns `503 WORKER_BUSY`; draining returns `503 WORKER_DRAINING`. stdout, stderr and error are redacted |
| `GET /v1/commands/{request_id}` | `running` or `finished` plus the result (the last 1000 are kept) |
| `POST /v1/containers/recover?force=` | `podman`/`docker rm -f` of every container labelled `WORKER_CONTAINER_LABEL`. Refused (409) while commands run unless `force` |
| `POST /v1/selftest` | writable, disk ≥ 1 GiB, engine version, and a real sandbox `echo` |

Workspace ids must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`. This is enforced centrally in
`ExecutionService.workspace_dir()`, so it also covers `CommandRequest.workspace`, which arrives in the JSON
body and is not checked as a path parameter. Violations return `400 WORKSPACE_ID_INVALID`.

The tar helpers (`extract_tar_safely`, `build_tar`, `manifest`) and the sandbox (`make_sandbox(policy)`)
belong to the sandbox component. Both are imported lazily and can be injected.

Model worker `.224` (`worker.model.app.create_app(settings, *, ollama_transport=None, extra_routers=(), ...)`):

| Endpoint | Behaviour |
|---|---|
| `GET /health` | checks `ollama` (`/api/version`) and `gpu` (`nvidia-smi`) |
| `GET /v1/models` | `loaded` (`/api/ps`) + `installed` (`/api/tags`) |
| `POST /v1/models/load?exclusive=&keep=` | `POST /api/generate` with an empty prompt, `options.num_ctx = context_tokens` and `keep_alive`, then checks `/api/ps`. `exclusive` first unloads every other resident model except those in `keep`. Errors: `404 MODEL_NOT_INSTALLED`, `502 MODEL_LOAD_FAILED` |
| `POST /v1/models/unload` | `keep_alive: 0`, then polls `/api/ps` until the model is gone. Gives up with `504 MODEL_UNLOAD_TIMEOUT` |
| `GET /v1/gpu` | `nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu,temperature.gpu,driver_version --format=csv,noheader,nounits` |
| `POST /v1/selftest` | Ollama version and ps, GPU, disk ≥ 2 GiB, and an optional tiny inference. Only counters are returned, never generated text |

Load and unload are serialized by `ModelService.residency_lock`. Inference traffic does **not** go
through the daemon; the model gateway talks to Ollama directly. The `OLLAMA_URL` default is `http://127.0.0.1:11434`.

**Media extension point (P29).** `create_app(..., extra_routers=[...])` mounts additional routers behind
the same middleware and error handlers. The marked block in `worker/model/app.py` documents the
contract: use `state.work(...)` so the worker reports busy, take `residency_lock` for GPU use, and announce
capabilities through `WORKER_CAPABILITIES`.

## 4. Data and event flow

```text
daemon (HeartbeatSender, every interval)                orchestrator
  build WorkerHeartbeat: state, capabilities, /proc cpu/ram/load,
  disk (shutil), nvidia-smi gpus, ollama /api/ps, versions
  sign (HMAC) ─── POST /api/workers/heartbeat ───────────▶ precheck headers → read ≤256 KiB → verify HMAC + replay
                                                          WorkerRegistry.ingest_heartbeat (row lock FOR UPDATE)
                                                            workers row: state, versions, last_heartbeat_at, active job/step
                                                            worker_capabilities: upsert reported, delete vanished
                                                            worker_health: one sample row
                                                            events: worker.state / worker.registered / worker.offline
  ◀──────────────────────────── HeartbeatAck (interval, compatible) ─┘
OfflineMonitor (every offline_after/2) → sweep_offline (SKIP LOCKED) → state offline + worker.state + worker.offline
scheduler/tool layer → select_worker(capability) → WorkerClient.for_worker(info, token_store) → signed daemon calls
```

Events, all written with `source_type="worker_registry"` and `source_id=<worker id>`:

* `worker.registered`
  * A row was created from config or from the first authenticated heartbeat (`source`).
  * The config changed (`action=config_updated`, `changed=[…]`).
  * The worker was removed from the config (`action=removed_from_config`).
* `worker.state`
  * The effective state changed. Payload: `from`, `to`, `reason`, `reported`, and `incompatibility` when relevant.
  * The reported capability set changed (`reason=capabilities_changed`, with `added` and `removed`).
* `worker.offline`
  * The sweep fired (`reason=heartbeat_timeout` or `wake_timeout`; payload includes `silent_seconds`, `threshold_seconds`, `active_job`, `active_step`).
  * The worker sent a final `offline` heartbeat on graceful shutdown (`reason=worker_shutdown`).

The client emits no events. Its callers record their own actions (`command.run`, model events) with
the job and step context.

**State ownership.**

* A live worker *reports* `starting`, `ready`, `busy`, `draining`, `error` or `offline` (its final heartbeat).
* The orchestrator owns three things:
  * `offline`, set by the sweep.
  * `sleeping` and `waking`, set by the Wake-on-LAN controller through `set_state`.
  * The sticky operator drain: while drained, a `ready` or `busy` report is stored as `draining`.
* A protocol-version mismatch or a kind mismatch forces `error`, whatever the worker reports.

**Stale heartbeats.** A heartbeat whose `sent_at` is at or before the last accepted one, by at most 120 s,
is ignored (`accepted=false, stale=true`). This covers duplicate or reordered delivery. A larger backwards
jump is treated as a clock reset and accepted.

**Selection.** `select_worker` returns a worker that meets all of these conditions:

* It *reported* the capability.
* Its state is `ready`, or `busy` when that is allowed.
* Its heartbeat is fresher than `offline_after`.
* Its `protocol_version` equals `WORKER_PROTOCOL_VERSION`.
* It is compatible and not drained.
* It has an `api_url`, which means it is registered from `hosts.yaml`. A heartbeat-only registration is visible but never dispatched to.

Ordering: `ready` before `busy`, then workers without an active job, then the most recent heartbeat.
Selection reserves nothing; resource leases (P25) do that.

## 5. Configuration

Orchestrator:

* `config/hosts.yaml`: hosts with role `execution_worker` or `model_worker` become `workers` rows. The fields used are `id`, `address`, `worker_kind`, `worker_api` (default `http://<address>:8787`), `wake_on_lan.*` and `labels`.
* `config/capabilities.yaml`: capabilities whose `worker_kind` matches are stored as `declared_capabilities`. Waking a worker relies on these.
* `Settings.worker_token_refs`: `{worker_id: "cred:<name>" | "file:<path>" | "env:<VAR>"}`. Files are re-read every 30 s, which allows rotation without a restart.
* `RegistrySettings`: code defaults as shown above. The API and scheduler phase constructs it.

Daemon environment (`worker.common.settings.WorkerDaemonSettings.from_env`):

| Variable | Default | Meaning |
|---|---|---|
| `WORKER_ID` (required) | – | must equal the host `id` in `hosts.yaml` |
| `WORKER_KIND` | daemon's own kind | `execution` / `model` / `media`; a mismatch with the daemon refuses startup |
| `WORKER_TOKEN_FILE` | `$CREDENTIALS_DIRECTORY/worker-token`, else `/etc/hermclaw-worker/worker-token` | one token per line; the first line signs, later lines are still accepted (rotation) |
| `ORCHESTRATOR_URL` | unset (no heartbeats) | e.g. `http://192.168.178.225:8080`; no prefix-stripping proxy, because the signed path must match |
| `WORKER_BIND` | `127.0.0.1:8787` | listen address (`0.0.0.0:8787` on the LAN hosts) |
| `WORKER_HEARTBEAT_SECONDS` | 15 | initial interval (the ack overrides it, clamped to 1–600 s) |
| `WORKER_DATA_DIR` | `/var/lib/hermclaw-worker` | workspaces under `<dir>/workspaces` |
| `WORKER_CAPABILITIES` / `WORKER_CAPABILITIES_FILE` | – | reported capabilities. The kind's base capabilities are always added: execution `sandbox`, `workspace_sync`; model `ollama`, `gpu_telemetry` |
| `WORKER_MAX_SKEW_SECONDS` | 120 | allowed clock skew |
| `WORKER_MAX_BODY_MB` | 1024 | maximum upload size (streamed and hashed into a spooled temp file) |
| `WORKER_MAX_CONCURRENT_COMMANDS` | 2 | execution slots |
| `WORKER_POLICIES_FILE` | – | `policies.yaml`, whose `sandbox` section is used (`SandboxPolicy`) |
| `WORKER_CONTAINER_LABEL` | `hermclaw.managed=true` | label of the containers that recovery removes |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | model worker |
| `WORKER_NVIDIA_SMI` | `nvidia-smi` | GPU telemetry binary |
| `WORKER_LOG_LEVEL`, `WORKER_LOG_JSON`, `WORKER_HOSTNAME` | `INFO`, `true`, hostname | logging and identity |

## 6. Failure behaviour

| Failure | Behaviour |
|---|---|
| Daemon killed (no goodbye) | heartbeats stop. The sweep after `3 × interval` (45 s by default; worst case about 1.5 × that) sets `offline` and emits `worker.offline` with `active_job` and `active_step` |
| Graceful stop (SIGTERM) | the daemon drains, then sends a final `offline` heartbeat (best effort, 3 s), which produces `worker.offline(reason=worker_shutdown)` immediately |
| Orchestrator unreachable | the heartbeat sender backs off exponentially (capped at the interval) and keeps trying forever; `/health` shows `orchestrator_reachable=false` |
| Protocol or kind mismatch | the worker is set to `error` and an event is written; `select_worker` never returns it; the ack has `compatible=false` and the daemon logs an error. `ensure_compatible()` covers the orchestrator → daemon direction |
| Wrong, rotated-out or missing token | 401 with a specific code. A daemon without a readable, valid token refuses to start |
| Replay or skew | 401 `WORKER_AUTH_REPLAY` / `WORKER_AUTH_SKEW`. The replay cache is in-memory per process (window 240 s); after a restart, replays are still bounded by the 120 s skew |
| Two first heartbeats racing | `INSERT … ON CONFLICT DO NOTHING` plus a row lock: one creates the row, the other updates it (no 500) |
| Concurrent sweeps | `FOR UPDATE SKIP LOCKED`; a row locked by a heartbeat is handled on the next tick |
| Monitor DB error | logged, `last_error` is set, and the loop continues |
| Sandbox runner raises | `CommandResult(exit_code=None, error="sandbox error: …")` (redacted), HTTP 200 |
| Upload interrupted | `.incoming-*` and `.trash-*` leftovers are removed at startup, together with labelled containers |
| Ollama down | health is `degraded`, the heartbeat state is `error` (readiness), and load/unload return `503 OLLAMA_UNREACHABLE` |
| No GPU / `nvidia-smi` missing | `available=false` with a reason; heartbeats continue |
| Client transport, timeout or 5xx | typed errors; GETs are retried (2×, exponential backoff), mutations are not |

Security notes:

* Daemons run commands only through the sandbox runner (rootless podman, network off by default; the policy lives in the sandbox component).
* All subprocesses (`nvidia-smi`, `podman ps`/`rm`) use exec argv. No shell is involved.
* No model output or reasoning is stored or returned by the daemons.

## 7. Operating on the real hosts (`.222`, `.224`)

> **Status: implemented; live verification is blocked by BLOCKER-001.** The LAN `192.168.178.0/24` cannot be
> reached from the build environment, and the live tests are marked `@pytest.mark.live`.

1. Generate one token per worker on `.225`:
   `python -c "from hermclaw.workers.auth import generate_worker_token as g; print(g())"`.
   * Store it as `/etc/hermclaw/secrets/worker-exec-222` (mode 0400) and set `worker_token_refs: {exec-222: "cred:worker-exec-222"}`.
   * Copy it to the worker as a systemd credential: `LoadCredential=worker-token:/etc/hermclaw-worker/worker-token`.
2. Install a systemd unit on each worker. Sketch:

   ```ini
   [Service]
   User=hermclaw-worker
   Environment=WORKER_ID=exec-222 WORKER_BIND=0.0.0.0:8787 ORCHESTRATOR_URL=http://192.168.178.225:8080
   Environment=WORKER_POLICIES_FILE=/etc/hermclaw-worker/policies.yaml WORKER_CAPABILITIES_FILE=/etc/hermclaw-worker/capabilities.yaml
   LoadCredential=worker-token:/etc/hermclaw-worker/worker-token
   ExecStart=/opt/hermclaw/.venv/bin/python -m worker.execution     # .224: -m worker.model (+ OLLAMA_URL)
   KillSignal=SIGTERM
   TimeoutStopSec=40
   ```

   Keep the clocks in sync (chrony or systemd-timesyncd); skew above 120 s rejects every request.
3. Check a host:
   * `curl http://192.168.178.222:8787/health` should return `status ok`.
   * `GET /api/workers` on the orchestrator should list the worker as `ready` with a fresh `heartbeat_age_seconds`.
   * `ExecutionWorkerClient.selftest()` / `ModelWorkerClient.selftest(SelftestRequest(model="qwen3:8b"))`.
4. Rotate a token:
   1. Prepend the new token to the worker's token file. The daemon then signs with it, and the orchestrator still accepts the old one if that is listed second in its file.
   2. Update the orchestrator file with the new token first.
   3. Remove the old token from both files after at least 30 s, the token-file TTL.
5. Live checks still to run on the target hosts:
   * a real heartbeat from `.222` and `.224`;
   * `nvidia-smi` on the GTX 1080 (driver branch 580);
   * Ollama load and unload with `num_ctx`;
   * podman container recovery;
   * offline detection after `systemctl kill -s KILL`.

## 8. Tests

```bash
.venv/bin/pytest -q tests/unit/test_workers_*.py tests/integration/test_workers_*.py tests/failure/test_workers_*.py
.venv/bin/ruff check hermclaw/workers worker/common worker/execution/app.py worker/execution/__main__.py worker/model
.venv/bin/mypy hermclaw/workers worker/common worker/execution/app.py worker/execution/__main__.py worker/model
```

* `tests/unit/test_workers_auth.py`: sign and verify; tampering with method, path, query, body, timestamp or nonce; skew (inside and outside 120 s); replay; cache not poisoned; token rotation; token files and refs; bearer only over https.
* `tests/unit/test_workers_client.py`: signing, GET-only retries, error mapping, timeouts, protocol errors, `ensure_compatible`, refusal of unsafe ids.
* `tests/unit/test_workers_daemon_common.py`: env settings, `/proc` parsers, `nvidia-smi` CSV and a fake executable, daemon state, middleware (exemption, body limits, multi-chunk replay, websocket refusal).
* `tests/integration/test_workers_registry.py` (PostgreSQL 16): config registration, heartbeat ingest (row, capabilities, health, events), stale heartbeats, auto-registration including the concurrent race, selection skips workers without an `api_url`, protocol and kind mismatch, offline sweep, wake timeout, graceful-offline event, selection, monitor loop.
* `tests/integration/test_workers_api.py`: signed heartbeat end to end through the router, every auth failure code, header check before the body, incompatible ack, drain, the real `HeartbeatSender` against the router.
* `tests/integration/test_workers_execution_daemon.py`: the real `ExecutionWorkerClient` against the in-process app. Covers workspaces, path safety, rejection of command workspace ids that could escape the root, idempotency, redaction, busy and draining, RWLock regressions and container recovery. Real podman is used where it is available.
* `tests/integration/test_workers_model_daemon.py`: a real aiohttp fake Ollama server. Covers load, unload, exclusive load, timeouts, selftest, degraded health and the media extension point.
* `tests/failure/test_workers_failures.py`: real daemon processes (uvicorn subprocess) heartbeating into the router. SIGTERM must produce a final offline heartbeat and SIGKILL must lead to sweep detection. Also covers back-off and a daemon without a credential.
