"""Port collision detection before a deployment (Bauplan §6 port plan, §31 "port collision", P28.4).

The host ports a compose file, an nginx config or a systemd unit *requests* are compared with

(a) the **port plan** of the architecture (§6) – each planned port has an owner; a config that is not that owner
    must not take the port ("Keine vorhandenen Dienste überschreiben"), and
(b) the **live listening sockets** of the target host (``ss -ltnpH`` / ``ss -lunpH`` locally or through the remote
    runner) – a port that is already bound by another process is a collision, a port bound by the very service
    being redeployed (``owner``) is expected.

Collisions carry owner information (planned owner, listening process/pid when ``ss`` may show it).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from hermclaw.core.config import HostsConfig
from hermclaw.core.errors import HermclawError
from hermclaw.infratools.compose import load_compose_text, service_ports
from hermclaw.infratools.errors import INFRA_COMMAND_FAILED, INFRA_INVALID_ARGUMENT, Finding, InfraToolError
from hermclaw.infratools.nginxconf import parse as parse_nginx
from hermclaw.infratools.nginxconf import parse_listen
from hermclaw.infratools.process import LOCAL_HOST, HostExecutor, InfraContext

ConfigKind = Literal["auto", "compose", "nginx", "systemd"]
WILDCARDS = {None, "", "*", "0.0.0.0", "::", "[::]"}  # noqa: S104 – wildcard bind addresses are data here


# ============================================================================================ port plan
@dataclass(frozen=True)
class PlannedPort:
    address: str
    port: int
    owner: str
    description: str
    protocol: str = "tcp"
    processes: tuple[str, ...] = ()  # process names that legitimately listen on it

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PY = ("python", "python3", "uvicorn", "gunicorn")
#: Bauplan §6 "Netzwerk- und Portplan"
BAUPLAN_PORT_PLAN: tuple[PlannedPort, ...] = (
    PlannedPort("192.168.178.223", 80, "nginx", "Nginx HTTP", processes=("nginx",)),
    PlannedPort("192.168.178.223", 443, "nginx", "Nginx HTTPS", processes=("nginx",)),
    PlannedPort("192.168.178.225", 8000, "hermclaw-api", "Hermclaw API intern", processes=(*_PY, "hermclaw", "hermclaw-api")),
    PlannedPort("192.168.178.225", 4000, "litellm", "LiteLLM Gateway", processes=(*_PY, "litellm")),
    PlannedPort(
        "192.168.178.225",
        5432,
        "postgresql",
        "PostgreSQL lokal/privat – nicht allgemein veröffentlichen",
        processes=("postgres", "postgresql"),
    ),
    PlannedPort("192.168.178.225", 9100, "metrics", "Metrics (optional)", processes=("node_exporter", "prometheus-node", "node-exporter")),
    PlannedPort(
        "192.168.178.222", 8787, "execution-worker", "Hermclaw Execution Worker API", processes=(*_PY, "hermclaw", "hermclaw-worker")
    ),
    PlannedPort(
        "192.168.178.224", 8787, "model-worker", "Hermclaw Model/Media Worker API", processes=(*_PY, "hermclaw", "hermclaw-worker")
    ),
    PlannedPort("192.168.178.224", 11434, "ollama", "Ollama", processes=("ollama",)),
    PlannedPort("192.168.178.226", 22, "gitlab-ssh", "Git SSH", processes=("sshd", "gitlab-sshd")),
    PlannedPort("192.168.178.226", 80, "gitlab-web", "GitLab Web", processes=("nginx", "gitlab-workhorse", "puma")),
    PlannedPort("192.168.178.226", 443, "gitlab-web", "GitLab Web (TLS)", processes=("nginx", "gitlab-workhorse", "puma")),
)


class PortPlan:
    def __init__(self, entries: Iterable[PlannedPort] = BAUPLAN_PORT_PLAN) -> None:
        self.entries = tuple(entries)

    def for_address(self, address: str | None) -> list[PlannedPort]:
        if not address:
            return []
        norm = normalise_address(address)
        return [e for e in self.entries if normalise_address(e.address) == norm]

    def lookup(self, address: str | None, port: int, protocol: str) -> list[PlannedPort]:
        return [e for e in self.for_address(address) if e.port == port and e.protocol == protocol]


# ============================================================================================ addresses
def normalise_address(address: str | None) -> str:
    if address is None:
        return "*"
    text = address.strip().strip("[]")
    text = text.split("%", 1)[0]
    if text.lower().startswith("::ffff:") and "." in text:
        text = text[7:]
    if text.lower() == "localhost":
        return "127.0.0.1"
    if text in WILDCARDS:
        return "*"
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text.lower()


def _loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def addresses_overlap(a: str | None, b: str | None) -> bool:
    """Two bind addresses conflict when either is a wildcard or both are the same (loopback v4/v6 counted equal)."""
    na, nb = normalise_address(a), normalise_address(b)
    if na == "*" or nb == "*":
        return True
    if na == nb:
        return True
    return _loopback(na) and _loopback(nb)


# ============================================================================================ requested ports
@dataclass(frozen=True)
class RequestedPort:
    port: int
    protocol: str
    address: str | None  # bind address; None = all interfaces
    source: str
    service: str | None = None
    heuristic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_kind(text: str) -> Literal["compose", "nginx", "systemd"]:
    if re.search(r"^\s*\[(Unit|Service|Socket|Install)\]\s*$", text, re.M):
        return "systemd"
    if re.search(r"^\s*services\s*:", text, re.M):
        return "compose"
    return "nginx"


def compose_requested_ports(text: str) -> tuple[list[RequestedPort], list[Finding]]:
    loaded = load_compose_text(text, path=Path("compose.yaml"))
    ports, findings = service_ports(loaded.services)
    out: list[RequestedPort] = []
    for p in ports:
        if p.variable and p.published is None:
            findings.append(
                Finding(
                    "warning",
                    "PORT_UNRESOLVED",
                    f"port '{p.raw}' uses variables and cannot be checked",
                    location=f"services.{p.service}.ports",
                )
            )
            continue
        if p.published is None:
            continue
        out.append(RequestedPort(p.published, p.protocol, p.host_ip, f"compose services.{p.service}.ports '{p.raw}'", p.service))
    return out, findings


def nginx_requested_ports(text: str) -> tuple[list[RequestedPort], list[Finding]]:
    cfg = parse_nginx(text)
    out: list[RequestedPort] = []
    seen: set[tuple[str, int, str]] = set()
    for server in [d for d in cfg.walk() if d.name == "server" and d.is_block]:
        names = [a for d in server.children("server_name") for a in d.args]
        listens = server.children("listen")
        parent = server.parent.name if server.parent else "main"
        if parent == "upstream":
            continue
        if not listens and parent in ("http", "main"):
            items = [("*", 80, "tcp", "listen 80 (implicit)", server.line)]
        else:
            items = []
            for d in listens:
                lst = parse_listen(d)
                if lst.port is None:
                    continue
                items.append((lst.address, lst.port, lst.transport, lst.raw, lst.line))
        for addr, port, transport, raw, line in items:
            key = (addr.strip("[]").lower(), port, transport)
            if key in seen:  # several server blocks share one listening socket in nginx – not a collision
                continue
            seen.add(key)
            out.append(
                RequestedPort(port, transport, None if addr == "*" else addr, f"nginx line {line}: {raw}", names[0] if names else None)
            )
    return out, []


_SYSTEMD_LISTEN = re.compile(r"^\s*(ListenStream|ListenDatagram|ListenSequentialPacket)\s*=\s*(.*?)\s*$")
_EXEC_PORT = re.compile(r"--(?:port|http-port|listen-port|server-port)[=\s]+(\d{1,5})\b")
_EXEC_BIND = re.compile(r"--(?:bind|listen|host-port|address)[=\s]+(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9_.-]+)?:(\d{1,5})\b")
_ENV_PORT = re.compile(r"\b(?:[A-Z_]*_)?PORT=(\d{1,5})\b")


def _split_hostport(value: str) -> tuple[str | None, int] | None:
    value = value.strip()
    if not value or value.startswith(("/", "@", "vsock:")):
        return None
    if value.isdigit():
        return None, int(value)
    if value.startswith("["):
        end = value.find("]")
        if end < 0 or not value[end + 1 :].startswith(":"):
            return None
        host, port_s = value[1:end], value[end + 2 :]
    elif ":" in value:
        host, port_s = value.rsplit(":", 1)
    else:
        return None
    return (host, int(port_s)) if port_s.isdigit() else None


def systemd_requested_ports(text: str) -> tuple[list[RequestedPort], list[Finding]]:
    out: list[RequestedPort] = []
    findings: list[Finding] = []
    section = ""
    socket_ports: list[RequestedPort] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        m = _SYSTEMD_LISTEN.match(line)
        if m and section == "Socket":
            if not m.group(2):
                socket_ports = []  # an empty assignment resets the list (systemd semantics)
                continue
            parsed = _split_hostport(m.group(2))
            if parsed is None:
                continue
            host, port = parsed
            proto = "udp" if m.group(1) == "ListenDatagram" else "tcp"
            socket_ports.append(RequestedPort(port, proto, host, f"systemd line {lineno}: {line}", None))
            continue
        if section == "Service" and (line.startswith(("ExecStart=", "Environment="))):
            for pm in _EXEC_BIND.finditer(line):
                host = (pm.group(1) or "").strip("[]") or None
                out.append(RequestedPort(int(pm.group(2)), "tcp", host, f"systemd line {lineno}: {line[:120]}", None, heuristic=True))
            for pm in _EXEC_PORT.finditer(line):
                out.append(RequestedPort(int(pm.group(1)), "tcp", None, f"systemd line {lineno}: {line[:120]}", None, heuristic=True))
            if line.startswith("Environment="):
                for pm in _ENV_PORT.finditer(line):
                    out.append(RequestedPort(int(pm.group(1)), "tcp", None, f"systemd line {lineno}: {line[:120]}", None, heuristic=True))
    out = socket_ports + out
    valid = [p for p in out if 0 < p.port <= 65535]
    for p in out:
        if p not in valid:
            findings.append(Finding("error", "PORT_INVALID", f"invalid port {p.port}", location=p.source))
    # de-duplicate identical heuristic hits
    unique: list[RequestedPort] = []
    for p in valid:
        if not any(u.port == p.port and u.protocol == p.protocol and u.address == p.address for u in unique):
            unique.append(p)
    return unique, findings


def requested_ports(
    text: str, kind: ConfigKind = "auto"
) -> tuple[Literal["compose", "nginx", "systemd"], list[RequestedPort], list[Finding]]:
    real = detect_kind(text) if kind == "auto" else kind
    if real == "compose":
        ports, findings = compose_requested_ports(text)
    elif real == "systemd":
        ports, findings = systemd_requested_ports(text)
    elif real == "nginx":
        ports, findings = nginx_requested_ports(text)
    else:
        raise InfraToolError(f"unknown config kind '{kind}'", code=INFRA_INVALID_ARGUMENT)
    return real, ports, findings


# ============================================================================================ live sockets
@dataclass(frozen=True)
class ListeningSocket:
    address: str
    port: int
    protocol: str
    process: str | None = None
    pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SS_PROC = re.compile(r'\("([^"]+)",pid=(\d+)')
_NETIDS = {"tcp", "udp", "sctp", "raw", "tcp6", "udp6", "mptcp"}


def parse_ss(text: str, *, protocol: str = "tcp") -> list[ListeningSocket]:
    """``ss -ltnH``/``ss -ltnpH``/``ss -lunpH`` lines → listening sockets (process info when present)."""
    out: list[ListeningSocket] = []
    for line in (text or "").splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        proto = protocol
        if cols[0].lower() in _NETIDS:
            proto = "udp" if cols[0].lower().startswith("udp") else "tcp"
            cols = cols[1:]
        if len(cols) < 4 or not cols[1].isdigit():
            continue
        local = cols[3]
        if ":" not in local:
            continue
        host, port_s = local.rsplit(":", 1)
        if not port_s.isdigit():
            continue
        host = host.strip("[]").split("%", 1)[0] or "*"
        rest = " ".join(cols[5:]) if len(cols) > 5 else ""
        m = _SS_PROC.search(rest)
        out.append(ListeningSocket(host, int(port_s), proto, m.group(1) if m else None, int(m.group(2)) if m else None))
    return out


# ============================================================================================ check
@dataclass(frozen=True)
class PortCollision:
    port: int
    protocol: str
    kind: Literal["plan", "live", "duplicate"]
    owner: str
    detail: str
    requested: RequestedPort
    pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["requested"] = self.requested.to_dict()
        return data

    def render(self) -> str:
        pid = f" pid {self.pid}" if self.pid else ""
        head = f"{self.kind} collision on {self.port}/{self.protocol}: owned by {self.owner}{pid}"
        return f"{head} – {self.detail} (requested by {self.requested.source})"


@dataclass
class PortCheckReport:
    host: str
    address: str | None
    kind: str
    requested: list[RequestedPort]
    collisions: list[PortCollision] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    live_checked: bool = False
    live_error: str | None = None
    listening: list[ListeningSocket] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.collisions and not any(f.severity == "error" for f in self.findings) and self.live_error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "host": self.host,
            "address": self.address,
            "kind": self.kind,
            "requested": [p.to_dict() for p in self.requested],
            "collisions": [c.to_dict() for c in self.collisions],
            "notes": self.notes,
            "findings": [f.to_dict() for f in self.findings],
            "live_checked": self.live_checked,
            "live_error": self.live_error,
        }

    def render(self, limit: int = 12000) -> str:
        ports = ", ".join(sorted({f"{p.port}/{p.protocol}" for p in self.requested})) or "-"
        lines = [
            f"port check ({self.kind}) on {self.host}{' ' + self.address if self.address else ''}: "
            f"{'OK' if self.ok else 'COLLISIONS'} – requested {ports}"
        ]
        if not self.live_checked:
            lines.append(f"live ports not checked{': ' + self.live_error if self.live_error else ''}")
        lines.extend(c.render() for c in self.collisions)
        lines.extend(f"note: {n}" for n in self.notes)
        lines.extend(f.render() for f in self.findings)
        text = "\n".join(lines)
        return text[:limit]


def _owner_matches(owner: str | None, plan: PlannedPort) -> bool:
    return bool(owner) and owner is not None and owner.strip().lower() == plan.owner.lower()


def _process_is_owner(process: str | None, owner: str | None, plans: Sequence[PlannedPort]) -> bool:
    if not process or not owner:
        return False
    proc = process.lower()
    if proc == owner.lower():
        return True
    return any(_owner_matches(owner, p) and proc in (x.lower() for x in p.processes) for p in plans)


def evaluate_collisions(
    requested: Sequence[RequestedPort],
    *,
    plan_entries: Sequence[PlannedPort],
    listening: Sequence[ListeningSocket] | None,
    owner: str | None = None,
    check_duplicates: bool = True,
) -> tuple[list[PortCollision], list[str]]:
    collisions: list[PortCollision] = []
    notes: list[str] = []
    if check_duplicates:
        for i, a in enumerate(requested):
            for b in requested[i + 1 :]:
                if a.port == b.port and a.protocol == b.protocol and addresses_overlap(a.address, b.address) and a.source != b.source:
                    collisions.append(
                        PortCollision(a.port, a.protocol, "duplicate", b.service or "this config", f"also requested by {b.source}", a)
                    )
    for req in requested:
        plans = [p for p in plan_entries if p.port == req.port and p.protocol == req.protocol]
        for plan in plans:
            if _owner_matches(owner, plan):
                notes.append(f"{req.port}/{req.protocol} is planned for {plan.owner} ({plan.description}) – matches the deploying service")
            else:
                collisions.append(
                    PortCollision(req.port, req.protocol, "plan", plan.owner, f"reserved by the port plan for {plan.description}", req)
                )
        for sock in listening or []:
            if sock.port != req.port or sock.protocol != req.protocol or not addresses_overlap(sock.address, req.address):
                continue
            if _process_is_owner(sock.process, owner, plans):
                notes.append(f"{req.port}/{req.protocol} is already served by {sock.process} (the service being redeployed)")
                continue
            if sock.process is None and plans and any(_owner_matches(owner, p) for p in plans):
                notes.append(f"{req.port}/{req.protocol} is listening (process unknown) – assumed to be {owner} per port plan")
                continue
            who = sock.process or (f"{plans[0].owner} (per port plan, process not visible)" if plans else "unknown process")
            collisions.append(
                PortCollision(req.port, req.protocol, "live", who, f"{sock.address}:{sock.port} is already listening", req, pid=sock.pid)
            )
    return collisions, notes


class PortChecker:
    """Checks a config's requested host ports against the port plan and the live sockets of the target host."""

    def __init__(
        self, executor: HostExecutor, *, hosts: HostsConfig | None = None, plan: PortPlan | None = None, timeout: float = 30.0
    ) -> None:
        self.executor = executor
        self.hosts = hosts
        self.plan = plan or PortPlan()
        self.timeout = timeout

    def address_of(self, host: str | None) -> str | None:
        if host is None or host == LOCAL_HOST:
            return None
        if self.hosts is None:
            return host  # an address given directly
        try:
            return self.hosts.by_id(host).address
        except HermclawError:
            return host

    async def listening(
        self, host: str | None, protocols: Iterable[str] = ("tcp",), *, ctx: InfraContext | None = None
    ) -> list[ListeningSocket]:
        sockets: list[ListeningSocket] = []
        for proto in sorted(set(protocols)):
            if proto not in ("tcp", "udp"):
                continue
            flag = "-ltnpH" if proto == "tcp" else "-lunpH"
            res = await self.executor.run(["ss", flag], host=host, timeout=self.timeout, ctx=ctx)
            if not res.ok:
                raise InfraToolError(
                    f"cannot list listening sockets on {host or LOCAL_HOST}: {res.describe(300)}", code=INFRA_COMMAND_FAILED
                )
            sockets.extend(parse_ss(res.stdout, protocol=proto))
        return sockets

    async def check(
        self,
        text: str,
        *,
        kind: ConfigKind = "auto",
        host: str | None = None,
        owner: str | None = None,
        live: bool = True,
        plan_address: str | None = None,
        ctx: InfraContext | None = None,
    ) -> PortCheckReport:
        target = self.executor.check_host(host) if live else (host or LOCAL_HOST)
        real, ports, findings = requested_ports(text, kind)
        address = plan_address or self.address_of(host)
        report = PortCheckReport(target, address, real, ports, findings=findings)
        listening: list[ListeningSocket] | None = None
        if live:
            try:
                listening = await self.listening(host, {p.protocol for p in ports} or {"tcp"}, ctx=ctx)
                report.live_checked = True
                report.listening = listening
            except InfraToolError as exc:
                report.live_error = exc.message
        collisions, notes = evaluate_collisions(
            ports, plan_entries=self.plan.for_address(address), listening=listening, owner=owner, check_duplicates=real != "nginx"
        )
        report.collisions = collisions
        report.notes = notes
        return report


__all__ = [
    "BAUPLAN_PORT_PLAN",
    "ListeningSocket",
    "PlannedPort",
    "PortCheckReport",
    "PortChecker",
    "PortCollision",
    "PortPlan",
    "RequestedPort",
    "addresses_overlap",
    "compose_requested_ports",
    "detect_kind",
    "evaluate_collisions",
    "nginx_requested_ports",
    "normalise_address",
    "parse_ss",
    "requested_ports",
    "systemd_requested_ports",
]
