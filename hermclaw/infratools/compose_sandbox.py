"""``compose_up_sandbox`` – start a compose project in isolation, observe it, always tear it down (Bauplan §31, P28.3).

Flow (every step is bounded by the caller's timeout; teardown has its own budget and also runs on errors and
cancellation):

1. structural validation with the **strict** policy – privileged containers, host network/namespaces, engine
   sockets, sensitive or absolute host binds, external networks/volumes, ``include`` and cross-directory ``extends``
   are refused (the sandbox must never touch real host state);
2. a runtime engine is picked (Podman preferred, Docker as adapter); none usable → ``skipped`` with the reasons;
3. the project directory (without ``.git``) is **staged** as a copy – locally into a private temp dir, remotely
   as a tar stream into ``mktemp -d`` on the host – so containers writing into bind mounts never modify the job
   workspace;
4. a complete sandbox compose file is generated next to the staged compose file (see
   :func:`build_sandbox_model`): unique project name ``hermclaw-sbx-<id>``, every published port remapped to a free
   host port on ``127.0.0.1``, ``container_name`` dropped, ``restart: "no"``, label ``hermclaw.sandbox=<id>``.
   A *full* file instead of a merge override: Compose merges ``ports`` additively, and ``!reset`` is not supported
   by podman-compose 1.0.x – an override could not remove the original host ports;
5. ``up -d``, then polling via ``inspect`` until every expected container is running (and ``healthy`` when it has
   a healthcheck) for ``settle_seconds`` – or one exits non-zero / turns unhealthy / the timeout expires;
6. logs (tail) of every container are collected (redacted), published bindings are verified (only the allocated
   loopback ports), local ports are probed with a TCP connect;
7. teardown: ``down -v --remove-orphans`` plus a label sweep of containers/networks/volumes and removal of the
   staged copy; leftovers are reported.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import io
import os
import re
import secrets
import shutil
import socket
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.infratools.compose import (
    SANDBOX_LABEL,
    ComposeEngine,
    ComposeEngines,
    ComposePolicy,
    LoadedCompose,
    _port_range,
    _split_colons,
    load_compose,
    parse_port_entry,
    validate_structure,
)
from hermclaw.infratools.containers import ContainerInfo, ContainerInventory, Engine, parse_docker_ps, parse_podman_ps
from hermclaw.infratools.errors import (
    INFRA_COMPOSE_INVALID,
    INFRA_COMPOSE_REFUSED,
    INFRA_INVALID_ARGUMENT,
    INFRA_SANDBOX_FAILED,
    INFRA_SANDBOX_UNAVAILABLE,
    INFRA_TIMEOUT,
    Finding,
    InfraToolError,
    has_errors,
)
from hermclaw.infratools.ports import parse_ss
from hermclaw.infratools.process import LOCAL_HOST, HostExecutor, InfraContext

PROJECT_PREFIX = "hermclaw-sbx-"
SANDBOX_FILE = ".hermclaw-sandbox-{id}.compose.yaml"
REMOTE_PORT_RANGE = (20000, 32000)
REMOTE_STAGE_DIR = "/tmp"  # noqa: S108 – mktemp -d creates a private 0700 directory below it
MAX_SANDBOX_SECONDS = 3600.0
MAX_STAGE_BYTES = 200 * 1024 * 1024
STAGE_EXCLUDES = {".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
FORBIDDEN_TOP_LEVEL_IN_SANDBOX = ("include",)

SandboxStatus = Literal["passed", "failed", "refused", "skipped", "timeout"]


# ============================================================================================ reports
@dataclass
class ServiceState:
    service: str
    container: str
    state: str
    health: str | None = None
    exit_code: int | None = None
    restart_count: int | None = None
    logs: str = ""


@dataclass
class RemappedPort:
    service: str
    container_port: str
    protocol: str
    host_port: int
    original: str
    reachable: bool | None = None


@dataclass
class TeardownReport:
    ok: bool = True
    leftovers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0


@dataclass
class SandboxReport:
    status: SandboxStatus
    project: str
    host: str
    path: str
    engine: str | None = None
    reason: str | None = None
    code: str | None = None
    services: list[ServiceState] = field(default_factory=list)
    ports: list[RemappedPort] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    up_output: str = ""
    teardown: TeardownReport | None = None
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "passed" and (self.teardown is None or self.teardown.ok)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        data["findings"] = [f.to_dict() for f in self.findings]
        return data

    def render(self, limit: int = 12000) -> str:
        head = f"compose_up_sandbox {self.path}: {self.status.upper()} (project {self.project} on {self.host}"
        head += f", {self.engine})" if self.engine else ")"
        lines = [head]
        if self.reason:
            lines.append(f"reason: {self.reason}")
        for f in self.findings:
            if f.severity != "info":
                lines.append(f.render())
        for p in self.ports:
            reach = "" if p.reachable is None else (" reachable" if p.reachable else " not reachable")
            lines.append(f"port {p.service} {p.original} → 127.0.0.1:{p.host_port}->{p.container_port}/{p.protocol}{reach}")
        for s in self.services:
            extra = f", health {s.health}" if s.health else ""
            extra += f", exit {s.exit_code}" if s.exit_code is not None and s.state != "running" else ""
            lines.append(f"service {s.service} ({s.container}): {s.state}{extra}")
        lines.extend(f"note: {n}" for n in self.notes)
        if self.teardown is not None:
            td = self.teardown
            lines.append("teardown: " + ("complete" if td.ok else f"INCOMPLETE leftovers={td.leftovers} errors={td.errors}"))
        for s in self.services:
            if s.logs:
                lines.append(f"--- logs {s.service} ---\n{s.logs}")
        text = "\n".join(lines)
        return text if len(text) <= limit else text[: limit - 20] + "\n…[truncated]"


# ============================================================================================ model rewrite
@dataclass
class SandboxModel:
    model: dict[str, Any]
    ports: list[RemappedPort]
    notes: list[str]
    expected: dict[str, int]  # service -> expected container count (profiles excluded)


PortAllocator = Callable[[str], int]  # protocol -> free host port


def _labels_dict(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(k): "" if v is None else str(v) for k, v in value.items()}
    out: dict[str, str] = {}
    if isinstance(value, list):
        for item in value:
            key, _, val = str(item).partition("=")
            out[key] = val
    return out


def _merge(base: Any, override: Any, key: str = "") -> Any:
    """Minimal Compose merge for ``extends``: mappings deep-merge, port/volume-like lists concatenate."""
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        out = dict(base)
        for k, v in override.items():
            out[k] = _merge(base[k], v, str(k)) if k in base else copy.deepcopy(v)
        return out
    concat = {
        "ports",
        "expose",
        "volumes",
        "dns",
        "dns_search",
        "tmpfs",
        "devices",
        "cap_add",
        "cap_drop",
        "security_opt",
        "env_file",
        "external_links",
        "extra_hosts",
    }
    if key in concat and isinstance(base, list) and isinstance(override, list):
        return [*base, *[v for v in override if v not in base]]
    return copy.deepcopy(override)


def resolve_extends(loaded: LoadedCompose, *, depth: int = 0) -> dict[str, Any]:
    """Services with ``extends`` inlined (only files in the same directory – paths inside stay valid)."""
    if depth > 10:
        raise InfraToolError("extends nested more than 10 levels", code=INFRA_COMPOSE_REFUSED)
    services: dict[str, Any] = {}
    cache: dict[str, LoadedCompose] = {}

    def resolve(name: str, source: LoadedCompose, chain: tuple[str, ...]) -> dict[str, Any]:
        svc = source.services.get(name)
        if not isinstance(svc, Mapping):
            raise InfraToolError(f"extends unknown service '{name}'", code=INFRA_COMPOSE_INVALID)
        key = f"{source.path}:{name}"
        if key in chain:
            raise InfraToolError(f"extends cycle at service '{name}'", code=INFRA_COMPOSE_INVALID)
        ext = svc.get("extends")
        result = {k: copy.deepcopy(v) for k, v in svc.items() if k != "extends"}
        if ext is None:
            return result
        if isinstance(ext, str):
            ext = {"service": ext}
        if not isinstance(ext, Mapping) or not ext.get("service"):
            raise InfraToolError(f"service '{name}': invalid extends", code=INFRA_COMPOSE_INVALID)
        other = source
        if ext.get("file"):
            ext_path = (source.project_dir / str(ext["file"])).resolve()
            if ext_path.parent != loaded.project_dir.resolve():
                raise InfraToolError(
                    f"service '{name}' extends a file in another directory – not supported in the sandbox", code=INFRA_COMPOSE_REFUSED
                )
            other = cache.get(str(ext_path)) or load_compose(ext_path)
            cache[str(ext_path)] = other
        base = resolve(str(ext["service"]), other, (*chain, key))
        merged = _merge(base, result)
        assert isinstance(merged, dict)
        return merged

    for name in loaded.services:
        services[str(name)] = resolve(str(name), loaded, ())
    return services


def _rewrite_port(service: str, entry: Any, allocate: PortAllocator) -> tuple[list[str], list[RemappedPort]]:
    """Every published (or ephemeral) port → ``127.0.0.1:<free>:<container>/<proto>``."""
    if isinstance(entry, Mapping):
        target = str(entry.get("target", ""))
        proto = str(entry.get("protocol") or "tcp").lower()
        original = ", ".join(f"{k}={v}" for k, v in entry.items())
    else:
        parsed, problems = parse_port_entry(service, entry)
        if problems or not parsed:
            raise InfraToolError(f"service '{service}': cannot remap port {entry!r}: {'; '.join(problems)}", code=INFRA_COMPOSE_INVALID)
        text = str(entry).strip()
        proto = parsed[0].protocol
        body = text.rsplit("/", 1)[0] if "/" in text else text
        target = (_split_colons(body.rsplit("]", 1)[-1]) if body.startswith("[") else _split_colons(body))[-1]
        original = text
    rng = _port_range(target)
    targets = [str(p) for p in range(rng[0], rng[1] + 1)] if rng else [target]
    entries: list[str] = []
    mapped: list[RemappedPort] = []
    for t in targets:
        host_port = allocate(proto)
        entries.append(f"127.0.0.1:{host_port}:{t}/{proto}")
        mapped.append(RemappedPort(service, t, proto, host_port, original))
    return entries, mapped


def build_sandbox_model(loaded: LoadedCompose, sandbox_id: str, allocate: PortAllocator) -> SandboxModel:
    """The complete compose model the sandbox runs (see module docstring)."""
    project = PROJECT_PREFIX + sandbox_id
    for key in FORBIDDEN_TOP_LEVEL_IN_SANDBOX:
        if key in loaded.model:
            raise InfraToolError(f"top-level '{key}' is not supported in the sandbox", code=INFRA_COMPOSE_REFUSED)
    for top in ("networks", "volumes", "configs", "secrets"):
        entries = loaded.model.get(top)
        if isinstance(entries, Mapping):
            for name, spec in entries.items():
                if isinstance(spec, Mapping) and spec.get("external"):
                    raise InfraToolError(
                        f"external {top[:-1]} '{name}' would attach the sandbox to real host state – refused", code=INFRA_COMPOSE_REFUSED
                    )
    services = resolve_extends(loaded)
    notes: list[str] = []
    ports: list[RemappedPort] = []
    expected: dict[str, int] = {}
    for name, svc in services.items():
        if svc.get("network_mode") == "host":
            raise InfraToolError(f"service '{name}' uses the host network – refused", code=INFRA_COMPOSE_REFUSED)
        net_mode = svc.get("network_mode")
        if isinstance(net_mode, str) and net_mode.startswith("container:"):
            raise InfraToolError(
                f"service '{name}' joins container '{net_mode[10:]}' outside the sandbox – refused", code=INFRA_COMPOSE_REFUSED
            )
        if "container_name" in svc:
            notes.append(f"{name}: container_name '{svc.pop('container_name')}' dropped (unique sandbox names)")
        if svc.get("restart") not in (None, "no"):
            notes.append(f"{name}: restart '{svc.get('restart')}' replaced by 'no'")
        svc["restart"] = "no"
        new_ports: list[str] = []
        for entry in svc.get("ports") or []:
            rewritten, mapped = _rewrite_port(name, entry, allocate)
            new_ports.extend(rewritten)
            ports.extend(mapped)
        if new_ports:
            svc["ports"] = new_ports
        else:
            svc.pop("ports", None)
        labels = _labels_dict(svc.get("labels"))
        labels[SANDBOX_LABEL] = sandbox_id
        svc["labels"] = labels
        if not svc.get("profiles"):
            replicas = 1
            deploy = svc.get("deploy")
            if isinstance(deploy, Mapping) and isinstance(deploy.get("replicas"), int):
                replicas = int(deploy["replicas"])
            if isinstance(svc.get("scale"), int):
                replicas = int(svc["scale"])
            expected[name] = max(replicas, 0)
    model: dict[str, Any] = {"name": project, "services": services}
    for top in ("networks", "volumes", "configs", "secrets"):
        if top in loaded.model and loaded.model[top] is not None:
            model[top] = copy.deepcopy(loaded.model[top])
    for key, value in loaded.model.items():
        if isinstance(key, str) and key.startswith("x-"):
            model[key] = copy.deepcopy(value)
    return SandboxModel(model, ports, notes, expected)


# ============================================================================================ ports
class LocalPortAllocator:
    """Free loopback ports (bind to port 0); ports handed out stay reserved until :meth:`release`."""

    _reserved: set[tuple[str, int]] = set()  # noqa: RUF012 – process-wide on purpose (concurrent sandboxes)

    def __init__(self) -> None:
        self.mine: set[tuple[str, int]] = set()

    def __call__(self, protocol: str) -> int:
        kind = socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM
        for _ in range(50):
            with socket.socket(socket.AF_INET, kind) as sock:
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            key = (protocol, port)
            if key not in self._reserved:
                self._reserved.add(key)
                self.mine.add(key)
                return port
        raise InfraToolError("no free local port found", code=INFRA_SANDBOX_FAILED)

    def release(self) -> None:
        self._reserved.difference_update(self.mine)
        self.mine.clear()


class RemotePortAllocator:
    """Random ports in ``REMOTE_PORT_RANGE`` that are not listening on the remote host (from ``ss``)."""

    def __init__(self, used: set[int]) -> None:
        self.used = set(used)

    def __call__(self, protocol: str) -> int:
        lo, hi = REMOTE_PORT_RANGE
        span = hi - lo
        offset = secrets.randbelow(span)  # random start, then a linear scan: always finds a free port if one exists
        for i in range(span):
            port = lo + (offset + i) % span
            if port not in self.used:
                self.used.add(port)
                return port
        raise InfraToolError("no free port found on the remote host", code=INFRA_SANDBOX_FAILED)

    def release(self) -> None:
        return None


# ============================================================================================ staging
def _iter_stage_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in STAGE_EXCLUDES)
        base = Path(dirpath)
        files.extend(base / d for d in dirnames if (base / d).is_symlink())
        files.extend(base / f for f in sorted(filenames))
    return files


def stage_size(root: Path) -> int:
    total = 0
    for p in _iter_stage_files(root):
        with contextlib.suppress(OSError):
            if not p.is_symlink():
                total += p.stat().st_size
    return total


def _inside_project(root: Path, project_dir: Path, value: str) -> bool:
    resolved = (project_dir / value).resolve()
    return resolved == root or root in resolved.parents


def check_bind_sources(loaded: LoadedCompose, services: Mapping[str, Any]) -> None:
    """Sandbox rule: bind sources must be relative and stay inside the project directory (symlinks resolved)."""
    root = loaded.project_dir.resolve()
    for name, svc in services.items():
        for entry in svc.get("volumes") or []:
            if isinstance(entry, Mapping):
                vtype, source = str(entry.get("type") or "volume"), entry.get("source")
            else:
                parts = str(entry).split(":")
                source = parts[0] if len(parts) > 1 else None
                vtype = "bind" if source and source.startswith(("/", ".", "~", "$")) else "volume"
            if vtype != "bind" or not source:
                continue
            src = str(source)
            if src.startswith(("/", "~", "$")):
                raise InfraToolError(
                    f"service '{name}': bind mount '{src}' outside the project is not allowed in the sandbox", code=INFRA_COMPOSE_REFUSED
                )
            if not _inside_project(root, loaded.project_dir, src):
                raise InfraToolError(
                    f"service '{name}': bind source '{src}' resolves outside the project directory", code=INFRA_COMPOSE_REFUSED
                )


def _project_file(root: Path, project_dir: Path, value: str, what: str) -> None:
    if value.startswith(("/", "~", "$")) or not _inside_project(root, project_dir, value):
        raise InfraToolError(f"{what} '{value}' lies outside the project directory – refused in the sandbox", code=INFRA_COMPOSE_REFUSED)


def check_sandbox_rules(loaded: LoadedCompose, services: Mapping[str, Any]) -> None:
    """Further sandbox-only refusals (beyond the strict structural policy).

    Everything the engine would read from or attach to *outside* the staged project is refused: ``env_file``,
    build contexts/Dockerfiles, top-level ``configs``/``secrets`` files outside the project, ``volumes_from`` /
    ``external_links`` / ``*: container:<x>`` namespaces of foreign containers, host devices and any capability
    beyond the engine defaults that the strict policy only warns about.
    """
    root = loaded.project_dir.resolve()
    project_dir = loaded.project_dir
    for name, svc in services.items():
        where = f"service '{name}'"
        for item in _as_list_any(svc.get("env_file")):
            path = item.get("path") if isinstance(item, Mapping) else item
            if isinstance(path, str):
                _project_file(root, project_dir, path, f"{where}: env_file")
        build = svc.get("build")
        if build is not None:
            context = build if isinstance(build, str) else str(build.get("context") or ".") if isinstance(build, Mapping) else "."
            if re.match(r"^(https?|git|ssh)://|^git@", context):
                raise InfraToolError(f"{where}: remote build context '{context}' is not allowed in the sandbox", code=INFRA_COMPOSE_REFUSED)
            _project_file(root, project_dir, context, f"{where}: build context")
            if isinstance(build, Mapping) and isinstance(build.get("dockerfile"), str):
                _project_file(root, project_dir / context, str(build["dockerfile"]), f"{where}: dockerfile")
        if svc.get("devices"):
            raise InfraToolError(f"{where}: host devices are not allowed in the sandbox", code=INFRA_COMPOSE_REFUSED)
        if svc.get("cap_add"):
            raise InfraToolError(f"{where}: cap_add is not allowed in the sandbox", code=INFRA_COMPOSE_REFUSED)
        if svc.get("volumes_from"):
            foreign = [v for v in _as_list_any(svc.get("volumes_from")) if str(v).split(":", 1)[0] not in services]
            if foreign:
                raise InfraToolError(f"{where}: volumes_from {foreign} reaches outside the project", code=INFRA_COMPOSE_REFUSED)
        if svc.get("external_links"):
            raise InfraToolError(f"{where}: external_links reach containers outside the sandbox", code=INFRA_COMPOSE_REFUSED)
        for ns in ("pid", "ipc", "network_mode", "uts", "cgroup", "userns_mode"):
            value = svc.get(ns)
            if isinstance(value, str) and value.startswith("container:"):
                raise InfraToolError(f"{where}: {ns} '{value}' joins a container outside the sandbox", code=INFRA_COMPOSE_REFUSED)
            if value == "host":
                raise InfraToolError(f"{where}: {ns}: host is not allowed in the sandbox", code=INFRA_COMPOSE_REFUSED)
    for top in ("configs", "secrets"):
        entries = loaded.model.get(top)
        if not isinstance(entries, Mapping):
            continue
        for key, spec in entries.items():
            if not isinstance(spec, Mapping):
                continue
            if isinstance(spec.get("file"), str):
                _project_file(root, project_dir, str(spec["file"]), f"{top}.{key}: file")
            if spec.get("environment") is not None:
                raise InfraToolError(
                    f"{top}.{key}: values from the host environment are not allowed in the sandbox", code=INFRA_COMPOSE_REFUSED
                )


def _as_list_any(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _tar_project(root: Path, max_bytes: int) -> bytes:
    buf = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in _iter_stage_files(root):
            rel = path.relative_to(root).as_posix()
            info = tar.gettarinfo(str(path), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if info.isreg():
                total += info.size
                if total > max_bytes:
                    raise InfraToolError(f"project directory larger than {max_bytes // (1024 * 1024)} MiB", code=INFRA_COMPOSE_REFUSED)
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
            elif info.issym() or info.isdir():
                tar.addfile(info)
    return buf.getvalue()


def _stage_local(project_dir: Path, sandbox_id: str, file_name: str, text: str) -> tuple[str, str]:
    """Private copy of the project (``0700`` temp dir) plus the generated sandbox compose file; cleaned up on failure."""
    root = Path(tempfile.mkdtemp(prefix=PROJECT_PREFIX + sandbox_id + "-"))
    try:
        os.chmod(root, 0o700)
        dest = root / "project"
        shutil.copytree(project_dir, dest, symlinks=True, ignore=shutil.ignore_patterns(*STAGE_EXCLUDES))
        compose_path = dest / file_name
        compose_path.write_text(text, encoding="utf-8")
        compose_path.chmod(0o600)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return str(root), str(compose_path)


def _remote_stage_prefix(sandbox_id: str) -> str:
    return f"{REMOTE_STAGE_DIR}/{PROJECT_PREFIX}{sandbox_id}-"


# ============================================================================================ sandbox
@dataclass(frozen=True)
class SandboxOptions:
    settle_seconds: float = 2.0
    poll_interval: float = 1.0
    log_tail: int = 200
    log_chars: int = 4000
    teardown_timeout: float = 120.0
    stop_timeout: int = 5
    max_stage_bytes: int = MAX_STAGE_BYTES
    probe_ports: bool = True
    engine_preference: tuple[Engine, ...] = ("podman", "docker")


@dataclass
class _Run:
    """State of one sandbox run (shared by the steps and the teardown)."""

    sandbox_id: str
    host: str
    ctx: InfraContext
    engine: ComposeEngine | None = None
    compose_file: str | None = None
    stage_root: str | None = None

    @property
    def project(self) -> str:
        return PROJECT_PREFIX + self.sandbox_id

    @property
    def remote(self) -> bool:
        return self.host != LOCAL_HOST

    @property
    def cwd(self) -> Path | None:
        return Path(self.compose_file).parent if self.compose_file and not self.remote else None


class ComposeSandbox:
    """Runs ``compose_up_sandbox`` flows on the local machine or a registered host."""

    def __init__(
        self,
        executor: HostExecutor,
        engines: ComposeEngines,
        *,
        options: SandboxOptions | None = None,
        redactor: Redactor = DEFAULT_REDACTOR,
        max_parallel: int = 2,
    ) -> None:
        self.executor = executor
        self.engines = engines
        self.options = options or SandboxOptions()
        self.redactor = redactor
        self.inventory = ContainerInventory(executor, redactor=redactor)
        self._slots = asyncio.Semaphore(max_parallel)

    async def run(
        self,
        path: Path,
        *,
        timeout: float = 300.0,  # noqa: ASYNC109 – bounds the whole run; teardown has its own budget
        host: str | None = None,
        ctx: InfraContext | None = None,
    ) -> SandboxReport:
        if not 0 < timeout <= MAX_SANDBOX_SECONDS:
            raise InfraToolError(f"timeout must be within (0, {MAX_SANDBOX_SECONDS}] seconds", code=INFRA_INVALID_ARGUMENT)
        async with self._slots:
            return await self._run(path, budget=timeout, host=host, ctx=ctx or InfraContext())

    async def _run(self, path: Path, *, budget: float, host: str | None, ctx: InfraContext) -> SandboxReport:
        started = time.monotonic()
        deadline = started + budget
        run = _Run(uuid.uuid4().hex[:12], self.executor.check_host(host), ctx)
        report = SandboxReport("failed", run.project, run.host, str(path))

        def done(status: SandboxStatus, reason: str | None = None, code: str | None = None) -> SandboxReport:
            report.status = status
            report.reason = self.redactor.text(reason) if reason else None
            report.code = code
            report.duration_ms = int((time.monotonic() - started) * 1000)
            return report

        # 1. strict validation
        try:
            loaded = await asyncio.to_thread(load_compose, path)
        except InfraToolError as exc:
            return done("refused", exc.message, exc.code)
        report.findings = await asyncio.to_thread(validate_structure, loaded, ComposePolicy(allow_absolute_binds=False))
        if has_errors(report.findings):
            return done("refused", "the compose file violates the sandbox rules", INFRA_COMPOSE_REFUSED)
        try:
            services = resolve_extends(loaded)
            check_bind_sources(loaded, services)
            check_sandbox_rules(loaded, services)
        except InfraToolError as exc:
            return done("refused", exc.message, exc.code)

        # 2. engine
        engine, reasons = await self.engines.for_runtime(host, prefer=self.options.engine_preference, ctx=ctx)
        if engine is None:
            return done("skipped", "no usable container engine: " + "; ".join(reasons), INFRA_SANDBOX_UNAVAILABLE)
        run.engine = engine
        report.engine = engine.label
        if run.remote and not self.executor.remote_stdin:
            return done("skipped", "the remote runner cannot stage files (no stdin support)", INFRA_SANDBOX_UNAVAILABLE)

        # 3. model + ports
        allocator: LocalPortAllocator | RemotePortAllocator
        try:
            allocator = RemotePortAllocator(await self._remote_used_ports(run)) if run.remote else LocalPortAllocator()
        except InfraToolError as exc:
            return done("failed", exc.message, exc.code)
        try:
            try:
                sbx = build_sandbox_model(loaded, run.sandbox_id, allocator)
            except InfraToolError as exc:
                return done("refused", exc.message, exc.code)
            report.ports = sbx.ports
            report.notes = sbx.notes
            if not sum(sbx.expected.values()):
                return done("refused", "no service would start (all services are behind profiles or scaled to 0)", INFRA_COMPOSE_REFUSED)
            if await asyncio.to_thread(stage_size, loaded.project_dir) > self.options.max_stage_bytes:
                return done(
                    "refused", f"project directory larger than {self.options.max_stage_bytes // (1024 * 1024)} MiB", INFRA_COMPOSE_REFUSED
                )
            return await self._start_and_observe(run, loaded=loaded, sbx=sbx, report=report, deadline=deadline, done=done)
        finally:
            td_task = asyncio.ensure_future(self._teardown(run))
            try:
                report.teardown = await asyncio.shield(td_task)
            except asyncio.CancelledError:
                report.teardown = await td_task
                raise
            finally:
                allocator.release()
                report.duration_ms = int((time.monotonic() - started) * 1000)

    async def _start_and_observe(
        self,
        run: _Run,
        *,
        loaded: LoadedCompose,
        sbx: SandboxModel,
        report: SandboxReport,
        deadline: float,
        done: Callable[[SandboxStatus, str | None, str | None], SandboxReport],
    ) -> SandboxReport:
        engine = run.engine
        assert engine is not None
        # 4. stage + write sandbox file
        try:
            run.stage_root, run.compose_file = await self._stage(run, loaded, sbx)
        except InfraToolError as exc:
            return done("failed", f"staging failed: {exc.message}", exc.code)
        # 5. up
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            return done("timeout", "no time left to start the project", INFRA_TIMEOUT)
        up = await self.executor.run(
            [*engine.argv, "-p", run.project, "-f", run.compose_file, "up", "-d"],
            host=self._target(run),
            timeout=remaining,
            cwd=run.cwd,
            classification="mutate",
            ctx=run.ctx,
        )
        report.up_output = self.redactor.text((up.stdout + "\n" + up.stderr).strip())[-4000:]
        if not up.ok:
            await self._collect(report, run)
            if up.timed_out:
                return done("timeout", "'up' did not finish before the timeout", INFRA_TIMEOUT)
            return done("failed", f"compose up failed: {up.describe(1500)}", INFRA_SANDBOX_FAILED)
        # 6. wait for running/healthy, logs, binding verification, probes
        status, reason = await self._wait(run, sbx, deadline)
        infos = await self._collect(report, run)
        self._verify_bindings(report, sbx, infos)
        if status == "passed" and has_errors(report.findings):
            status, reason = "failed", "unexpected host port bindings"
        if status == "passed" and not run.remote and self.options.probe_ports:
            await self._probe(report)
        code = None if status == "passed" else (INFRA_TIMEOUT if status == "timeout" else INFRA_SANDBOX_FAILED)
        return done(status, reason, code)

    # ------------------------------------------------------------------------------------------ steps
    def _target(self, run: _Run) -> str | None:
        return run.host if run.remote else None

    async def _remote_used_ports(self, run: _Run) -> set[int]:
        used: set[int] = set()
        for flag in ("-ltnH", "-lunH"):
            res = await self.executor.run(["ss", flag], host=run.host, timeout=30, ctx=run.ctx)
            if not res.ok:
                raise InfraToolError(f"cannot list listening ports on {run.host}: {res.describe(300)}", code=INFRA_SANDBOX_FAILED)
            used.update(s.port for s in parse_ss(res.stdout, protocol="udp" if "u" in flag else "tcp"))
        return used

    async def _stage(self, run: _Run, loaded: LoadedCompose, sbx: SandboxModel) -> tuple[str, str]:
        file_name = SANDBOX_FILE.format(id=run.sandbox_id)
        text = yaml.safe_dump(sbx.model, sort_keys=False, default_flow_style=False, allow_unicode=True)
        project_dir = await asyncio.to_thread(loaded.project_dir.resolve)
        if not run.remote:
            return await asyncio.to_thread(_stage_local, project_dir, run.sandbox_id, file_name, text)
        archive = await asyncio.to_thread(_tar_project, project_dir, self.options.max_stage_bytes)
        prefix = _remote_stage_prefix(run.sandbox_id)
        res = await self.executor.run(
            ["mktemp", "-d", prefix + "XXXXXXXX"], host=run.host, timeout=30, classification="mutate", ctx=run.ctx
        )
        remote_root = res.stdout.strip()
        if not res.ok or not remote_root.startswith(prefix) or "/" in remote_root[len(prefix) :]:
            raise InfraToolError(f"mktemp failed on {run.host}: {res.describe(300)}", code=INFRA_SANDBOX_FAILED)
        run.stage_root = remote_root  # from here on the teardown removes it, even if the next steps fail
        unpack = await self.executor.run(
            ["tar", "-xzf", "-", "--no-same-owner", "-C", remote_root],
            host=run.host,
            timeout=120,
            stdin=archive,
            classification="mutate",
            ctx=run.ctx,
        )
        if not unpack.ok:
            raise InfraToolError(f"unpacking the project on {run.host} failed: {unpack.describe(300)}", code=INFRA_SANDBOX_FAILED)
        remote_file = f"{remote_root}/{file_name}"
        write = await self.executor.run(
            ["install", "-m", "0600", "/dev/stdin", remote_file],
            host=run.host,
            timeout=30,
            stdin=text.encode("utf-8"),
            classification="mutate",
            ctx=run.ctx,
        )
        if not write.ok:
            raise InfraToolError(f"writing the sandbox compose file on {run.host} failed: {write.describe(300)}", code=INFRA_SANDBOX_FAILED)
        return remote_root, remote_file

    async def _list(self, run: _Run) -> list[ContainerInfo]:
        assert run.engine is not None
        argv = ContainerInventory.ps_argv(run.engine.engine, all_containers=True, filters=[f"label={SANDBOX_LABEL}={run.sandbox_id}"])
        res = await self.executor.run(argv, host=self._target(run), timeout=30, ctx=run.ctx, audit=False, raw_stdout=True)
        if not res.ok:
            return []
        try:
            return parse_podman_ps(res.stdout, run.host) if run.engine.engine == "podman" else parse_docker_ps(res.stdout, run.host)
        except InfraToolError:
            return []

    async def _inspect(self, run: _Run) -> list[ContainerInfo]:
        assert run.engine is not None
        names = [c.name for c in await self._list(run) if c.name]
        if not names:
            return []
        try:
            return await self.inventory.inspect_many(names, engine=run.engine.engine, host=self._target(run), ctx=run.ctx)
        except InfraToolError:
            return []

    async def _wait(self, run: _Run, sbx: SandboxModel, deadline: float) -> tuple[SandboxStatus, str | None]:
        expected = sum(sbx.expected.values())
        stable_since: float | None = None
        restarts: dict[str, int | None] = {}
        last_problem = "containers did not start"
        while True:
            infos = await self._inspect(run)
            if await self._run_healthchecks(run, infos):
                infos = await self._inspect(run)
            failed = [c for c in infos if (c.state in ("exited", "dead", "stopped") and (c.exit_code or 0) != 0) or c.health == "unhealthy"]
            if failed:
                c = failed[0]
                why = "is unhealthy" if c.health == "unhealthy" else f"exited with code {c.exit_code}"
                return "failed", f"container {c.name} {why}"
            ready = [c for c in infos if (c.running and c.health in (None, "healthy")) or (c.state == "exited" and c.exit_code == 0)]
            settled = bool(infos) and len(infos) >= expected and len(ready) == len(infos)
            changed = any(restarts.get(c.name) not in (None, c.restart_count) for c in infos)
            restarts = {c.name: c.restart_count for c in infos}
            now = time.monotonic()
            if settled and not changed:
                stable_since = stable_since or now
                if now - stable_since >= self.options.settle_seconds:
                    return "passed", None
            else:
                stable_since = None
                waiting = [f"{c.name}: {c.state}{'/' + c.health if c.health else ''}" for c in infos if c not in ready]
                missing = f"waiting for {expected - len(infos)} container(s) to appear; " if len(infos) < expected else ""
                last_problem = (missing + ", ".join(waiting)).strip("; ") or "containers restarting"
            if now + self.options.poll_interval >= deadline:
                return "timeout", f"not ready before the timeout: {last_problem}"
            await asyncio.sleep(self.options.poll_interval)

    async def _run_healthchecks(self, run: _Run, infos: list[ContainerInfo]) -> bool:
        """Podman schedules healthchecks with systemd timers; without systemd (containers, minimal hosts) they never
        run and the state stays ``starting``. ``podman healthcheck run`` executes the check once and records it."""
        assert run.engine is not None
        if run.engine.engine != "podman":
            return False
        ran = False
        for info in infos:
            if info.running and info.health == "starting":
                await self.executor.run(
                    ["podman", "healthcheck", "run", info.name], host=self._target(run), timeout=30, ctx=run.ctx, audit=False
                )
                ran = True
        return ran

    async def _collect(self, report: SandboxReport, run: _Run) -> list[ContainerInfo]:
        assert run.engine is not None
        infos = await self._inspect(run)
        report.services = []
        for info in sorted(infos, key=lambda c: c.name):
            service = info.labels.get("com.docker.compose.service") or info.name
            logs = await self.executor.run(
                [run.engine.engine, "logs", "--tail", str(self.options.log_tail), info.name],
                host=self._target(run),
                timeout=30,
                ctx=run.ctx,
                audit=False,
            )
            text = (logs.stdout + ("\n" + logs.stderr if logs.stderr.strip() else "")).strip()
            report.services.append(
                ServiceState(
                    service,
                    info.name,
                    info.state,
                    info.health,
                    info.exit_code,
                    info.restart_count,
                    self.redactor.text(text)[-self.options.log_chars :],
                )
            )
        return infos

    @staticmethod
    def _verify_bindings(report: SandboxReport, sbx: SandboxModel, infos: list[ContainerInfo]) -> None:
        allowed = {(p.protocol, p.host_port) for p in sbx.ports}
        for info in infos:
            for port in info.ports:
                if port.host_port is None:
                    continue
                loopback = port.host_ip in ("127.0.0.1", "localhost")
                if not loopback or (port.protocol, port.host_port) not in allowed:
                    report.findings.append(
                        Finding(
                            "error",
                            "SANDBOX_UNEXPECTED_BINDING",
                            f"{info.name} publishes {port.render()} – only the allocated loopback ports are allowed",
                            location=info.name,
                        )
                    )

    @staticmethod
    async def _probe(report: SandboxReport) -> None:
        for p in report.ports:
            if p.protocol != "tcp":
                continue
            try:
                _reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", p.host_port), 2)
            except (OSError, TimeoutError):
                p.reachable = False
                continue
            p.reachable = True
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _teardown(self, run: _Run) -> TeardownReport:
        started = time.monotonic()
        td = TeardownReport()
        target = self._target(run)
        if run.engine is not None and run.compose_file is not None:
            down = await self.executor.run(
                [
                    *run.engine.argv,
                    "-p",
                    run.project,
                    "-f",
                    run.compose_file,
                    "down",
                    "-v",
                    "--remove-orphans",
                    "-t",
                    str(self.options.stop_timeout),
                ],
                host=target,
                timeout=self.options.teardown_timeout,
                cwd=run.cwd,
                classification="destructive",
                ctx=run.ctx,
            )
            if not down.ok:
                td.errors.append(f"down: {down.describe(500)}")
        if run.engine is not None:
            await self._sweep(run, td)
        if run.stage_root:
            if not run.remote:
                await asyncio.to_thread(shutil.rmtree, run.stage_root, True)
                if await asyncio.to_thread(os.path.exists, run.stage_root):
                    td.leftovers.append(f"staging directory {run.stage_root}")
            elif run.stage_root.startswith(_remote_stage_prefix(run.sandbox_id)):  # created by mktemp in _stage
                rm = await self.executor.run(
                    ["rm", "-rf", "--", run.stage_root], host=run.host, timeout=60, classification="destructive", ctx=run.ctx
                )
                if not rm.ok:
                    td.leftovers.append(f"staging directory {run.host}:{run.stage_root}")
        td.ok = not td.leftovers
        td.duration_ms = int((time.monotonic() - started) * 1000)
        return td

    async def _sweep(self, run: _Run, td: TeardownReport) -> None:
        """Remove whatever ``down`` left behind (labels of the sandbox id and of the unique project name)."""
        assert run.engine is not None
        engine = run.engine.engine
        target = self._target(run)
        labels = [
            f"label={SANDBOX_LABEL}={run.sandbox_id}",
            f"label=com.docker.compose.project={run.project}",
            f"label=io.podman.compose.project={run.project}",
        ]
        for kind, ls, rm in (
            ("container", ["ps", "-aq"], ["rm", "-f", "-v"]),
            ("network", ["network", "ls", "-q"], ["network", "rm"]),
            ("volume", ["volume", "ls", "-q"], ["volume", "rm", "-f"]),
        ):
            ids = await self._ids(engine, ls, labels, target, run.ctx)
            if not ids:
                continue
            res = await self.executor.run([engine, *rm, *ids], host=target, timeout=60, classification="destructive", ctx=run.ctx)
            if not res.ok:
                td.errors.append(f"{kind} sweep: {res.describe(300)}")
            td.leftovers.extend(f"{kind} {i}" for i in await self._ids(engine, ls, labels, target, run.ctx))

    async def _ids(self, engine: str, ls: list[str], labels: list[str], host: str | None, ctx: InfraContext) -> list[str]:
        ids: list[str] = []
        for label in labels:
            res = await self.executor.run([engine, *ls, "--filter", label], host=host, timeout=30, ctx=ctx, audit=False)
            if res.ok:
                ids.extend(i for i in res.stdout.split() if i and i not in ids)
        return ids


async def cleanup_stale_sandboxes(
    executor: HostExecutor,
    *,
    host: str | None = None,
    engines: tuple[Engine, ...] = ("podman", "docker"),
    ctx: InfraContext | None = None,
) -> list[str]:
    """Remove containers left behind by crashed sandbox runs (label ``hermclaw.sandbox``) – recovery helper."""
    removed: list[str] = []
    for engine in engines:
        res = await executor.run([engine, "ps", "-aq", "--filter", f"label={SANDBOX_LABEL}"], host=host, timeout=30, ctx=ctx, audit=False)
        if not res.ok:
            continue
        ids = res.stdout.split()
        if ids:
            rm = await executor.run([engine, "rm", "-f", "-v", *ids], host=host, timeout=120, classification="destructive", ctx=ctx)
            if rm.ok:
                removed.extend(f"{engine}:{i}" for i in ids)
    return removed


__all__ = [
    "PROJECT_PREFIX",
    "ComposeSandbox",
    "LocalPortAllocator",
    "RemappedPort",
    "RemotePortAllocator",
    "SandboxModel",
    "SandboxOptions",
    "SandboxReport",
    "ServiceState",
    "TeardownReport",
    "build_sandbox_model",
    "check_bind_sources",
    "check_sandbox_rules",
    "cleanup_stale_sandboxes",
    "resolve_extends",
]
