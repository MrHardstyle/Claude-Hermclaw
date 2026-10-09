"""Execution sandbox of the execution worker on ``.222`` (P18, Bauplan §28, research 20261008-005).

Every command of a step runs in its own throw-away container::

    podman run --rm --name hermclaw-<request> --label hermclaw.managed=true --label hermclaw.request=<id>
        --label hermclaw.job=<job> --label hermclaw.step=<step> --network=none --read-only
        --tmpfs /tmp:rw,size=<tmpfs> --userns=keep-id --cap-drop=ALL --security-opt=no-new-privileges
        --pids-limit <n> --memory <m> --memory-swap <m> --cpus <c> -v <workspace>:/workspace:Z -w /workspace
        --env NAME=value ... --entrypoint=sh <image> -lc <command>

* **18.1 rootless Podman** – :class:`PodmanSandbox`; ``--userns=keep-id`` maps the worker's uid into the
  container so the workspace bind mount stays writable without root. ``require_rootless`` (default on in
  ``production``) refuses to run when podman itself runs as root. :func:`podman_info` reports whether
  resource limits are effective (cgroups v2 with delegated ``cpu``/``memory``/``pids`` controllers).
* **18.2 images** – only the policy's ``image``/``images`` (aliases such as ``python``) or an explicit extra
  allowlist may be used; :meth:`ContainerSandbox.ensure_images` pre-pulls them.
* **18.3 mounts** – exactly one bind mount (the workspace at ``/workspace``), read-only root filesystem and a
  size-limited ``/tmp`` tmpfs.
* **18.4 resource limits** – ``--cpus``, ``--memory`` (= ``--memory-swap``, no swap), ``--pids-limit``;
  requests may lower but never raise the policy limits.
* **18.5 timeouts** – the command is killed (``kill -s KILL`` + ``rm -f``) when it exceeds its timeout;
  the result has ``timed_out=True``.
* **18.6/18.7 network** – ``--network=none`` unless the request carries the network capability (or the
  policy's ``network_default`` is ``allowed``); then ``slirp4netns``/``pasta`` (podman) or ``bridge`` (docker).
* **18.8 cleanup** – ``--rm``; on timeout, cancellation and engine errors the container is killed and
  removed explicitly. Leftovers of crashed runs are found by label (:mod:`worker.execution.recovery`).

Environment: ``policy.env_allowlist`` lists the names (exact or ``fnmatch`` glob) a request may set; nothing
from the worker's own environment is forwarded into a container (podman additionally gets
``--http-proxy=false`` so host proxy variables, which may embed credentials, never leak in).

Output of both streams is captured up to ``policies.commands.max_output_bytes`` each (head and tail are
kept, the middle is replaced by a marker, ``*_truncated`` is set) and passed through the redactor. Neither
the command text nor its output nor env values are logged.

:class:`DockerSandbox` is the supported Docker adapter with equivalent flags; :class:`LocalSandbox` runs
without isolation and is only allowed when ``HERMCLAW_ENV`` is ``development`` or ``test``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

import yaml

from hermclaw.contracts.worker import CommandRequest, CommandResult
from hermclaw.core.config import CommandPolicy, SandboxPolicy
from hermclaw.core.errors import ConfigError, PolicyViolation
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED, Redactor
from worker.common.settings import DEFAULT_CONTAINER_LABEL

log = get_logger(__name__)

EngineName = Literal["podman", "docker", "local"]
PullPolicy = Literal["always", "missing", "never", "newer"]

WORKDIR = "/workspace"
CONTAINER_PREFIX = "hermclaw-"
LABEL_REQUEST = "hermclaw.request"
LABEL_JOB = "hermclaw.job"
LABEL_STEP = "hermclaw.step"
DEFAULT_SHELL: tuple[str, ...] = ("sh", "-lc")
#: podman/docker exit code when the engine itself (not the command) failed
ENGINE_ERROR_EXIT = 125
#: exit code of a SIGKILLed process (timeout kill, OOM kill)
KILLED_EXIT = 137
MIN_OUTPUT_BYTES = 1024
#: truncation cut points move to a line boundary at most this far away
LINE_ALIGN_MAX_BYTES = 4096
#: shortest fragment of a known secret that is masked at a truncation boundary
MIN_SECRET_FRAGMENT = 3
MIN_MEMORY_BYTES = 6 * 1024 * 1024  # podman/docker refuse smaller memory limits
MAX_ENV_VALUE_CHARS = 32_768
#: environment every container starts with (read-only rootfs: ``/tmp`` is the only writable scratch dir)
BASE_CONTAINER_ENV: Mapping[str, str] = {"HOME": "/tmp", "TMPDIR": "/tmp"}  # noqa: S108 - paths inside the container

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SIZE_RE = re.compile(r"^(\d{1,15})([bkmg]?)$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}$")
_NAME_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_NAME_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.-]")
_LABEL_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.:@/+=-]")
_SENSITIVE_ENV_RE = re.compile(r"(?i)(pass(word|wd)?|secret|token|api[_-]?key|credential|auth|private[_-]?key|dsn)")
_FORBIDDEN_MOUNT_CHARS = frozenset(":,\n\r\x00")
_SIZE_FACTORS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_LIMIT_CONTROLLERS = frozenset({"cpu", "memory", "pids"})
_ENGINE_ERROR_RE = re.compile(r"^(?:Error:|Error response from daemon:|docker: |podman: )")
_NAME_CONFLICT_RE = re.compile(r"(?i)already in use|name .* is in use|conflict")


# ---------------------------------------------------------------------------------------------- protocol
@runtime_checkable
class SandboxRunner(Protocol):
    """Runs one :class:`CommandRequest` with ``workspace_dir`` as working directory."""

    async def run(self, req: CommandRequest, workspace_dir: Path) -> CommandResult: ...


# ---------------------------------------------------------------------------------------------- helpers
def parse_size(value: str) -> int:
    """``"512m"`` -> bytes. Accepts an integer with optional ``b``/``k``/``m``/``g`` suffix (case-insensitive)."""
    m = _SIZE_RE.match(value.strip().lower())
    if m is None:
        raise ValueError(f"invalid size {value!r} (expected e.g. 512m, 2g, 1048576)")
    return int(m.group(1)) * _SIZE_FACTORS[m.group(2)]


def _normalize_size(value: str) -> str:
    parse_size(value)
    return value.strip().lower()


def container_name_for(request_id: str) -> str:
    """Deterministic, engine-valid container name ``hermclaw-<request_id>``.

    Request ids that are not valid container names (or are too long) are sanitized and suffixed with a short
    hash of the original id, so distinct ids never map to the same name."""
    if _NAME_SAFE_RE.match(request_id):
        return CONTAINER_PREFIX + request_id
    digest = hashlib.sha256(request_id.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    cleaned = _NAME_UNSAFE_CHARS_RE.sub("-", request_id)[:48].strip("-.") or "req"
    return f"{CONTAINER_PREFIX}{cleaned}-{digest}"


def label_value(value: str) -> str:
    """A label value restricted to a safe character set and 128 characters."""
    return _LABEL_UNSAFE_CHARS_RE.sub("_", value)[:128]


def allowed_images(policy: SandboxPolicy, extra: Iterable[str] = ()) -> frozenset[str]:
    return frozenset({policy.image, *policy.images.values(), *extra})


def resolve_image(policy: SandboxPolicy, requested: str | None, extra: Iterable[str] = ()) -> str:
    """The image to run: the policy default, an alias of ``policy.images`` or an explicitly allowed image."""
    if not requested:
        image = policy.image
    elif requested in policy.images:
        image = policy.images[requested]
    elif requested in allowed_images(policy, extra):
        image = requested
    else:
        raise PolicyViolation(
            f"image {requested!r} is not allowed (configure it in policies.sandbox.images)",
            code="SANDBOX_IMAGE_NOT_ALLOWED",
            details={"image": requested},
        )
    if not _IMAGE_RE.match(image):
        raise PolicyViolation(f"invalid image reference {image!r}", code="SANDBOX_IMAGE_INVALID", details={"image": image})
    return image


def env_name_allowed(name: str, allowlist: Sequence[str]) -> bool:
    return any(name == pattern or fnmatch.fnmatchcase(name, pattern) for pattern in allowlist)


def validate_request_env(env: Mapping[str, str], allowlist: Sequence[str]) -> dict[str, str]:
    """Validate the names/values a request wants to set; raises :class:`PolicyViolation`."""
    out: dict[str, str] = {}
    for name, value in env.items():
        if not _ENV_NAME_RE.match(name):
            raise PolicyViolation(f"invalid environment variable name {name[:64]!r}", code="SANDBOX_ENV_INVALID")
        if not env_name_allowed(name, allowlist):
            raise PolicyViolation(
                f"environment variable {name!r} is not in policies.sandbox.env_allowlist",
                code="SANDBOX_ENV_NOT_ALLOWED",
                details={"name": name},
            )
        if "\x00" in value or len(value) > MAX_ENV_VALUE_CHARS:
            raise PolicyViolation(f"invalid value for environment variable {name!r}", code="SANDBOX_ENV_INVALID")
        out[name] = value
    return out


def secret_env_values(env: Mapping[str, str]) -> list[str]:
    """Values of variables whose *name* marks them as secret; they are masked in the captured output."""
    return [v for k, v in env.items() if _SENSITIVE_ENV_RE.search(k) and len(v) >= 4]


def validate_workspace_dir(workspace_dir: Path) -> Path:
    """The workspace must be an existing, non-symlinked absolute directory other than ``/``."""
    if any(ch in _FORBIDDEN_MOUNT_CHARS for ch in str(workspace_dir)):
        raise PolicyViolation("workspace path contains characters that cannot be mounted", code="SANDBOX_WORKSPACE_INVALID")
    if workspace_dir.is_symlink():
        raise PolicyViolation("workspace path must not be a symlink", code="SANDBOX_WORKSPACE_INVALID")
    resolved = workspace_dir.resolve()
    if not resolved.is_dir():
        raise PolicyViolation(f"workspace directory does not exist: {workspace_dir}", code="SANDBOX_WORKSPACE_MISSING")
    if resolved == Path(resolved.anchor):
        raise PolicyViolation("the filesystem root cannot be a workspace", code="SANDBOX_WORKSPACE_INVALID")
    return resolved


def _violation_result(req: CommandRequest, engine: EngineName, exc: PolicyViolation, name: str | None = None) -> CommandResult:
    log.warning(
        "sandbox request rejected",
        extra={"request_id": req.request_id, "job_id": req.job_id, "step_id": req.step_id, "code": exc.code, "reason": exc.message},
    )
    return CommandResult(request_id=req.request_id, exit_code=None, sandbox=engine, container_name=name, error=f"{exc.code}: {exc.message}")


def engine_error_line(stderr: str) -> str | None:
    """The engine's own error message in ``stderr`` (``Error: …`` / ``docker: …``), else ``None``.

    Exit code 125 is ambiguous (the command itself may ``exit 125``); only a message written by the engine
    makes it an engine error."""
    for line in reversed(stderr.strip().splitlines()):
        if _ENGINE_ERROR_RE.match(line.strip()):
            return line.strip()
    return None


def _command_digest(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", "surrogatepass")).hexdigest()[:16]


class OutputCapture:
    """Bounded capture of a byte stream: keeps the first and the last ``limit // 2`` bytes.

    When the stream is truncated, both cut points are moved to the nearest line boundary (if one lies within
    a quarter of the kept part, max. :data:`LINE_ALIGN_MAX_BYTES`), so ``KEY=value`` lines are kept or
    dropped as a whole and the redactor always sees complete secrets. :meth:`render` additionally masks
    fragments of known secrets that a cut inside an overlong line would leave at the boundary."""

    def __init__(self, limit: int) -> None:
        self.limit = max(MIN_OUTPUT_BYTES, limit)
        self._tail_limit = self.limit // 2
        self._head_limit = self.limit - self._tail_limit
        self._head = bytearray()
        self._tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self._head_limit - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self._tail += chunk
            if len(self._tail) > 2 * self._tail_limit:  # amortised trimming
                del self._tail[: len(self._tail) - self._tail_limit]

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def segments(self) -> tuple[bytes, int, bytes]:
        """``(head, omitted_bytes, tail)``; ``tail`` is empty and ``omitted`` 0 when nothing was dropped."""
        if not self.truncated:
            return bytes(self._head) + bytes(self._tail), 0, b""
        head = bytes(self._head)
        tail = bytes(self._tail[-self._tail_limit :])
        window = min(LINE_ALIGN_MAX_BYTES, self._head_limit // 4)
        cut = head.rfind(b"\n")
        if cut >= 0 and len(head) - (cut + 1) <= window:
            head = head[: cut + 1]
        window = min(LINE_ALIGN_MAX_BYTES, self._tail_limit // 4)
        start = tail.find(b"\n")
        if 0 <= start < window:
            tail = tail[start + 1 :]
        return head, self.total - len(head) - len(tail), tail

    def text(self) -> str:
        """The captured text (not redacted)."""
        return self.render(lambda s: s)

    def render(self, clean: Callable[[str], str], secrets: Sequence[str] = ()) -> str:
        """The captured text with ``clean`` (the redactor) applied to each kept part separately."""
        head, omitted, tail = self.segments()
        if not omitted:
            return clean(head.decode("utf-8", "replace"))
        head_text = _mask_cut_fragments(clean(head.decode("utf-8", "replace")), secrets, at_end=True)
        tail_text = _mask_cut_fragments(clean(tail.decode("utf-8", "replace")), secrets, at_end=False)
        sep_head = "" if head_text.endswith("\n") else "\n"
        sep_tail = "" if not tail_text or tail_text.startswith("\n") else "\n"
        return f"{head_text}{sep_head}...[{omitted} bytes omitted]...{sep_tail}{tail_text}"


def _mask_cut_fragments(text: str, secrets: Sequence[str], *, at_end: bool) -> str:
    """Mask a prefix (``at_end``: the text ends with it) or suffix of a known secret cut by truncation."""
    for secret in sorted(secrets, key=len, reverse=True):
        for k in range(len(secret) - 1, MIN_SECRET_FRAGMENT - 1, -1):
            if at_end and text.endswith(secret[:k]):
                return text[: len(text) - k] + REDACTED
            if not at_end and text.startswith(secret[-k:]):
                return REDACTED + text[k:]
    return text


async def _pump(stream: asyncio.StreamReader | None, capture: OutputCapture) -> None:
    if stream is None:
        return
    while chunk := await stream.read(65536):
        capture.feed(chunk)


async def engine_exec(argv: Sequence[str], *, timeout_seconds: float = 60.0, max_bytes: int = 4_000_000) -> tuple[int, str, str]:
    """Run an engine helper command (``kill``, ``rm``, ``inspect``, ``info``, ``pull`` …) with a timeout.

    Returns ``(returncode, stdout, stderr)``; a missing executable yields 127 and a timeout -9."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except (FileNotFoundError, PermissionError) as exc:
        return 127, "", f"{argv[0]}: {exc.strerror or exc}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_seconds)
    except BaseException as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.shield(proc.wait())
        if isinstance(exc, TimeoutError):
            return -9, "", f"{argv[0]} {argv[1] if len(argv) > 1 else ''} timed out after {timeout_seconds:g}s"
        raise
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, out[:max_bytes].decode("utf-8", "replace"), err[:max_bytes].decode("utf-8", "replace")


@dataclass
class _ProcOutcome:
    returncode: int | None
    timed_out: bool
    stdout: OutputCapture
    stderr: OutputCapture
    duration_ms: int


async def _supervise(
    proc: asyncio.subprocess.Process,
    *,
    timeout_seconds: float,
    terminate: Callable[[], Awaitable[None]],
    limit: int,
    reader_grace: float,
    started: float,
    after_exit: Callable[[], Awaitable[None]] | None = None,
) -> _ProcOutcome:
    """Capture both streams, enforce ``timeout`` and always leave no process behind.

    ``terminate`` stops whatever the process started (container, process group); it runs on timeout and on
    cancellation (shielded), after which the process itself is killed if it is still alive. ``after_exit``
    runs once the process has exited, before the remaining output is drained (kills stragglers that would
    otherwise keep the pipes open)."""
    out_cap, err_cap = OutputCapture(limit), OutputCapture(limit)
    readers = [asyncio.create_task(_pump(proc.stdout, out_cap)), asyncio.create_task(_pump(proc.stderr, err_cap))]
    timed_out = False

    async def stop() -> None:
        with contextlib.suppress(Exception):
            await terminate()
        try:
            await asyncio.wait_for(proc.wait(), reader_grace)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout_seconds)
        except TimeoutError:
            timed_out = True
            await stop()
        if after_exit is not None:
            await after_exit()
        _done, pending = await asyncio.wait(readers, timeout=reader_grace)
        for task in pending:  # a grandchild still holds the pipe: give up on the rest of the output
            task.cancel()
    except asyncio.CancelledError:
        await asyncio.shield(stop())
        raise
    finally:
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
    return _ProcOutcome(
        returncode=None if timed_out else proc.returncode,
        timed_out=timed_out,
        stdout=out_cap,
        stderr=err_cap,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _finish_result(
    req: CommandRequest,
    engine: EngineName,
    outcome: _ProcOutcome,
    *,
    redactor: Redactor,
    secrets: Sequence[str],
    timeout: int,
    container_name: str | None,
    memory: str | None = None,
) -> CommandResult:
    run_redactor = Redactor(secrets)

    def clean(text: str) -> str:
        return run_redactor.text(redactor.text(text))

    stderr = outcome.stderr.render(clean, secrets)
    error: str | None = None
    engine_line = engine_error_line(stderr) if outcome.returncode == ENGINE_ERROR_EXIT and engine != "local" else None
    if outcome.timed_out:
        error = f"timeout: command exceeded {timeout}s and was killed"
    elif engine_line is not None:
        error = f"sandbox engine error: {engine_line[:500]}"
    elif outcome.returncode == KILLED_EXIT and memory:
        error = f"killed (exit {KILLED_EXIT}): possibly out of memory (limit {memory})"
    return CommandResult(
        request_id=req.request_id,
        exit_code=outcome.returncode,
        timed_out=outcome.timed_out,
        stdout=outcome.stdout.render(clean, secrets),
        stderr=stderr,
        stdout_truncated=outcome.stdout.truncated,
        stderr_truncated=outcome.stderr.truncated,
        duration_ms=outcome.duration_ms,
        sandbox=engine,
        container_name=container_name,
        error=error,
    )


def default_max_output_bytes(environ: Mapping[str, str] | None = None) -> int:
    """``policies.commands.max_output_bytes`` of ``WORKER_POLICIES_FILE`` (the file the worker's sandbox
    policy comes from), else the :class:`CommandPolicy` default."""
    env = os.environ if environ is None else environ
    raw = env.get("WORKER_POLICIES_FILE", "").strip()
    if not raw:
        return CommandPolicy().max_output_bytes
    path = Path(raw)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path} must contain a mapping")
        return CommandPolicy.model_validate(data.get("commands") or {}).max_output_bytes
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigError(f"cannot read policies.commands from {path}: {exc}") from exc


def _current_environment() -> str:
    from hermclaw.core.settings import get_settings

    return get_settings().env


# ---------------------------------------------------------------------------------------------- engine info
@dataclass
class EngineInfo:
    """Health facts of a container engine (``podman info`` / ``docker info``)."""

    engine: EngineName
    available: bool
    version: str | None = None
    rootless: bool | None = None
    cgroup_version: str | None = None
    cgroup_manager: str | None = None
    cgroup_controllers: list[str] = field(default_factory=list)
    network_backend: str | None = None
    limits_effective: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "available": self.available,
            "version": self.version,
            "rootless": self.rootless,
            "cgroup_version": self.cgroup_version,
            "cgroup_manager": self.cgroup_manager,
            "cgroup_controllers": list(self.cgroup_controllers),
            "network_backend": self.network_backend,
            "limits_effective": self.limits_effective,
            "warnings": list(self.warnings),
            "error": self.error,
        }


def _limits_assessment(info: EngineInfo) -> None:
    """Resource limits only work with cgroups v2 + delegated controllers (rootless) or rootful cgroups v1."""
    controllers = set(info.cgroup_controllers)
    if info.cgroup_version == "v2":
        missing = sorted(_LIMIT_CONTROLLERS - controllers)
        info.limits_effective = not missing
        if missing:
            info.warnings.append(
                f"cgroups v2 controllers not delegated: {', '.join(missing)} (enable systemd Delegate= for the worker user)"
            )
    elif info.cgroup_version == "v1":
        info.limits_effective = info.rootless is False and controllers >= _LIMIT_CONTROLLERS
        if info.rootless:
            info.warnings.append("cgroups v1: resource limits are not enforced for rootless containers (switch the host to cgroups v2)")
    else:
        info.warnings.append("cgroup version unknown: resource limits may not be enforced")
    if info.rootless is False:
        info.warnings.append("container engine runs as root (Bauplan §28 requires rootless)")


def parse_podman_info(data: Mapping[str, Any]) -> EngineInfo:
    host = data.get("host") or {}
    security = host.get("security") or {}
    info = EngineInfo(
        engine="podman",
        available=True,
        version=(data.get("version") or {}).get("Version"),
        rootless=security.get("rootless"),
        cgroup_version=host.get("cgroupVersion"),
        cgroup_manager=host.get("cgroupManager"),
        cgroup_controllers=[str(c) for c in host.get("cgroupControllers") or []],
        network_backend=host.get("networkBackend"),
    )
    _limits_assessment(info)
    return info


def parse_docker_info(data: Mapping[str, Any]) -> EngineInfo:
    security_options = [str(o) for o in data.get("SecurityOptions") or []]
    cgroup = data.get("CgroupVersion")
    info = EngineInfo(
        engine="docker",
        available=True,
        version=data.get("ServerVersion"),
        rootless=any("rootless" in o for o in security_options),
        cgroup_version=f"v{cgroup}" if cgroup and not str(cgroup).startswith("v") else cgroup,
        cgroup_manager=data.get("CgroupDriver"),
        # docker info does not list controllers; it reports the effective limit support instead
        cgroup_controllers=[
            name for name, key in (("memory", "MemoryLimit"), ("cpu", "CpuCfsQuota"), ("pids", "PidsLimit")) if data.get(key)
        ],
        network_backend="docker",
    )
    _limits_assessment(info)
    return info


async def podman_info(executable: str = "podman") -> EngineInfo:
    """``podman info`` health: version, rootless, cgroup version/controllers, whether limits are effective."""
    return await engine_info("podman", executable)


async def engine_info(engine: Literal["podman", "docker"], executable: str | None = None) -> EngineInfo:
    exe = executable or engine
    argv = [exe, "info", "--format", "json"] if engine == "podman" else [exe, "info", "--format", "{{json .}}"]
    rc, out, err = await engine_exec(argv, timeout_seconds=60)
    if rc != 0:
        return EngineInfo(engine=engine, available=False, error=(err.strip() or out.strip() or f"exit {rc}")[:500])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return EngineInfo(engine=engine, available=False, error=f"unparseable {engine} info output: {exc}")
    return parse_podman_info(data) if engine == "podman" else parse_docker_info(data)


# ---------------------------------------------------------------------------------------------- containers
@dataclass(frozen=True)
class SandboxInvocation:
    """Everything decided for one request before the engine is started (also used by tests and logs)."""

    argv: list[str]
    container_name: str
    image: str
    network: bool
    timeout_seconds: int
    cpus: str
    memory: str
    env_names: tuple[str, ...]
    secrets: tuple[str, ...] = ()


class ContainerSandbox:
    """Shared implementation of the podman and docker adapters (one container per command)."""

    engine: ClassVar[Literal["podman", "docker"]] = "podman"
    default_allowed_network: ClassVar[str] = "slirp4netns"

    def __init__(
        self,
        policy: SandboxPolicy,
        *,
        executable: str | None = None,
        max_output_bytes: int | None = None,
        redactor: Redactor | None = None,
        managed_label: str = DEFAULT_CONTAINER_LABEL,
        extra_images: Iterable[str] = (),
        allowed_network: str | None = None,
        pull: PullPolicy = "missing",
        kill_grace_seconds: float = 10.0,
        require_rootless: bool = False,
        selinux_relabel: bool = True,
        shell: Sequence[str] = DEFAULT_SHELL,
    ) -> None:
        if not shell:
            raise ConfigError("sandbox shell must not be empty")
        key, sep, value = managed_label.partition("=")
        if not key or _LABEL_UNSAFE_CHARS_RE.search(managed_label) or (sep and not value):
            raise ConfigError(f"invalid managed label {managed_label!r} (expected key or key=value)")
        for size_name, size in (("memory", policy.memory), ("tmpfs_size", policy.tmpfs_size)):
            try:
                parse_size(size)
            except ValueError as exc:
                raise ConfigError(f"policies.sandbox.{size_name}: {exc}") from exc
        if policy.cpus <= 0 or policy.pids_limit <= 0 or policy.default_timeout_seconds <= 0:
            raise ConfigError("policies.sandbox cpus, pids_limit and default_timeout_seconds must be positive")
        self.policy = policy
        self.executable = executable or self.engine
        self.max_output_bytes = max_output_bytes if max_output_bytes is not None else default_max_output_bytes()
        self.redactor = redactor or DEFAULT_REDACTOR
        self.managed_label = managed_label
        self.extra_images = frozenset(extra_images)
        self.allowed_network = allowed_network
        self.pull = pull
        self.kill_grace_seconds = kill_grace_seconds
        self.require_rootless = require_rootless
        self.selinux_relabel = selinux_relabel
        self.shell = tuple(shell)
        self._active: dict[str, str] = {}  # container name -> request id

    # ------------------------------------------------------------------ introspection
    def active_containers(self) -> dict[str, str]:
        """Containers of commands currently running in this process (``name -> request_id``)."""
        return dict(self._active)

    def allowed_images(self) -> frozenset[str]:
        return allowed_images(self.policy, self.extra_images)

    async def health(self) -> EngineInfo:
        return await engine_info(self.engine, self.executable)

    # ------------------------------------------------------------------ command line
    def network_enabled(self, req: CommandRequest) -> bool:
        return req.network or self.policy.network_default == "allowed"

    def effective_timeout(self, req: CommandRequest) -> int:
        """The request's timeout when it set one explicitly, else ``policies.sandbox.default_timeout_seconds``."""
        return req.timeout_seconds if "timeout_seconds" in req.model_fields_set else self.policy.default_timeout_seconds

    def resolve_limits(self, req: CommandRequest) -> tuple[str, str]:
        """``(cpus, memory)`` – requests may lower but never raise the policy limits."""
        memory = _normalize_size(self.policy.memory)
        if req.memory:
            try:
                wanted = parse_size(req.memory)
            except ValueError as exc:
                raise PolicyViolation(str(exc), code="SANDBOX_LIMIT_INVALID") from exc
            if wanted > parse_size(self.policy.memory):
                raise PolicyViolation(f"memory {req.memory} exceeds the sandbox limit {self.policy.memory}", code="SANDBOX_LIMIT_EXCEEDED")
            if wanted < MIN_MEMORY_BYTES:
                raise PolicyViolation(f"memory {req.memory} is below the engine minimum of 6m", code="SANDBOX_LIMIT_INVALID")
            memory = _normalize_size(req.memory)
        cpus = self.policy.cpus
        if req.cpus is not None:
            if not 0 < req.cpus <= self.policy.cpus:
                raise PolicyViolation(
                    f"cpus {req.cpus:g} must be > 0 and <= the sandbox limit {self.policy.cpus:g}", code="SANDBOX_LIMIT_EXCEEDED"
                )
            cpus = req.cpus
        return f"{cpus:g}", memory

    def _network_args(self, enabled: bool) -> list[str]:
        if not enabled:
            return ["--network=none"]
        return [f"--network={self.allowed_network or self.default_allowed_network}"]

    def _engine_args(self, workspace: Path) -> list[str]:
        """Engine specific isolation flags and the workspace mount."""
        raise NotImplementedError

    def build_invocation(self, req: CommandRequest, workspace_dir: Path) -> SandboxInvocation:
        """Validate the request against the policy and build the engine command line (no I/O but stat)."""
        if self.require_rootless and os.geteuid() == 0:
            raise PolicyViolation("the sandbox engine must run rootless (worker runs as root)", code="SANDBOX_NOT_ROOTLESS")
        if "\x00" in req.command:
            raise PolicyViolation("command contains a NUL byte", code="SANDBOX_COMMAND_INVALID")
        workspace = validate_workspace_dir(workspace_dir)
        image = resolve_image(self.policy, req.image, self.extra_images)
        env = dict(BASE_CONTAINER_ENV)
        env.update(validate_request_env(req.env, self.policy.env_allowlist))
        cpus, memory = self.resolve_limits(req)
        network = self.network_enabled(req)
        name = container_name_for(req.request_id)
        tmpfs = _normalize_size(self.policy.tmpfs_size)
        argv = [
            self.executable,
            "run",
            "--rm",
            f"--name={name}",
            f"--label={self.managed_label}",
            f"--label={LABEL_REQUEST}={label_value(req.request_id)}",
            f"--label={LABEL_JOB}={label_value(req.job_id)}",
            f"--label={LABEL_STEP}={label_value(req.step_id)}",
            *self._network_args(network),
            "--read-only",
            f"--tmpfs=/tmp:rw,nosuid,nodev,size={tmpfs},mode=1777",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--pids-limit={self.policy.pids_limit}",
            f"--memory={memory}",
            f"--memory-swap={memory}",
            f"--cpus={cpus}",
            f"--pull={self.pull}",
            *self._engine_args(workspace),
            f"--workdir={WORKDIR}",
            *(f"--env={k}={v}" for k, v in env.items()),
            f"--entrypoint={self.shell[0]}",
            image,
            *self.shell[1:],
            req.command,
        ]
        return SandboxInvocation(
            argv=argv,
            container_name=name,
            image=image,
            network=network,
            timeout_seconds=self.effective_timeout(req),
            cpus=cpus,
            memory=memory,
            env_names=tuple(sorted(env)),
            secrets=tuple(secret_env_values(env)),
        )

    # ------------------------------------------------------------------ container lifecycle
    def _rm_argv(self, name: str) -> list[str]:
        return [self.executable, "rm", "-f", name]

    @staticmethod
    def _is_name_conflict(outcome: _ProcOutcome) -> bool:
        if outcome.returncode != ENGINE_ERROR_EXIT:
            return False
        line = engine_error_line(outcome.stderr.text())
        return line is not None and _NAME_CONFLICT_RE.search(line) is not None

    async def container_exists(self, name: str) -> bool:
        rc, _out, _err = await engine_exec([self.executable, "container", "inspect", "--format", "{{.Id}}", name], timeout_seconds=30)
        return rc == 0

    async def container_labels(self, name: str) -> dict[str, str] | None:
        """Labels of container ``name`` or ``None`` when it does not exist."""
        rc, out, _err = await engine_exec(
            [self.executable, "container", "inspect", "--format", "{{json .Config.Labels}}", name], timeout_seconds=30
        )
        if rc != 0:
            return None
        try:
            labels = json.loads(out.strip() or "null")
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}

    async def _is_managed_leftover(self, name: str) -> bool:
        """A container with our deterministic name that carries the managed label (never a foreign one)."""
        labels = await self.container_labels(name)
        if labels is None:
            return False
        key, _sep, value = self.managed_label.partition("=")
        return key in labels and (not value or labels[key] == value)

    async def kill_container(self, name: str) -> bool:
        rc, _out, _err = await engine_exec([self.executable, "kill", "-s", "KILL", name], timeout_seconds=30)
        return rc == 0

    async def remove_container(self, name: str) -> bool:
        """``rm -f`` (idempotent: a container that is already gone counts as removed)."""
        rc, _out, err = await engine_exec(self._rm_argv(name), timeout_seconds=60)
        return rc == 0 or "no such container" in err.lower()

    async def cleanup(self, name: str) -> bool:
        """Kill and remove one container (18.8); safe to call for containers that no longer exist."""
        await self.kill_container(name)
        return await self.remove_container(name)

    async def ensure_images(self, images: Iterable[str] | None = None, *, pull_timeout: float = 1800.0) -> dict[str, str]:
        """Make sure the allowed images are present locally (18.2): ``image -> present|pulled|error: …``."""
        report: dict[str, str] = {}
        for image in sorted(set(images) if images is not None else self.allowed_images()):
            if not _IMAGE_RE.match(image):
                report[image] = "error: invalid image reference"
                continue
            rc, _out, _err = await engine_exec([self.executable, "image", "inspect", "--format", "{{.Id}}", image], timeout_seconds=60)
            if rc == 0:
                report[image] = "present"
                continue
            if self.pull == "never":
                report[image] = "error: missing and pull policy is never"
                continue
            rc, _out, err = await engine_exec([self.executable, "pull", image], timeout_seconds=pull_timeout)
            report[image] = "pulled" if rc == 0 else f"error: {self.redactor.text(err.strip())[-300:] or f'exit {rc}'}"
        return report

    # ------------------------------------------------------------------ run
    async def run(self, req: CommandRequest, workspace_dir: Path) -> CommandResult:
        try:
            inv = self.build_invocation(req, workspace_dir)
        except PolicyViolation as exc:
            return _violation_result(req, self.engine, exc)
        name = inv.container_name
        if name in self._active:
            return _violation_result(
                req, self.engine, PolicyViolation("a command with this request_id is already running", code="SANDBOX_REQUEST_ACTIVE"), name
            )
        self._active[name] = req.request_id
        log.info(
            "sandbox command starting",
            extra={
                "request_id": req.request_id,
                "job_id": req.job_id,
                "step_id": req.step_id,
                "engine": self.engine,
                "container": name,
                "image": inv.image,
                "network": inv.network,
                "timeout_seconds": inv.timeout_seconds,
                "command_sha256": _command_digest(req.command),
                "env_names": list(inv.env_names),
            },
        )
        try:
            outcome = await self._run_once(inv)
            if self._is_name_conflict(outcome) and await self._is_managed_leftover(name):
                # a leftover container of a crashed earlier attempt blocks the deterministic name
                log.warning("removing stale sandbox container", extra={"container": name, "request_id": req.request_id})
                await self.cleanup(name)
                outcome = await self._run_once(inv)
        except asyncio.CancelledError:
            await asyncio.shield(self.cleanup(name))
            raise
        finally:
            self._active.pop(name, None)
        if outcome.timed_out or outcome.returncode is None:
            await self.cleanup(name)
        result = _finish_result(
            req,
            self.engine,
            outcome,
            redactor=self.redactor,
            secrets=inv.secrets,
            timeout=inv.timeout_seconds,
            container_name=name,
            memory=inv.memory,
        )
        log.info(
            "sandbox command finished",
            extra={
                "request_id": req.request_id,
                "container": name,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_ms": result.duration_ms,
                "stdout_bytes": outcome.stdout.total,
                "stderr_bytes": outcome.stderr.total,
            },
        )
        return result

    async def _run_once(self, inv: SandboxInvocation) -> _ProcOutcome:
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *inv.argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except (FileNotFoundError, PermissionError) as exc:
            err = OutputCapture(MIN_OUTPUT_BYTES)
            err.feed(f"Error: cannot execute {self.executable}: {exc.strerror or exc}".encode())
            return _ProcOutcome(ENGINE_ERROR_EXIT, False, OutputCapture(MIN_OUTPUT_BYTES), err, 0)

        async def terminate() -> None:
            await self.kill_container(inv.container_name)

        return await _supervise(
            proc,
            timeout_seconds=inv.timeout_seconds,
            terminate=terminate,
            limit=self.max_output_bytes,
            reader_grace=self.kill_grace_seconds,
            started=started,
        )


class PodmanSandbox(ContainerSandbox):
    """Rootless podman (preferred engine, Bauplan §5/§28)."""

    engine: ClassVar[Literal["podman", "docker"]] = "podman"

    def _network_args(self, enabled: bool) -> list[str]:
        if not enabled:
            return ["--network=none"]
        mode = self.allowed_network or self._auto_network()
        return [f"--network={mode}"]

    @staticmethod
    def _auto_network() -> str:
        # podman 5 uses pasta for rootless networking; pasta is refused for rootful podman
        if os.geteuid() != 0 and shutil.which("pasta"):
            return "pasta"
        return "slirp4netns"

    def _engine_args(self, workspace: Path) -> list[str]:
        mount = f"{workspace}:{WORKDIR}:Z" if self.selinux_relabel else f"{workspace}:{WORKDIR}"
        return [
            "--userns=keep-id",
            "--http-proxy=false",  # podman forwards the host's proxy variables (with credentials) by default
            "--log-driver=none",  # output is streamed to us; never persisted in container logs
            f"--volume={mount}",
        ]

    def _rm_argv(self, name: str) -> list[str]:
        return [self.executable, "rm", "-f", "-i", "-t", "0", name]


class DockerSandbox(ContainerSandbox):
    """Docker adapter (supported, Bauplan §5) with the same isolation flags."""

    engine: ClassVar[Literal["podman", "docker"]] = "docker"
    default_allowed_network: ClassVar[str] = "bridge"

    def _engine_args(self, workspace: Path) -> list[str]:
        st = workspace.stat()  # run as the owner of the workspace so the bind mount stays writable without root
        mount = f"type=bind,source={workspace},target={WORKDIR}"
        return [f"--user={st.st_uid}:{st.st_gid}", f"--mount={mount}"]


# ---------------------------------------------------------------------------------------------- local
class LocalSandbox:
    """Runs commands directly on the host – **no isolation**; development/test only."""

    engine: ClassVar[EngineName] = "local"

    def __init__(
        self,
        policy: SandboxPolicy,
        *,
        environment: str | None = None,
        max_output_bytes: int | None = None,
        redactor: Redactor | None = None,
        shell: Sequence[str] = DEFAULT_SHELL,
        kill_grace_seconds: float = 5.0,
    ) -> None:
        env_name = environment or _current_environment()
        if env_name not in ("development", "test"):
            raise PolicyViolation(
                f"the local (non-isolated) sandbox is not allowed in environment {env_name!r}", code="SANDBOX_LOCAL_FORBIDDEN"
            )
        if not shell:
            raise ConfigError("sandbox shell must not be empty")
        self.policy = policy
        self.max_output_bytes = max_output_bytes if max_output_bytes is not None else default_max_output_bytes()
        self.redactor = redactor or DEFAULT_REDACTOR
        self.shell = tuple(shell)
        self.kill_grace_seconds = kill_grace_seconds
        log.warning("local sandbox active: commands run WITHOUT isolation", extra={"environment": env_name})

    def effective_timeout(self, req: CommandRequest) -> int:
        return req.timeout_seconds if "timeout_seconds" in req.model_fields_set else self.policy.default_timeout_seconds

    def build_env(self, req: CommandRequest) -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}
        env.update(BASE_CONTAINER_ENV)
        env["TMPDIR"] = os.environ.get("TMPDIR", "/tmp")  # noqa: S108 - host temp dir
        env["HOME"] = env["TMPDIR"]
        env.update(validate_request_env(req.env, self.policy.env_allowlist))
        return env

    async def run(self, req: CommandRequest, workspace_dir: Path) -> CommandResult:
        try:
            if "\x00" in req.command:
                raise PolicyViolation("command contains a NUL byte", code="SANDBOX_COMMAND_INVALID")
            workspace = validate_workspace_dir(workspace_dir)
            env = self.build_env(req)
        except PolicyViolation as exc:
            return _violation_result(req, "local", exc)
        timeout = self.effective_timeout(req)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.shell,
                req.command,
                cwd=workspace,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group: timeout/cleanup kill everything it spawned
            )
        except (FileNotFoundError, PermissionError) as exc:
            return CommandResult(
                request_id=req.request_id,
                exit_code=None,
                sandbox="local",
                error=f"cannot execute {self.shell[0]}: {exc.strerror or exc}",
            )

        async def terminate() -> None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

        try:
            outcome = await _supervise(
                proc,
                timeout_seconds=timeout,
                terminate=terminate,
                limit=self.max_output_bytes,
                reader_grace=self.kill_grace_seconds,
                started=started,
                after_exit=terminate,
            )
        finally:
            await terminate()  # background children of a finished command (18.8)
        return _finish_result(
            req, "local", outcome, redactor=self.redactor, secrets=secret_env_values(env), timeout=timeout, container_name=None
        )


# ---------------------------------------------------------------------------------------------- factory
def make_sandbox(
    policy: SandboxPolicy,
    *,
    environment: str | None = None,
    max_output_bytes: int | None = None,
    redactor: Redactor | None = None,
    **engine_options: Any,
) -> PodmanSandbox | DockerSandbox | LocalSandbox:
    """The sandbox for ``policy.engine``.

    ``environment`` defaults to ``HERMCLAW_ENV``; in ``production`` podman must run rootless and the local
    engine is refused. ``engine_options`` are passed to :class:`ContainerSandbox` (``executable``,
    ``managed_label``, ``extra_images``, ``allowed_network``, ``pull``, ``require_rootless`` …)."""
    env_name = environment or _current_environment()
    if policy.engine == "local":
        if engine_options:
            raise ConfigError(f"options not supported by the local sandbox: {', '.join(sorted(engine_options))}")
        return LocalSandbox(policy, environment=env_name, max_output_bytes=max_output_bytes, redactor=redactor)
    if policy.engine == "podman":
        engine_options.setdefault("require_rootless", env_name == "production")
        return PodmanSandbox(policy, max_output_bytes=max_output_bytes, redactor=redactor, **engine_options)
    return DockerSandbox(policy, max_output_bytes=max_output_bytes, redactor=redactor, **engine_options)
