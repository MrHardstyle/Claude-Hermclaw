# Execution Sandbox (P18)

Bauplan §5 (Podman rootless preferred, Docker supported adapter), §28 (Execution Sandbox `.222`),
§34 (Sandbox: rootless, no host root, network off by default), Phase 18 (steps 18.1–18.9).
Research: R-005 rootless Podman (`docs/research/20261008-005-podman-rootless.md`).
Related: worker daemon `docs/architecture/workers.md` (P07, `worker/execution/app.py`), tool executors
`docs/architecture/tools.md` (`hermclaw/tools/executors.py`).

## 1. Purpose

Every command of a coder or verifier step runs on the execution worker `.222` in its **own throw-away
container**. The container gets:

* the step's workspace bind-mounted at `/workspace`, and nothing else from the host;
* a read-only root filesystem, plus size-limited `/tmp` and `/dev/shm` tmpfs mounts;
* hard CPU, memory (no swap) and PID limits, with all capabilities dropped and `no-new-privileges`;
* a timeout, after which the container is killed and removed;
* **no network** unless the step was granted the network capability;
* only allowlisted environment variables.

The sandbox has no planning authority. It runs exactly the `CommandRequest` it is given. Git mutations
stay with the runtime: `.git` never travels to the sandbox or back (see §5).

## 2. Code map

| Path | Role |
|---|---|
| `worker/execution/sandbox.py` | `SandboxRunner` protocol, `PodmanSandbox`, `DockerSandbox`, `LocalSandbox`, `make_sandbox`, engine health (`podman_info`), output capture/redaction |
| `worker/execution/workspaces.py` | `extract_tar_safely`, `build_tar`, `manifest`, `diff_manifest` (workspace sync between `.225` and `.222`) |
| `worker/execution/recovery.py` | 18.8/18.9: list managed containers, remove abandoned ones, prune stale workspaces, periodic maintenance loop |
| `tests/unit/test_sandbox_command_line.py` | exact engine argv, policy checks, helpers (no engine needed) |
| `tests/unit/test_sandbox_local.py` | `LocalSandbox` against real subprocesses (timeouts, process-group cleanup, env, redaction) |
| `tests/unit/test_sandbox_workspaces.py` | tar attacks (traversal, symlinks, hardlinks, devices, limits), round-trips, manifest diffs |
| `tests/unit/test_sandbox_recovery.py` | time/inspect parsing, recovery flow against a scripted fake engine CLI (test-only), workspace pruning |
| `tests/integration/test_sandbox_podman.py` | the real podman engine: isolation, network, limits, timeouts, env, redaction, pulls, docker flag set |
| `tests/integration/test_sandbox_recovery.py` | the real podman engine: a crashed worker's leftover containers are found and removed |

## 3. Public interfaces

```python
# worker/execution/sandbox.py
class SandboxRunner(Protocol):
    async def run(self, req: CommandRequest, workspace_dir: Path) -> CommandResult: ...

def make_sandbox(policy: SandboxPolicy, *, environment: str | None = None, max_output_bytes: int | None = None,
                 redactor: Redactor | None = None, **engine_options: Any) -> PodmanSandbox | DockerSandbox | LocalSandbox

class ContainerSandbox:                       # shared by PodmanSandbox / DockerSandbox
    def __init__(self, policy: SandboxPolicy, *, executable: str | None = None, max_output_bytes: int | None = None,
                 redactor: Redactor | None = None, managed_label: str | None = None, extra_images: Iterable[str] = (),
                 allowed_network: str | None = None, pull: Literal["always", "missing", "never", "newer"] = "missing",
                 kill_grace_seconds: float = 10.0, require_rootless: bool = False, selinux_relabel: bool = True,
                 shell: Sequence[str] = ("sh", "-lc"), shm_size: str = "64m") -> None
    async def run(self, req: CommandRequest, workspace_dir: Path) -> CommandResult
    def build_invocation(self, req: CommandRequest, workspace_dir: Path) -> SandboxInvocation   # validation + argv, no side effects
    async def preflight(self) -> EngineInfo         # startup check: engine reachable, rootless when required
    async def health(self) -> EngineInfo
    async def ensure_images(self, images: Iterable[str] | None = None, *, pull_timeout: float = 1800.0) -> dict[str, str]
    async def cleanup(self, name: str) -> bool      # kill + rm -f, idempotent
    def active_containers(self) -> dict[str, str]   # container name -> request_id of commands running now

class LocalSandbox:                           # NO isolation; only HERMCLAW_ENV in (development, test)
    def __init__(self, policy: SandboxPolicy, *, environment: str | None = None, max_output_bytes: int | None = None,
                 redactor: Redactor | None = None, shell: Sequence[str] = ("sh", "-c"), kill_grace_seconds: float = 5.0)

async def podman_info(executable: str = "podman") -> EngineInfo     # version, rootless, cgroup version/controllers, limits_effective, warnings
async def engine_info(engine: Literal["podman", "docker"], executable: str | None = None) -> EngineInfo
def resolve_image(policy: SandboxPolicy, requested: str | None, extra: Iterable[str] = ()) -> str
def validate_request_env(env: Mapping[str, str], allowlist: Sequence[str]) -> dict[str, str]
def container_name_for(request_id: str) -> str                      # "hermclaw-<request_id>" (sanitised + hash when needed)
def default_managed_label(environ: Mapping[str, str] | None = None) -> str   # WORKER_CONTAINER_LABEL or "hermclaw.managed=true"
def default_max_output_bytes(environ: Mapping[str, str] | None = None) -> int # policies.commands.max_output_bytes of WORKER_POLICIES_FILE

# worker/execution/workspaces.py
def extract_tar_safely(data: bytes, dest: Path, *, max_members: int = 200_000, max_total_bytes: int = 4 GiB,
                       max_file_bytes: int = 1 GiB) -> None          # raises UnsafeArchiveError / WorkspaceLimitExceeded
def build_tar(src: Path, paths: list[str] | None = None, *, exclude: Iterable[str] = (".git",),
              max_total_bytes: int = 4 GiB, compress: bool = False) -> bytes
def manifest(src: Path, *, exclude: Iterable[str] = (".git",)) -> dict[str, str]   # path -> sha256
def diff_manifest(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str]]   # (changed, deleted)

# worker/execution/recovery.py
async def recover_abandoned(*, engine: Literal["podman", "docker"] = "podman", executable: str | None = None,
                            label: str | None = None, older_than_seconds: float = 60.0,
                            tracked: Collection[str] | Callable[[], Collection[str]] = (),   # evaluated after listing
                            dry_run: bool = False, now: datetime | None = None) -> RecoveryReport
async def recover_for_sandbox(sandbox: ContainerSandbox, *, older_than_seconds: float = 60.0, dry_run: bool = False) -> RecoveryReport
async def list_managed_containers(executable: str, *, label: str | None = None) -> list[ContainerRecord]
async def remove_containers(executable: str, names: Iterable[str], *, engine: ... = "podman") -> tuple[list[str], list[str]]
def prune_stale_workspaces(root: Path, *, older_than_seconds: float, keep: Collection[str] = (), now: float | None = None,
                           dry_run: bool = False) -> list[str]
async def run_periodic_recovery(sandbox: ContainerSandbox, *, interval_seconds: float, stop: asyncio.Event,
                                older_than_seconds: float = 60.0, workspaces_root: Path | None = None,
                                workspace_max_age_seconds: float | None = None,
                                busy_workspaces: Callable[[], Collection[str]] | None = None) -> None
```

Policy violations never raise from `run()`. They return
`CommandResult(exit_code=None, error="<CODE>: <message>")`, and no process is started. Codes:
`SANDBOX_IMAGE_NOT_ALLOWED`, `SANDBOX_IMAGE_INVALID`, `SANDBOX_ENV_NOT_ALLOWED`, `SANDBOX_ENV_INVALID`,
`SANDBOX_LIMIT_EXCEEDED`, `SANDBOX_LIMIT_INVALID`, `SANDBOX_WORKSPACE_MISSING`, `SANDBOX_WORKSPACE_INVALID`,
`SANDBOX_COMMAND_INVALID`, `SANDBOX_NOT_ROOTLESS`, `SANDBOX_REQUEST_ACTIVE`.

Construction errors are raised:

* `ConfigError` for an invalid policy, label or shell;
* `PolicyViolation` `SANDBOX_LOCAL_FORBIDDEN` when `LocalSandbox` is used outside development or test.

## 4. The container command line (podman)

```
podman run --rm --name=hermclaw-<request_id>
  --label=hermclaw.managed=true --label=hermclaw.request=<id> --label=hermclaw.job=<job> --label=hermclaw.step=<step>
  --network=none                                   # 18.6; with the network capability: pasta|slirp4netns (18.7)
  --read-only --tmpfs=/tmp:rw,nosuid,nodev,size=<policy.tmpfs_size>,mode=1777        # 18.3
  --cap-drop=ALL --security-opt=no-new-privileges
  --pids-limit=<n> --memory=<m> --memory-swap=<m> --cpus=<c>                         # 18.4
  --pull=missing
  --userns=keep-id                                  # 18.1: worker uid inside, bind mount writable without root
  --read-only-tmpfs=false --tmpfs=/dev/shm:rw,nosuid,nodev,noexec,size=64m,mode=1777
  --http-proxy=false --log-driver=none
  --volume=<workspace>:/workspace:Z --workdir=/workspace
  --env=HOME=/tmp --env=TMPDIR=/tmp --env=<NAME> ...
  --entrypoint=sh <image> -lc <command>
```

* **Images (18.2).** A request can only use one of these: `policy.image`, a value or alias of
  `policy.images` (for example `image: "node"`), or an explicit `extra_images` entry. References are checked
  against a strict regex, so a leading `-` is impossible. `ensure_images()` pre-pulls them through the
  host's registry/proxy configuration.
* **Limits (18.4).** A request may lower `cpus`/`memory` but never raise them above the policy. The minimums
  are 0.01 CPUs and 6 MiB of memory. `--memory-swap` equals `--memory`, so there is no swap.
* **Writable paths.** Only `/workspace`, `/tmp` and `/dev/shm` are writable. Podman's implicit `/run` and
  `/var/tmp` tmpfs mounts are switched off, which gives the same result as docker's `--read-only`.
* **Environment.** The container starts with `HOME=/tmp` and `TMPDIR=/tmp`, plus the request's variables.
  Each request name must match `policy.env_allowlist` (an exact name or an `fnmatch` glob) and the pattern
  `[A-Za-z_][A-Za-z0-9_]*`. Values may not contain NUL and are limited to 32 KiB.
  * **Values stay off the command line.** `/proc/<pid>/cmdline` is world-readable, so values are handed to
    the engine process through its environment and forwarded by name (`--env=NAME`).
  * **Exception: names the engine binary reads itself.** Names such as `HOME`, `PATH`, `XDG_*`, `LD_*` and
    `CONTAINERS_*` would change how podman itself behaves. They are passed as `--env=NAME=value` instead. If
    such a name also looks secret (`DOCKER_AUTH_CONFIG`, `REGISTRY_AUTH_FILE`, …), it is refused.
  * **Nothing from the worker's own environment reaches the container.** `--http-proxy=false` also stops
    podman from injecting the host's proxy variables.
* **Shell.** The command is a single argv element of `sh -lc`, so the worker never interpolates it into a
  shell. A login shell sources the image's `/etc/profile`. On Debian images that resets `PATH` to the
  standard directories. For images that depend on a custom `PATH`, pass `shell=("sh", "-c")`.
* **Docker adapter.** `DockerSandbox` uses the same flags, except that it runs as
  `--user=<workspace uid>:<gid>` (instead of `--userns=keep-id`). It also uses `--shm-size` and
  `--mount=type=bind,...`, and `bridge` networking when network is allowed. Docker has no
  `--http-proxy=false`: do not configure `proxies` in the worker user's `~/.docker/config.json`.

## 5. Data and event flow

1. The orchestrator `.225` uploads the workspace snapshot as a tar (`PUT /v1/workspaces/{ws}`).
   `extract_tar_safely` validates the **whole** archive before writing anything. It rejects:
   * absolute paths and `..` segments;
   * symlinks whose target is absolute or escapes;
   * hardlinks to anything other than an earlier regular file;
   * devices, FIFOs and sockets;
   * duplicates, and entries below a file or symlink;
   * entries that would replace an existing directory;
   * entries that would be written through an existing symlink;
   * archives over the size or count limits.

   Files are written through `O_NOFOLLOW` directory file descriptors. Symlinks are created last and checked
   again with `realpath`, which catches escaping chains such as `a -> .` and `b -> a/..`. setuid, setgid,
   sticky and group/other-write bits are stripped.
2. `POST /v1/commands` reaches `SandboxRunner.run(req, workspace_dir)`, which starts one container. Both
   output streams are captured up to `policies.commands.max_output_bytes` each. The head and the tail are
   kept, the cut is aligned to a line boundary and `*_truncated` is set. The redactor runs over each kept
   part, together with the values of secret-named request variables (and fragments of them that a cut
   leaves behind).
3. The orchestrator fetches `manifest()` (`path -> sha256`; a symlink hashes as `sha256("symlink\0"+target)`;
   `.git` is excluded), compares it with `diff_manifest(old, new)` and pulls the changed files with
   `build_tar(paths=changed)`. `build_tar` and `manifest` never follow symlinks (`os.fwalk`,
   `O_NOFOLLOW|O_NONBLOCK`), so a symlink a sandbox command planted to host files or FIFOs cannot exfiltrate
   or block anything.
4. **Events.** The sandbox runs on `.222` without database access. The observable facts (`exit_code`,
   `timed_out`, `duration_ms`, `container_name`, truncation flags, `error`) travel back in `CommandResult`.
   The orchestrator side records them as events: the tool executor (`hermclaw/tools/executors.py`) and the
   workers client.
5. **Logging.** Logs contain the request, job and step ids, the container name, the image, the network flag,
   the timeout, a sha256 digest of the command and the **names** of the environment variables. They never
   contain the command text, the output or any environment value.

## 6. Configuration

| Key | Used for |
|---|---|
| `policies.sandbox.engine` | `podman` (default) / `docker` / `local` |
| `policies.sandbox.image`, `policies.sandbox.images` | default image and allowed image aliases (18.2) |
| `policies.sandbox.cpus`, `memory`, `pids_limit`, `tmpfs_size` | resource limits (18.4) and the `/tmp` tmpfs (18.3) |
| `policies.sandbox.default_timeout_seconds` | timeout when the request does not set one explicitly (18.5) |
| `policies.sandbox.network_default` | `none` (default, 18.6) or `allowed` |
| `policies.sandbox.env_allowlist` | names and globs a request may set |
| `policies.commands.max_output_bytes` | per-stream capture limit (read from `WORKER_POLICIES_FILE` on the worker) |
| `WORKER_POLICIES_FILE` | `policies.yaml` on `.222` (its `sandbox` and `commands` sections) |
| `WORKER_CONTAINER_LABEL` | the managed label (default `hermclaw.managed=true`); sandbox and recovery both use it |
| `HERMCLAW_ENV` | `production` makes `make_sandbox` require rootless podman and refuse `local` |

## 7. Failure behaviour

| Situation | Result |
|---|---|
| **Timeout (18.5)** | `podman kill -s KILL <name>`, then the client gets `kill_grace_seconds` to exit (after that it is SIGKILLed), then `rm -f`. Result: `timed_out=True`, `exit_code=None`, `error="timeout: …"`. Output captured up to that point is kept |
| **Cancellation** of the calling task | container killed and removed (shielded), `CancelledError` re-raised |
| **Engine error** (exit 125 + an `Error:` line from the engine) | `error="sandbox engine error: …"`; a container that was created but not started (and carries our labels) is removed. A plain `exit 125` from the command itself is *not* an engine error |
| **Name conflict** with a leftover of the same request (managed label and `hermclaw.request` match) | leftover killed and removed, command retried once. A foreign container with the same name is never touched |
| **OOM kill** | `exit_code=137`, `error="killed (exit 137): possibly out of memory (limit …)"` |
| **Engine binary missing** | `exit_code=125`, `error="sandbox engine error: Error: cannot execute …"`; `preflight()` raises `ConfigError` `SANDBOX_ENGINE_UNAVAILABLE` |
| **Same `request_id` already running** in this process | `SANDBOX_REQUEST_ACTIVE` |
| **`LocalSandbox`** | command runs in its own session/process group. On timeout, cancellation **and** after a normal exit, the whole group is SIGKILLed, so background children cannot survive or keep the pipes open. Exit is detected without waiting for pipe EOF (`wait_exit`) |
| **Worker crash (18.9)** | the containers keep running under conmon. On the next start (or in the periodic loop), `recover_for_sandbox` lists containers by label. It removes everything that is not tracked by this process and is older than `older_than_seconds` (default 60 s; containers whose creation time cannot be parsed count as old). The report contains `scanned`, `removed`, `kept{name: reason}`, `errors` |
| **Listing fails** | `ExternalServiceError` `CONTAINER_LIST_FAILED`; the periodic loop logs it and keeps running |

`recover_for_sandbox` passes `sandbox.active_containers` as a callable that is evaluated *after* the engine
listing, so a command that starts while recovery runs is never mistaken for a leftover (the periodic loop can
run next to live commands).

Assumption: one execution daemon owns a label on a host. If two daemons share a label, each would see the
other's long-running commands as untracked. Give them distinct `WORKER_CONTAINER_LABEL` values.

## 8. Engine health and cgroups

`podman_info()` / `preflight()` read `podman info`:

* `version`, `rootless`;
* `cgroup_version`, `cgroup_manager`, `cgroup_controllers`;
* `network_backend`;
* `limits_effective` and `warnings`.

Resource limits only take effect in two setups:

* **cgroups v2** with the `cpu`, `memory` and `pids` controllers delegated to the worker user (systemd `Delegate=`);
* **rootful cgroups v1**.

Rootless podman on cgroups v1 silently ignores them, and the health check reports that as a warning.

Build-host result (this environment):

```
podman 4.9.3, rootless=false (runs as root), cgroups v1 / cgroupfs,
controllers [cpuset cpu cpuacct blkio memory devices freezer pids], netavark
limits_effective=true, warnings=["container engine runs as root (Bauplan §28 requires rootless)"]
```

So the limit tests (`pids`, `memory`) really run here. Rootless semantics are emulated with
`--userns=keep-id`, and with `require_rootless=True` the `preflight()` correctly refuses this rootful host.

## 9. Operating on `.222` (BLOCKER-001: live verification pending)

1. **Install podman and networking.** Install podman ≥ 4.4 together with `pasta` (passt) or `slirp4netns`.
   Create a dedicated worker user with subuid/subgid ranges (`usermod --add-subuids 100000-165535
   --add-subgids 100000-165535 hermclaw`), then run `loginctl enable-linger hermclaw`.
2. **Use cgroups v2 with delegation.** Configure
   `/etc/systemd/system/user@.service.d/delegate.conf` with `[Service] Delegate=cpu cpuset io memory pids`.
   Then `podman info --format json` must show `cgroupVersion: v2` and list the `cpu`, `memory` and `pids`
   controllers.
3. **Prepare the policies.** Write `policies.yaml` with the `sandbox` and `commands` sections, set
   `WORKER_POLICIES_FILE` to it and set `HERMCLAW_ENV=production`. `make_sandbox` then requires rootless
   (the euid check per command, plus `preflight()` at startup).
4. **Pre-pull the images**, either with `ensure_images()` or with `podman pull <policy.image>` for each
   entry of `policy.images`. With `--pull=missing` no pull happens during a step once the images are present.
5. **Verify on the host.** As the worker user, run
   `.venv/bin/pytest -m "integration or live" tests/integration/test_sandbox_podman.py tests/integration/test_sandbox_recovery.py`.
   The `live` test checks four things: rootless, cgroups v2, enforced memory/pids limits, and a working
   allowed-network mode. It also checks that files written in the workspace belong to the worker uid.
6. **Wire up recovery and cleanup.** Run `recover_for_sandbox(sandbox, older_than_seconds=0)` at daemon
   start, before accepting commands. Run `run_periodic_recovery(...)` with `workspaces_root` and
   `workspace_max_age_seconds` as a background task.

## 10. Tests

```
.venv/bin/pytest -q tests/unit/test_sandbox_command_line.py tests/unit/test_sandbox_local.py \
    tests/unit/test_sandbox_workspaces.py tests/unit/test_sandbox_recovery.py \
    tests/integration/test_sandbox_podman.py tests/integration/test_sandbox_recovery.py
```

The integration tests need podman and the local image `docker.io/library/alpine:3.20`. Without them they are
skipped with a clear reason. One test pulls `busybox:1.36.1` through the configured proxy and is skipped if
the registry is unreachable.

Each test uses its own label (`hermclaw.p18test=true`, or a random `hermclaw.p18rec=<hex>` for recovery),
so containers of other suites that share the engine are never touched.
