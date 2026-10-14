"""Container inventory: ``container_list`` / ``container_inspect`` for Podman and Docker (Bauplan §31, P28.1).

Both engines are queried through their CLI JSON output and normalised into :class:`ContainerInfo`:

* Podman: ``podman ps [-a] --format json`` (one JSON array) and ``podman inspect --type container <name>``.
* Docker: ``docker ps [-a] --no-trunc --format '{{json .}}'`` (one JSON object per line) and
  ``docker inspect --type container <name>``.

Commands run locally or on a registered host through the :class:`~hermclaw.infratools.process.HostExecutor`
(remote runner = the SSH tools), so the same code inventories ``.222``/``.223``/``.225`` before a deployment
("existing service inventory", §31). ``engine="auto"`` queries Podman *and* Docker and merges the results; an
engine that is not installed or whose daemon is down is reported in ``engines`` instead of failing the call.
Environment values of containers are never returned (only the variable names) – they regularly hold secrets.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED, Redactor
from hermclaw.infratools.errors import (
    INFRA_COMMAND_FAILED,
    INFRA_CONTAINER_NOT_FOUND,
    INFRA_ENGINE_UNAVAILABLE,
    INFRA_INVALID_ARGUMENT,
    INFRA_OUTPUT_INVALID,
    InfraToolError,
)
from hermclaw.infratools.process import ExecResult, HostExecutor, InfraContext

Engine = Literal["podman", "docker"]
EngineChoice = Literal["auto", "podman", "docker"]
ENGINES: tuple[Engine, ...] = ("podman", "docker")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
FILTER_RE = re.compile(r"^(label|name|status|id|ancestor|network|health)=[A-Za-z0-9_.:/@=+-]{1,300}$")
DEFAULT_TIMEOUT = 30.0

_UNAVAILABLE_HINTS = (
    "cannot connect to the docker daemon",
    "failed to connect to the docker api",
    "is the docker daemon running",
    "cannot connect to podman",
    "unable to connect to podman",
    "permission denied while trying to connect",
    "command not found",
    "no such file or directory",
)
_NOT_FOUND_HINTS = ("no such container", "no such object", "not found")


class PortMapping(BaseModel):
    model_config = ConfigDict(frozen=True)

    container_port: int
    protocol: str = "tcp"
    host_ip: str | None = None
    host_port: int | None = None

    def render(self) -> str:
        if self.host_port is None:
            return f"{self.container_port}/{self.protocol}"
        ip = self.host_ip or "0.0.0.0"  # noqa: S104 – display of a wildcard binding
        return f"{ip}:{self.host_port}->{self.container_port}/{self.protocol}"


class MountInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str
    source: str | None = None
    destination: str
    read_only: bool = False


class ContainerInfo(BaseModel):
    """Engine-independent view of one container."""

    model_config = ConfigDict(frozen=True)

    engine: Engine
    host: str
    id: str
    name: str
    image: str
    state: str  # running|exited|created|paused|restarting|removing|dead|stopped|unknown
    status: str = ""
    health: str | None = None  # healthy|unhealthy|starting – None without healthcheck
    ports: list[PortMapping] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    created: str | None = None
    # inspect-only details
    command: str | None = None
    networks: list[str] = Field(default_factory=list)
    mounts: list[MountInfo] = Field(default_factory=list)
    restart_count: int | None = None
    exit_code: int | None = None
    started_at: str | None = None
    privileged: bool | None = None
    network_mode: str | None = None
    restart_policy: str | None = None
    env_keys: list[str] = Field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.state == "running"

    def summary(self) -> str:
        health = f", {self.health}" if self.health else ""
        ports = ", ".join(p.render() for p in self.ports)
        return f"{self.name} [{self.engine}] {self.image} – {self.state}{health}" + (f" – {ports}" if ports else "")


class EngineStatus(BaseModel):
    engine: Engine
    available: bool
    error: str | None = None
    count: int = 0


class ContainerListResult(BaseModel):
    ok: bool
    host: str
    containers: list[ContainerInfo] = Field(default_factory=list)
    engines: list[EngineStatus] = Field(default_factory=list)
    error: str | None = None
    code: str | None = None


# ============================================================================================ parsing helpers
_HEALTH_RE = re.compile(r"\((healthy|unhealthy|health: starting|starting)\)", re.I)
_STATE_FROM_STATUS = (
    ("up ", "running"),
    ("exited", "exited"),
    ("created", "created"),
    ("paused", "paused"),
    ("restarting", "restarting"),
    ("removal", "removing"),
    ("dead", "dead"),
    ("stopped", "stopped"),
)


def health_from_status(status: str) -> str | None:
    m = _HEALTH_RE.search(status or "")
    if not m:
        return None
    value = m.group(1).lower()
    return "starting" if "starting" in value else value


def _state_from_status(status: str) -> str:
    low = (status or "").lower()
    if "(paused)" in low:
        return "paused"
    for prefix, state in _STATE_FROM_STATUS:
        if low.startswith(prefix) or (prefix == "up " and low == "up"):
            return state
    return "unknown"


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_DOCKER_PORT_RE = re.compile(
    r"^(?:(?P<ip>\[[0-9a-fA-F:.]+\]|[0-9a-fA-F:.]*?):(?P<hport>\d+(?:-\d+)?)->)?(?P<cport>\d+(?:-\d+)?)/(?P<proto>tcp|udp|sctp)$"
)


def _expand_range(text: str) -> list[int]:
    if "-" in text:
        lo_s, hi_s = text.split("-", 1)
        lo, hi = int(lo_s), int(hi_s)
        if hi < lo or hi - lo > 1024:
            return [lo]
        return list(range(lo, hi + 1))
    return [int(text)]


def parse_docker_ports(text: str) -> list[PortMapping]:
    """``0.0.0.0:8080->80/tcp, [::]:8080->80/tcp, 9000/tcp, 127.0.0.1:7000-7001->7000-7001/udp``."""
    ports: list[PortMapping] = []
    seen: set[tuple[int, str, str | None, int | None]] = set()
    for raw in (text or "").split(","):
        item = raw.strip()
        if not item:
            continue
        m = _DOCKER_PORT_RE.match(item)
        if not m:
            continue
        cports = _expand_range(m.group("cport"))
        hports: list[int | None] = list(_expand_range(m.group("hport"))) if m.group("hport") else [None] * len(cports)
        if len(hports) != len(cports):
            hports = [hports[0]] * len(cports)
        ip_raw = m.group("ip")
        ip = ip_raw.strip("[]") if ip_raw else None
        if m.group("hport") and not ip:
            ip = "::"  # docker prints ':::8080->80/tcp' for the IPv6 wildcard
        for cport, hport in zip(cports, hports, strict=True):
            key = (cport, m.group("proto"), ip, hport)
            if key not in seen:
                seen.add(key)
                ports.append(PortMapping(container_port=cport, protocol=m.group("proto"), host_ip=ip, host_port=hport))
    return ports


def parse_docker_labels(text: str | Mapping[str, Any] | None) -> dict[str, str]:
    """Docker prints labels as ``a=b,c=d``; values may contain commas (best effort: a piece without ``=`` is glued on)."""
    if isinstance(text, Mapping):
        return {str(k): str(v) for k, v in text.items()}
    labels: dict[str, str] = {}
    last: str | None = None
    for piece in (text or "").split(","):
        if "=" in piece:
            key, value = piece.split("=", 1)
            last = key.strip()
            labels[last] = value
        elif last is not None:
            labels[last] += "," + piece
    return labels


def _podman_ports(items: Any) -> list[PortMapping]:
    ports: list[PortMapping] = []
    if not isinstance(items, list):
        return ports
    for item in items:
        if not isinstance(item, Mapping):
            continue
        cport = _int(item.get("container_port"))
        if cport is None:
            continue
        hport = _int(item.get("host_port"))
        count = max(1, min(_int(item.get("range")) or 1, 1024))
        proto = str(item.get("protocol") or "tcp")
        ip = str(item.get("host_ip") or "") or None
        for i in range(count):
            ports.append(PortMapping(container_port=cport + i, protocol=proto, host_ip=ip, host_port=(hport + i) if hport else None))
    return ports


def _first_name(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").lstrip("/")


_BARE_REDACTED = re.compile(r"(:\s*)(" + re.escape(REDACTED) + r")")


def loads_lenient(text: str) -> Any:
    """``json.loads`` that tolerates transport redaction.

    The SSH runner redacts command output; ``"Passwd": true`` (podman inspect) becomes ``"Passwd": ***REDACTED***``,
    which is no JSON any more. A bare redaction marker after a colon is quoted before a second attempt.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = _BARE_REDACTED.sub(lambda m: f'{m.group(1)}"{m.group(2)}"', text)
        if repaired == text:
            raise
        return json.loads(repaired)


def parse_podman_ps(text: str, host: str) -> list[ContainerInfo]:
    try:
        data = loads_lenient(text or "[]")
    except json.JSONDecodeError as exc:
        raise InfraToolError(f"podman ps returned invalid JSON: {exc}", code=INFRA_OUTPUT_INVALID) from None
    if data is None:
        data = []
    if not isinstance(data, list):
        raise InfraToolError("podman ps did not return a JSON array", code=INFRA_OUTPUT_INVALID)
    out: list[ContainerInfo] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("Status") or "")
        state = str(item.get("State") or "").lower() or _state_from_status(status)
        out.append(
            ContainerInfo(
                engine="podman",
                host=host,
                id=str(item.get("Id") or item.get("ID") or ""),
                name=_first_name(item.get("Names")),
                image=str(item.get("Image") or ""),
                state=state,
                status=status,
                health=health_from_status(status),
                ports=_podman_ports(item.get("Ports")),
                labels={str(k): str(v) for k, v in (item.get("Labels") or {}).items()},
                created=str(item.get("CreatedAt") or item.get("Created") or "") or None,
                networks=[str(n) for n in (item.get("Networks") or [])],
                exit_code=_int(item.get("ExitCode")) if state in ("exited", "stopped") else None,
                restart_count=_int(item.get("Restarts")),
            )
        )
    return out


def parse_docker_ps(text: str, host: str) -> list[ContainerInfo]:
    out: list[ContainerInfo] = []
    for lineno, line in enumerate((text or "").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = loads_lenient(line)
        except json.JSONDecodeError as exc:
            raise InfraToolError(f"docker ps line {lineno} is not JSON: {exc}", code=INFRA_OUTPUT_INVALID) from None
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("Status") or "")
        state = str(item.get("State") or "").lower() or _state_from_status(status)
        networks = [n for n in str(item.get("Networks") or "").split(",") if n]
        out.append(
            ContainerInfo(
                engine="docker",
                host=host,
                id=str(item.get("ID") or ""),
                name=_first_name(str(item.get("Names") or "").split(",")[0]),
                image=str(item.get("Image") or ""),
                state=state,
                status=status,
                health=health_from_status(status),
                ports=parse_docker_ports(str(item.get("Ports") or "")),
                labels=parse_docker_labels(item.get("Labels")),
                created=str(item.get("CreatedAt") or "") or None,
                networks=networks,
            )
        )
    return out


def _inspect_ports(settings: Any) -> list[PortMapping]:
    ports: list[PortMapping] = []
    if not isinstance(settings, Mapping):
        return ports
    for key, bindings in (settings.get("Ports") or {}).items():
        cport_s, _, proto = str(key).partition("/")
        cport = _int(cport_s)
        if cport is None:
            continue
        if not bindings:
            ports.append(PortMapping(container_port=cport, protocol=proto or "tcp"))
            continue
        for b in bindings:
            if isinstance(b, Mapping):
                ports.append(
                    PortMapping(
                        container_port=cport,
                        protocol=proto or "tcp",
                        host_ip=str(b.get("HostIp") or "") or None,
                        host_port=_int(b.get("HostPort")),
                    )
                )
    return ports


def parse_inspect(text: str, engine: Engine, host: str) -> list[ContainerInfo]:
    """``docker|podman inspect --type container`` output (a JSON array) → :class:`ContainerInfo` with details."""
    try:
        data = loads_lenient(text or "[]")
    except json.JSONDecodeError as exc:
        raise InfraToolError(f"{engine} inspect returned invalid JSON: {exc}", code=INFRA_OUTPUT_INVALID) from None
    if not isinstance(data, list):
        raise InfraToolError(f"{engine} inspect did not return a JSON array", code=INFRA_OUTPUT_INVALID)
    out: list[ContainerInfo] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        state = item.get("State") if isinstance(item.get("State"), Mapping) else {}
        assert isinstance(state, Mapping)
        config = item.get("Config") if isinstance(item.get("Config"), Mapping) else {}
        assert isinstance(config, Mapping)
        host_cfg = item.get("HostConfig") if isinstance(item.get("HostConfig"), Mapping) else {}
        assert isinstance(host_cfg, Mapping)
        net = item.get("NetworkSettings") if isinstance(item.get("NetworkSettings"), Mapping) else {}
        assert isinstance(net, Mapping)
        health_obj = state.get("Health") or state.get("Healthcheck")
        health = None
        if isinstance(health_obj, Mapping):
            health = str(health_obj.get("Status") or "").lower() or None
            if health in ("", "none"):
                health = None
        status_text = str(state.get("Status") or "unknown").lower()
        cmd = config.get("Cmd")
        command = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else (str(cmd) if cmd else None)
        mounts = []
        for m in item.get("Mounts") or []:
            if isinstance(m, Mapping):
                mounts.append(
                    MountInfo(
                        type=str(m.get("Type") or "bind"),
                        source=str(m.get("Source") or m.get("Name") or "") or None,
                        destination=str(m.get("Destination") or ""),
                        read_only=not bool(m.get("RW", True)),
                    )
                )
        env_keys = sorted({str(e).split("=", 1)[0] for e in (config.get("Env") or []) if isinstance(e, str)})
        restart = host_cfg.get("RestartPolicy")
        image = str(config.get("Image") or item.get("ImageName") or item.get("Image") or "")
        out.append(
            ContainerInfo(
                engine=engine,
                host=host,
                id=str(item.get("Id") or ""),
                name=_first_name(item.get("Name")),
                image=image,
                state=status_text,
                status=status_text,
                health=health,
                ports=_inspect_ports(net),
                labels={str(k): str(v) for k, v in (config.get("Labels") or {}).items()},
                created=str(item.get("Created") or "") or None,
                command=command,
                networks=sorted((net.get("Networks") or {}).keys()) if isinstance(net.get("Networks"), Mapping) else [],
                mounts=mounts,
                restart_count=_int(item.get("RestartCount")),
                exit_code=_int(state.get("ExitCode")),
                started_at=str(state.get("StartedAt") or "") or None,
                privileged=bool(host_cfg.get("Privileged")) if "Privileged" in host_cfg else None,
                network_mode=str(host_cfg.get("NetworkMode") or "") or None,
                restart_policy=str(restart.get("Name") or "") or None if isinstance(restart, Mapping) else None,
                env_keys=env_keys,
            )
        )
    return out


def _unavailable(res: ExecResult) -> bool:
    if res.missing_binary:
        return True
    text = (res.error or res.stderr or "").lower()
    return any(h in text for h in _UNAVAILABLE_HINTS)


def _redact_info(info: ContainerInfo, redactor: Redactor) -> ContainerInfo:
    labels = redactor.obj(dict(info.labels))
    command = redactor.text(info.command) if info.command else None
    return info.model_copy(update={"labels": labels, "command": command})


# ============================================================================================ inventory
class ContainerInventory:
    """``container_list`` / ``container_inspect`` on the local machine or a registered host."""

    def __init__(self, executor: HostExecutor, *, timeout: float = DEFAULT_TIMEOUT, redactor: Redactor = DEFAULT_REDACTOR) -> None:
        self.executor = executor
        self.timeout = timeout
        self.redactor = redactor

    @staticmethod
    def ps_argv(engine: Engine, *, all_containers: bool = False, filters: Sequence[str] = ()) -> list[str]:
        argv = [engine, "ps"]
        if all_containers:
            argv.append("-a")
        for f in filters:
            if not FILTER_RE.match(f):
                raise InfraToolError(f"invalid container filter '{f[:80]}'", code=INFRA_INVALID_ARGUMENT)
            argv += ["--filter", f]
        if engine == "podman":
            argv += ["--format", "json"]
        else:
            argv += ["--no-trunc", "--format", "{{json .}}"]
        return argv

    @staticmethod
    def inspect_argv(engine: Engine, names: Iterable[str]) -> list[str]:
        items = list(names)
        for name in items:
            if not NAME_RE.match(name):
                raise InfraToolError(f"invalid container name '{name[:80]}'", code=INFRA_INVALID_ARGUMENT)
        if not items:
            raise InfraToolError("no container name given", code=INFRA_INVALID_ARGUMENT)
        return [engine, "inspect", "--type", "container", *items]

    @staticmethod
    def _engines(engine: EngineChoice) -> tuple[Engine, ...]:
        if engine == "auto":
            return ENGINES
        if engine not in ENGINES:
            raise InfraToolError(f"unknown container engine '{engine}'", code=INFRA_INVALID_ARGUMENT)
        return (engine,)

    async def list_containers(
        self,
        *,
        host: str | None = None,
        engine: EngineChoice = "auto",
        all_containers: bool = False,
        filters: Sequence[str] = (),
        ctx: InfraContext | None = None,
    ) -> ContainerListResult:
        target = self.executor.check_host(host)
        containers: list[ContainerInfo] = []
        statuses: list[EngineStatus] = []
        failures: list[str] = []
        for eng in self._engines(engine):
            res = await self.executor.run(
                self.ps_argv(eng, all_containers=all_containers, filters=filters),
                host=host,
                timeout=self.timeout,
                ctx=ctx,
                raw_stdout=True,  # JSON is parsed first, the normalised fields are redacted afterwards
            )
            if not res.ok:
                reason = res.describe(300)
                statuses.append(EngineStatus(engine=eng, available=False, error=reason))
                if not _unavailable(res):
                    failures.append(f"{eng}: {reason}")
                continue
            try:
                found = parse_podman_ps(res.stdout, target) if eng == "podman" else parse_docker_ps(res.stdout, target)
            except InfraToolError as exc:
                statuses.append(EngineStatus(engine=eng, available=True, error=exc.message))
                failures.append(f"{eng}: {exc.message}")
                continue
            statuses.append(EngineStatus(engine=eng, available=True, count=len(found)))
            containers.extend(_redact_info(c, self.redactor) for c in found)
        any_ok = any(s.available and s.error is None for s in statuses)
        if not any_ok:
            code = (
                INFRA_ENGINE_UNAVAILABLE if not failures else INFRA_OUTPUT_INVALID if "JSON" in " ".join(failures) else INFRA_COMMAND_FAILED
            )
            detail = "; ".join(failures) or "; ".join(f"{s.engine}: {s.error}" for s in statuses)
            return ContainerListResult(
                ok=False, host=target, engines=statuses, error=f"no container engine usable on {target}: {detail}", code=code
            )
        containers.sort(key=lambda c: (c.engine, c.name))
        return ContainerListResult(ok=True, host=target, containers=containers, engines=statuses)

    async def inspect(
        self, name: str, *, host: str | None = None, engine: EngineChoice = "auto", ctx: InfraContext | None = None
    ) -> ContainerInfo:
        target = self.executor.check_host(host)
        problems: list[str] = []
        not_found = 0
        engines = self._engines(engine)
        for eng in engines:
            res = await self.executor.run(self.inspect_argv(eng, [name]), host=host, timeout=self.timeout, ctx=ctx, raw_stdout=True)
            if res.ok:
                found = parse_inspect(res.stdout, eng, target)
                if found:
                    return _redact_info(found[0], self.redactor)
                not_found += 1
                continue
            text = (res.stderr or res.error or "").lower()
            if any(h in text for h in _NOT_FOUND_HINTS) and not _unavailable(res):
                not_found += 1
            else:
                problems.append(f"{eng}: {res.describe(300)}")
        if not_found:
            raise InfraToolError(
                f"container '{name}' not found on {target}",
                code=INFRA_CONTAINER_NOT_FOUND,
                details={"engines": list(engines), "problems": problems},
            )
        raise InfraToolError(f"no container engine usable on {target}: {'; '.join(problems)}", code=INFRA_ENGINE_UNAVAILABLE)

    async def inspect_many(
        self, names: Sequence[str], *, engine: Engine, host: str | None = None, ctx: InfraContext | None = None
    ) -> list[ContainerInfo]:
        """Inspect several containers of one engine at once (sandbox status polling)."""
        if not names:
            return []
        target = self.executor.check_host(host)
        res = await self.executor.run(
            self.inspect_argv(engine, names), host=host, timeout=self.timeout, ctx=ctx, audit=False, raw_stdout=True
        )
        if not res.ok and not res.stdout.strip().startswith("["):
            raise InfraToolError(res.describe(), code=INFRA_COMMAND_FAILED)
        return [_redact_info(c, self.redactor) for c in parse_inspect(res.stdout, engine, target)]


__all__ = [
    "ENGINES",
    "ContainerInfo",
    "ContainerInventory",
    "ContainerListResult",
    "Engine",
    "EngineChoice",
    "EngineStatus",
    "MountInfo",
    "PortMapping",
    "health_from_status",
    "loads_lenient",
    "parse_docker_labels",
    "parse_docker_ports",
    "parse_docker_ps",
    "parse_inspect",
    "parse_podman_ps",
]
