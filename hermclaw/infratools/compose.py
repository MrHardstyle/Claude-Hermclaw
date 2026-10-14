"""Compose file validation (Bauplan §31 ``compose_validate``, P28.2).

Two independent checks:

1. **Engine check** – the file is handed to a real Compose implementation: ``docker compose -f … config --quiet``
   (Compose-Specification schema validation, works without a running daemon) or, without Docker,
   ``podman-compose -f … config`` / ``podman compose``. No engine installed → the engine check is ``skipped`` with
   the reason; the structural check still runs.
2. **Structural check** with PyYAML (:func:`validate_structure`): services exist and have ``image`` or ``build``,
   port syntax and ranges, volume sources (relative/absolute, undeclared named volumes), ``env_file`` existence,
   ``depends_on``/``networks`` references, and the security rules – no ``privileged: true``, no host network or
   host namespaces, no Docker/Podman socket mounts, no sensitive host paths – unless a policy flag allows them.

The YAML is read like Compose itself reads it (YAML 1.2 core schema: ``22:22`` is a string, ``yes`` is not a
boolean, ``!reset``/``!override`` tags are accepted); unquoted ``a:b`` port mappings are flagged because YAML 1.1
parsers (PyYAML, podman-compose 1.0.x) silently turn ``22:22`` into the base-60 number 1342.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml

from hermclaw.infratools.containers import Engine
from hermclaw.infratools.errors import (
    INFRA_COMPOSE_INVALID,
    INFRA_PATH_INVALID,
    Finding,
    InfraToolError,
    has_errors,
)
from hermclaw.infratools.process import LOCAL_HOST, ExecResult, HostExecutor, InfraContext

MAX_COMPOSE_BYTES = 1_000_000
MAX_ALIASES = 200
MAX_EXPANDED_NODES = 200_000  # nodes after alias expansion (billion-laughs guard)
MAX_PORT_RANGE = 1000
DEFAULT_ENGINE_TIMEOUT = 60.0
SANDBOX_LABEL = "hermclaw.sandbox"

TOP_LEVEL_KEYS = {"version", "name", "services", "networks", "volumes", "configs", "secrets", "include", "models"}
SOCKET_PATTERNS = (
    re.compile(r"(^|/)docker\.sock$"),
    re.compile(r"(^|/)podman\.sock$"),
    re.compile(r"(^|/)containerd\.sock$"),
    re.compile(r"^/(var/)?run/(docker|podman|containerd)(/|$)"),
)
SENSITIVE_HOST_PATHS = (
    "/",
    "/etc",
    "/root",
    "/home",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/var",
    "/var/lib",
    "/var/lib/docker",
    "/var/lib/containers",
    "/var/run",
    "/run",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
)
DANGEROUS_CAPS = {"ALL", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_RAWIO", "DAC_READ_SEARCH", "NET_ADMIN", "SYS_BOOT"}
_VAR_RE = re.compile(r"\$\{?[A-Za-z_]")
_SEXAGESIMAL_RE = re.compile(r"^[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+$")
_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]*(?:[a-zA-Z0-9._-]+)?$", re.I)
_SECRET_KEY_RE = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key)")


# ============================================================================================ YAML loading
class ComposeLoader(yaml.SafeLoader):
    """SafeLoader with the YAML 1.2 core schema scalars Compose uses, ``!reset``/``!override`` and an alias limit."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.alias_count = 0

    def compose_node(self, parent: Any, index: Any) -> Any:  # type: ignore[override]
        if self.check_event(yaml.AliasEvent):
            self.alias_count += 1
            if self.alias_count > MAX_ALIASES:
                raise yaml.YAMLError(f"more than {MAX_ALIASES} YAML aliases (alias bomb?)")
        return super().compose_node(parent, index)


_BOOL, _INT, _FLOAT = "tag:yaml.org,2002:bool", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float"
ComposeLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag not in (_BOOL, _INT, _FLOAT)]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
ComposeLoader.add_implicit_resolver(_BOOL, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
ComposeLoader.add_implicit_resolver(_INT, re.compile(r"^(?:[-+]?(?:0|[1-9][0-9]*)|0x[0-9a-fA-F]+)$"), list("-+0123456789"))
ComposeLoader.add_implicit_resolver(
    _FLOAT,
    re.compile(r"^(?:[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
    list("-+0123456789."),
)


def _construct_tagged(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    value = loader.construct_scalar(node)  # type: ignore[arg-type]
    return None if value in ("", "null", "~") else value


ComposeLoader.add_constructor("!reset", _construct_tagged)
ComposeLoader.add_constructor("!override", _construct_tagged)


@dataclass
class LoadedCompose:
    path: Path
    model: dict[str, Any]
    findings: list[Finding] = field(default_factory=list)

    @property
    def project_dir(self) -> Path:
        return self.path.parent

    @property
    def services(self) -> dict[str, Any]:
        services = self.model.get("services")
        return services if isinstance(services, dict) else {}


def _plain_port_scalars(root: yaml.Node | None) -> list[tuple[str, str, int]]:
    """Plain (unquoted) ``a:b`` scalars in ``services.*.ports`` – YAML 1.1 parsers read them as base-60 numbers."""
    out: list[tuple[str, str, int]] = []
    if not isinstance(root, yaml.MappingNode):
        return out
    for key, value in root.value:
        if getattr(key, "value", None) != "services" or not isinstance(value, yaml.MappingNode):
            continue
        for skey, svalue in value.value:
            if not isinstance(svalue, yaml.MappingNode):
                continue
            for pkey, pvalue in svalue.value:
                if getattr(pkey, "value", None) != "ports" or not isinstance(pvalue, yaml.SequenceNode):
                    continue
                for item in pvalue.value:
                    if isinstance(item, yaml.ScalarNode) and item.style is None and _SEXAGESIMAL_RE.match(item.value):
                        out.append((str(skey.value), item.value, item.start_mark.line + 1))
    return out


def _expanded_size(node: yaml.Node, memo: dict[int, int]) -> int:
    """Number of nodes the document has once aliases are expanded (memoised per node, capped)."""
    key = id(node)
    if key in memo:
        return memo[key]
    memo[key] = MAX_EXPANDED_NODES + 1  # a recursive alias counts as too large
    total = 1
    if isinstance(node, yaml.SequenceNode):
        children: list[yaml.Node] = list(node.value)
    elif isinstance(node, yaml.MappingNode):
        children = [n for pair in node.value for n in pair]
    else:
        children = []
    for child in children:
        total += _expanded_size(child, memo)
        if total > MAX_EXPANDED_NODES:
            break
    memo[key] = total
    return total


def load_compose_text(text: str, *, path: Path) -> LoadedCompose:
    if len(text.encode("utf-8", errors="replace")) > MAX_COMPOSE_BYTES:
        raise InfraToolError(f"compose file larger than {MAX_COMPOSE_BYTES} bytes", code=INFRA_COMPOSE_INVALID)
    loader = ComposeLoader(text)
    try:
        node = loader.get_single_node()
        if node is not None and _expanded_size(node, {}) > MAX_EXPANDED_NODES:
            raise yaml.YAMLError(f"the document expands to more than {MAX_EXPANDED_NODES} nodes (alias bomb?)")
        data = loader.construct_document(node) if node is not None else None
    except yaml.YAMLError as exc:
        raise InfraToolError(f"invalid YAML: {exc}", code=INFRA_COMPOSE_INVALID) from None
    finally:
        loader.dispose()
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise InfraToolError("a compose file must contain a mapping at the top level", code=INFRA_COMPOSE_INVALID)
    loaded = LoadedCompose(path, data)
    for service, value, line in _plain_port_scalars(node):
        loaded.findings.append(
            Finding(
                "warning",
                "COMPOSE_PORT_UNQUOTED",
                f'port mapping {value} is not quoted – YAML 1.1 parsers read it as a base-60 number; write "{value}"',
                location=f"services.{service}.ports",
                line=line,
            )
        )
    return loaded


def load_compose(path: Path) -> LoadedCompose:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InfraToolError(f"compose file '{path}' cannot be read: {exc.strerror or exc}", code=INFRA_PATH_INVALID) from None
    if not path.is_file():
        raise InfraToolError(f"compose file '{path}' is not a regular file", code=INFRA_PATH_INVALID)
    if size > MAX_COMPOSE_BYTES:
        raise InfraToolError(f"compose file larger than {MAX_COMPOSE_BYTES} bytes", code=INFRA_COMPOSE_INVALID)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise InfraToolError("compose file is not UTF-8 text", code=INFRA_COMPOSE_INVALID) from None
    return load_compose_text(text, path=path)


# ============================================================================================ ports
@dataclass(frozen=True)
class ComposePort:
    """One requested port mapping (ranges are expanded: one entry per host port)."""

    service: str
    target: str  # container port (number as text, or an unresolved ${VAR})
    published: int | None  # host port; None = ephemeral / not published / unresolved
    host_ip: str | None
    protocol: str
    raw: str
    variable: bool = False
    published_range: bool = False  # host range for a single container port: compose picks one of them
    entry: int = 0  # index of the ``ports:`` entry inside its service (one entry may expand to several ports)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def bind_addresses_overlap(a: str | None, b: str | None) -> bool:
    """Do two host bind addresses compete for the same port?

    No address (all interfaces) overlaps everything; ``0.0.0.0`` overlaps every IPv4 address and ``::`` every IPv6
    address (container engines bind ``[::]`` with ``IPV6_V6ONLY``, so ``0.0.0.0:80`` and ``[::]:80`` coexist); other
    addresses overlap only themselves. Host names are compared literally.
    """

    def norm(value: str | None) -> tuple[str, str]:
        text = (value or "").strip().strip("[]")
        if text in ("", "*"):
            return "any", "*"
        try:
            ip = ipaddress.ip_address(text.split("%", 1)[0])
        except ValueError:
            return "name", text.lower()
        family = f"v{ip.version}"
        return family, "*" if ip.is_unspecified else str(ip)

    (fa, va), (fb, vb) = norm(a), norm(b)
    if "any" in (fa, fb):
        return True
    if fa != fb:
        return False
    return "*" in (va, vb) or va == vb


def _port_number(text: str) -> int | None:
    if not text.isdigit():
        return None
    value = int(text)
    return value if 0 < value <= 65535 else None


def _port_range(text: str) -> tuple[int, int] | None:
    if "-" in text:
        lo_s, hi_s = text.split("-", 1)
        lo, hi = _port_number(lo_s), _port_number(hi_s)
        if lo is None or hi is None or hi < lo or hi - lo >= MAX_PORT_RANGE:
            return None
        return lo, hi
    num = _port_number(text)
    return (num, num) if num is not None else None


def _split_colons(text: str) -> list[str]:
    """Split on ':' outside ``${...}`` (``${PORT:-8080}:80`` → ``['${PORT:-8080}', '80']``)."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "$" and i + 1 < len(text) and text[i + 1] == "{":
            depth += 1
            current.append("${")
            i += 2
            continue
        if ch == "}" and depth:
            depth -= 1
        elif ch == ":" and not depth:
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def parse_port_entry(service: str, entry: Any) -> tuple[list[ComposePort], list[str]]:
    """Parse one ``ports:`` entry (short or long syntax). Returns ``(ports, problems)``."""
    problems: list[str] = []
    if isinstance(entry, bool) or entry is None:
        return [], [f"invalid port entry {entry!r}"]
    if isinstance(entry, int):
        entry = str(entry)
    if isinstance(entry, Mapping):
        target = str(entry.get("target", "")).strip()
        published_raw = entry.get("published")
        published = "" if published_raw is None else str(published_raw).strip()
        long_ip = str(entry.get("host_ip") or "") or None
        proto = str(entry.get("protocol") or "tcp").lower()
        raw = ", ".join(f"{k}={v}" for k, v in entry.items())
        return _build_ports(service, host_ip=long_ip, published=published, target=target, proto=proto, raw=raw, problems=problems), problems
    if not isinstance(entry, str):
        return [], [f"invalid port entry {entry!r}"]
    raw = entry
    text = entry.strip()
    proto = "tcp"
    if "/" in text:
        text, proto = text.rsplit("/", 1)
        proto = proto.lower()
    host_ip: str | None = None
    if text.startswith("["):
        end = text.find("]")
        if end < 0 or not text[end + 1 :].startswith(":"):
            return [], [f"invalid IPv6 port mapping '{raw}'"]
        host_ip = text[1:end]
        rest = text[end + 2 :]
        if ":" not in rest:
            return [], [f"port mapping '{raw}' has a host IP but no container port"]
        rest_parts = _split_colons(rest)
        if len(rest_parts) != 2:
            return [], [f"invalid IPv6 port mapping '{raw}'"]
        published, target = rest_parts
    else:
        parts = _split_colons(text)
        if len(parts) == 1:
            published, target = "", parts[0]
        elif len(parts) == 2:
            published, target = parts
        elif len(parts) == 3:
            host_ip, published, target = parts[0] or None, parts[1], parts[2]
        else:
            return [], [f"invalid port mapping '{raw}' (expected [HOST_IP:][HOST:]CONTAINER[/PROTO])"]
    return _build_ports(service, host_ip=host_ip, published=published, target=target, proto=proto, raw=raw, problems=problems), problems


def _build_ports(
    service: str, *, host_ip: str | None, published: str, target: str, proto: str, raw: str, problems: list[str]
) -> list[ComposePort]:
    if proto not in ("tcp", "udp", "sctp"):
        problems.append(f"port mapping '{raw}' has unknown protocol '{proto}'")
        return []
    if not target:
        problems.append(f"port mapping '{raw}' has no container port")
        return []
    variable = bool(_VAR_RE.search(published) or _VAR_RE.search(target) or (host_ip and _VAR_RE.search(host_ip)))
    if variable:
        pub = _port_number(published) if published and not _VAR_RE.search(published) else None
        return [ComposePort(service, target, pub, host_ip, proto, raw, variable=True)]
    trange = _port_range(target)
    if trange is None:
        problems.append(f"port mapping '{raw}': container port '{target}' is not a port number or range (1-65535)")
        return []
    if not published:
        return [ComposePort(service, str(p), None, host_ip, proto, raw) for p in range(trange[0], trange[1] + 1)]
    prange = _port_range(published)
    if prange is None:
        problems.append(f"port mapping '{raw}': host port '{published}' is not a port number or range (1-65535)")
        return []
    tcount, pcount = trange[1] - trange[0] + 1, prange[1] - prange[0] + 1
    if tcount == pcount:
        return [ComposePort(service, str(trange[0] + i), prange[0] + i, host_ip, proto, raw) for i in range(tcount)]
    if tcount == 1:
        return [ComposePort(service, str(trange[0]), p, host_ip, proto, raw, published_range=True) for p in range(prange[0], prange[1] + 1)]
    problems.append(f"port mapping '{raw}': host range and container range differ in length")
    return []


def service_ports(services: Mapping[str, Any]) -> tuple[list[ComposePort], list[Finding]]:
    ports: list[ComposePort] = []
    findings: list[Finding] = []
    for name, svc in services.items():
        if not isinstance(svc, Mapping):
            continue
        entries = svc.get("ports") or []
        if not isinstance(entries, list):
            findings.append(Finding("error", "COMPOSE_PORT_INVALID", "ports must be a list", location=f"services.{name}.ports"))
            continue
        for index, entry in enumerate(entries):
            parsed, problems = parse_port_entry(str(name), entry)
            ports.extend(dataclasses.replace(p, entry=index) for p in parsed)
            findings.extend(Finding("error", "COMPOSE_PORT_INVALID", p, location=f"services.{name}.ports") for p in problems)
    return ports, findings


# ============================================================================================ volumes
@dataclass(frozen=True)
class ComposeVolume:
    service: str
    type: str  # bind | volume | tmpfs | npipe | cluster | image
    source: str | None
    target: str
    read_only: bool
    raw: str


def _looks_like_path(source: str) -> bool:
    return source.startswith(("/", ".", "~", "$")) or re.match(r"^[A-Za-z]:[\\/]", source) is not None


def parse_volume_entry(service: str, entry: Any) -> tuple[ComposeVolume | None, str | None]:
    if isinstance(entry, Mapping):
        vtype = str(entry.get("type") or "volume")
        source = entry.get("source")
        target = str(entry.get("target") or "")
        if not target:
            return None, "volume entry without target"
        return ComposeVolume(service, vtype, str(source) if source else None, target, bool(entry.get("read_only")), str(dict(entry))), None
    if not isinstance(entry, str) or not entry.strip():
        return None, f"invalid volume entry {entry!r}"
    parts = entry.split(":")
    # Windows drive letters are not supported on the Linux hosts of this deployment
    if len(parts) == 1:
        return ComposeVolume(service, "volume", None, parts[0], False, entry), None
    source, target = parts[0], parts[1]
    mode = parts[2] if len(parts) > 2 else ""
    if len(parts) > 3:
        return None, f"invalid volume entry '{entry}'"
    read_only = "ro" in mode.split(",")
    vtype = "bind" if _looks_like_path(source) else "volume"
    return ComposeVolume(service, vtype, source, target, read_only, entry), None


def service_volumes(services: Mapping[str, Any]) -> tuple[list[ComposeVolume], list[Finding]]:
    volumes: list[ComposeVolume] = []
    findings: list[Finding] = []
    for name, svc in services.items():
        if not isinstance(svc, Mapping):
            continue
        entries = svc.get("volumes") or []
        if not isinstance(entries, list):
            findings.append(Finding("error", "COMPOSE_VOLUME_INVALID", "volumes must be a list", location=f"services.{name}.volumes"))
            continue
        for entry in entries:
            vol, problem = parse_volume_entry(str(name), entry)
            if problem:
                findings.append(Finding("error", "COMPOSE_VOLUME_INVALID", problem, location=f"services.{name}.volumes"))
            elif vol is not None:
                volumes.append(vol)
    return volumes, findings


def resolve_bind_source(source: str, project_dir: Path) -> Path | None:
    """Host path of a bind source (``None`` for unresolved variables)."""
    if _VAR_RE.search(source):
        return None
    if source.startswith("~"):
        return Path(source).expanduser()
    path = Path(source)
    return path if path.is_absolute() else project_dir / path


def is_socket_path(path: str) -> bool:
    norm = PurePosixPath(path).as_posix()
    return any(p.search(norm) for p in SOCKET_PATTERNS)


def is_sensitive_host_path(path: str) -> bool:
    norm = PurePosixPath(path).as_posix().rstrip("/") or "/"
    return norm in SENSITIVE_HOST_PATHS


# ============================================================================================ structure
@dataclass(frozen=True)
class ComposePolicy:
    """What the structural check tolerates. The sandbox always uses the strict default."""

    allow_privileged: bool = False
    allow_host_network: bool = False  # network_mode: host and pid/ipc/uts/userns host namespaces
    allow_docker_socket: bool = False
    allow_sensitive_mounts: bool = False
    allow_absolute_binds: bool = True  # warning only


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _env_files(value: Any) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for item in _as_list(value):
        if isinstance(item, str):
            out.append((item, True))
        elif isinstance(item, Mapping) and item.get("path"):
            out.append((str(item["path"]), item.get("required", True) is not False))
    return out


def _depends_on(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [str(k) for k in value]
    return [str(v) for v in _as_list(value) if isinstance(v, str)]


def _networks_of(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [str(k) for k in value]
    return [str(v) for v in _as_list(value) if isinstance(v, str)]


def _env_items(value: Any) -> list[tuple[str, str | None]]:
    if isinstance(value, Mapping):
        return [(str(k), None if v is None else str(v)) for k, v in value.items()]
    out: list[tuple[str, str | None]] = []
    for item in _as_list(value):
        if isinstance(item, str):
            key, sep, val = item.partition("=")
            out.append((key, val if sep else None))
    return out


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_structure(loaded: LoadedCompose, policy: ComposePolicy | None = None) -> list[Finding]:
    """Structural and security rules over the parsed model (paths relative to the compose file's directory)."""
    pol = policy or ComposePolicy()
    model = loaded.model
    project_dir = loaded.project_dir
    findings: list[Finding] = list(loaded.findings)

    def add(sev: Literal["error", "warning", "info"], code: str, msg: str, loc: str | None = None) -> None:
        findings.append(Finding(sev, code, msg, location=loc))

    for key in model:
        if not isinstance(key, str) or (key not in TOP_LEVEL_KEYS and not key.startswith("x-")):
            add("warning", "COMPOSE_UNKNOWN_KEY", f"unknown top-level key '{key}'", str(key))
    if "version" in model:
        add("info", "COMPOSE_VERSION_OBSOLETE", "the top-level 'version' key is obsolete in the Compose Specification", "version")
    for inc in _as_list(model.get("include")):
        inc_path = inc if isinstance(inc, str) else (inc.get("path") if isinstance(inc, Mapping) else None)
        for p in _as_list(inc_path):
            if isinstance(p, str) and not _VAR_RE.search(p) and not _REMOTE_RE.match(p) and not (project_dir / p).exists():
                add("error", "COMPOSE_INCLUDE_MISSING", f"included file '{p}' does not exist", "include")

    services = model.get("services")
    if not isinstance(services, Mapping) or not services:
        add("error", "COMPOSE_NO_SERVICES", "the compose file defines no services", "services")
        return findings
    declared_volumes = model.get("volumes") if isinstance(model.get("volumes"), Mapping) else {}
    declared_networks = model.get("networks") if isinstance(model.get("networks"), Mapping) else {}
    assert isinstance(declared_volumes, Mapping) and isinstance(declared_networks, Mapping)
    container_names: dict[str, str] = {}

    for name, svc in services.items():
        loc = f"services.{name}"
        if not isinstance(svc, Mapping):
            add("error", "COMPOSE_SERVICE_INVALID", "a service must be a mapping", loc)
            continue
        image, build, extends = svc.get("image"), svc.get("build"), svc.get("extends")
        if not image and not build and not extends:
            add("error", "COMPOSE_NO_IMAGE", "service has neither 'image' nor 'build'", loc)
        if image is not None and (not isinstance(image, str) or (not _VAR_RE.search(image) and not _IMAGE_RE.match(image))):
            add("error", "COMPOSE_IMAGE_INVALID", f"invalid image reference {image!r}", f"{loc}.image")
        elif (
            isinstance(image, str)
            and not _VAR_RE.search(image)
            and "@" not in image
            and (":" not in image.rsplit("/", 1)[-1] or image.endswith(":latest"))
        ):
            add("warning", "COMPOSE_IMAGE_UNPINNED", f"image '{image}' is not pinned to a version tag or digest", f"{loc}.image")
        if build is not None:
            _check_build(build, project_dir, loc, add)
        if isinstance(extends, Mapping) and isinstance(extends.get("file"), str):
            ext_file = str(extends["file"])
            if not _VAR_RE.search(ext_file) and not (project_dir / ext_file).is_file():
                add("error", "COMPOSE_EXTENDS_MISSING", f"extends file '{ext_file}' does not exist", f"{loc}.extends")
        elif isinstance(extends, Mapping) and extends.get("service") and str(extends["service"]) not in services:
            add("error", "COMPOSE_EXTENDS_MISSING", f"extends unknown service '{extends['service']}'", f"{loc}.extends")

        # --- security
        if svc.get("privileged") is True and not pol.allow_privileged:
            add("error", "COMPOSE_PRIVILEGED", "privileged: true is not allowed", f"{loc}.privileged")
        net_mode = svc.get("network_mode")
        if isinstance(net_mode, str) and net_mode == "host" and not pol.allow_host_network:
            add("error", "COMPOSE_HOST_NETWORK", "network_mode: host is not allowed", f"{loc}.network_mode")
        if isinstance(net_mode, str) and net_mode.startswith("container:"):
            add(
                "warning",
                "COMPOSE_FOREIGN_NETWORK",
                f"network_mode '{net_mode}' joins a container outside the project",
                f"{loc}.network_mode",
            )
        for ns in ("pid", "ipc", "uts", "userns_mode", "cgroup"):
            if svc.get(ns) == "host" and not pol.allow_host_network:
                add("error", "COMPOSE_HOST_NAMESPACE", f"{ns}: host shares a host namespace", f"{loc}.{ns}")
        for cap in _as_list(svc.get("cap_add")):
            cap_name = str(cap).upper().removeprefix("CAP_")
            if cap_name in DANGEROUS_CAPS and not pol.allow_privileged:
                sev: Literal["error", "warning"] = "error" if cap_name in ("ALL", "SYS_ADMIN", "SYS_MODULE") else "warning"
                add(sev, "COMPOSE_CAPABILITY", f"cap_add {cap_name} grants host-level privileges", f"{loc}.cap_add")
        for opt in _as_list(svc.get("security_opt")):
            text = str(opt).replace("=", ":").lower()
            if (
                text in ("seccomp:unconfined", "apparmor:unconfined", "label:disable", "systempaths:unconfined")
                and not pol.allow_privileged
            ):
                add("error", "COMPOSE_SECURITY_OPT", f"security_opt '{opt}' disables a confinement", f"{loc}.security_opt")
        if svc.get("devices") and not pol.allow_privileged:
            add("warning", "COMPOSE_DEVICES", "host devices are passed into the container", f"{loc}.devices")
        cname = svc.get("container_name")
        if isinstance(cname, str):
            if cname in container_names:
                add(
                    "error", "COMPOSE_CONTAINER_NAME_DUPLICATE", f"container_name '{cname}' is also used by '{container_names[cname]}'", loc
                )
            container_names[cname] = str(name)

        # --- env files / environment
        for env_path, required in _env_files(svc.get("env_file")):
            if _VAR_RE.search(env_path):
                continue
            resolved = resolve_bind_source(env_path, project_dir)
            if resolved is not None and not resolved.is_file():
                add("error" if required else "info", "COMPOSE_ENV_FILE_MISSING", f"env_file '{env_path}' does not exist", f"{loc}.env_file")
            elif resolved is not None and not _inside(resolved.resolve(), project_dir.resolve()):
                add("warning", "COMPOSE_ENV_FILE_OUTSIDE", f"env_file '{env_path}' lies outside the project directory", f"{loc}.env_file")
        for key, value in _env_items(svc.get("environment")):
            if value and _SECRET_KEY_RE.search(key) and not _VAR_RE.search(value):
                add(
                    "warning",
                    "COMPOSE_LITERAL_SECRET",
                    f"environment '{key}' holds a literal secret – use env_file/secrets or ${{VAR}}",
                    f"{loc}.environment",
                )

        # --- references
        for dep in _depends_on(svc.get("depends_on")):
            if dep not in services:
                add("error", "COMPOSE_DEPENDS_UNKNOWN", f"depends_on unknown service '{dep}'", f"{loc}.depends_on")
        for net in _networks_of(svc.get("networks")):
            if net != "default" and net not in declared_networks:
                add("error", "COMPOSE_NETWORK_UNDECLARED", f"network '{net}' is not declared under top-level networks", f"{loc}.networks")
        hc = svc.get("healthcheck")
        if isinstance(hc, Mapping) and "test" in hc:
            test = hc["test"]
            if isinstance(test, list) and test and str(test[0]) not in ("NONE", "CMD", "CMD-SHELL"):
                add(
                    "warning",
                    "COMPOSE_HEALTHCHECK_FORMAT",
                    "healthcheck.test lists must start with CMD, CMD-SHELL or NONE",
                    f"{loc}.healthcheck",
                )

    ports, port_findings = service_ports(services)
    findings.extend(port_findings)
    claimed: list[ComposePort] = []
    for p in ports:
        if p.published is None or p.published_range:
            continue
        clash = next(
            (
                c
                for c in claimed
                if c.published == p.published
                and c.protocol == p.protocol
                and (c.service, c.entry) != (p.service, p.entry)
                and bind_addresses_overlap(c.host_ip, p.host_ip)
            ),
            None,
        )
        if clash is not None:
            add(
                "error",
                "COMPOSE_PORT_DUPLICATE",
                f"host port {p.published}/{p.protocol} is published twice (also by services.{clash.service}.ports '{clash.raw}')",
                f"services.{p.service}.ports",
            )
        claimed.append(p)
        if p.published < 1024:
            add(
                "info",
                "COMPOSE_PRIVILEGED_PORT",
                f"host port {p.published} < 1024 needs root or net.ipv4.ip_unprivileged_port_start",
                f"services.{p.service}.ports",
            )

    volumes, volume_findings = service_volumes(services)
    findings.extend(volume_findings)
    for vol in volumes:
        loc = f"services.{vol.service}.volumes"
        if not vol.target.startswith("/") and not _VAR_RE.search(vol.target):
            add("error", "COMPOSE_VOLUME_TARGET", f"container path '{vol.target}' must be absolute", loc)
        if vol.type == "volume" and vol.source and not _VAR_RE.search(vol.source) and vol.source not in declared_volumes:
            add("error", "COMPOSE_VOLUME_UNDECLARED", f"named volume '{vol.source}' is not declared under top-level volumes", loc)
        if vol.type != "bind" or not vol.source:
            continue
        if is_socket_path(vol.source) and not pol.allow_docker_socket:
            add("error", "COMPOSE_DOCKER_SOCKET", f"mounting the container engine socket '{vol.source}' gives root on the host", loc)
            continue
        host_path = resolve_bind_source(vol.source, project_dir)
        if host_path is None:
            add("info", "COMPOSE_VOLUME_VARIABLE", f"bind source '{vol.source}' uses variables and is not checked", loc)
            continue
        absolute = vol.source.startswith(("/", "~"))
        if absolute and is_sensitive_host_path(str(host_path)) and not pol.allow_sensitive_mounts:
            add("error", "COMPOSE_SENSITIVE_MOUNT", f"bind mount of the host path '{vol.source}' is not allowed", loc)
        elif absolute:
            add(
                "warning" if pol.allow_absolute_binds else "error",
                "COMPOSE_ABSOLUTE_BIND",
                f"bind mount of the absolute host path '{vol.source}'",
                loc,
            )
        else:
            if not _inside(host_path.resolve(), project_dir.resolve()):
                add("warning", "COMPOSE_BIND_OUTSIDE", f"relative bind source '{vol.source}' leaves the project directory", loc)
            if not host_path.exists():
                add("info", "COMPOSE_BIND_MISSING", f"bind source '{vol.source}' does not exist (the engine creates a directory)", loc)

    for top in ("configs", "secrets"):
        entries = model.get(top)
        if isinstance(entries, Mapping):
            for key, spec in entries.items():
                if isinstance(spec, Mapping) and isinstance(spec.get("file"), str) and not _VAR_RE.search(spec["file"]):
                    resolved = resolve_bind_source(spec["file"], project_dir)
                    if resolved is not None and not resolved.exists():
                        add("error", "COMPOSE_FILE_MISSING", f"{top[:-1]} file '{spec['file']}' does not exist", f"{top}.{key}")
    return findings


def _check_build(build: Any, project_dir: Path, loc: str, add: Any) -> None:
    if isinstance(build, str):
        context, dockerfile = build, None
    elif isinstance(build, Mapping):
        context = str(build.get("context") or ".")
        dockerfile = build.get("dockerfile")
    else:
        add("error", "COMPOSE_BUILD_INVALID", "build must be a path or a mapping", f"{loc}.build")
        return
    if re.match(r"^(https?|git|ssh)://|^git@|\.git(#|$)", context) or _VAR_RE.search(context):
        return
    ctx_dir = resolve_bind_source(context, project_dir)
    if ctx_dir is None:
        return
    if not ctx_dir.is_dir():
        add("error", "COMPOSE_BUILD_CONTEXT_MISSING", f"build context '{context}' does not exist", f"{loc}.build")
        return
    if isinstance(dockerfile, str) and not _VAR_RE.search(dockerfile):
        if not (ctx_dir / dockerfile).is_file():
            add("error", "COMPOSE_DOCKERFILE_MISSING", f"dockerfile '{dockerfile}' not found in the build context", f"{loc}.build")
    elif dockerfile is None and not any((ctx_dir / n).is_file() for n in ("Dockerfile", "Containerfile", "dockerfile")):
        add("error", "COMPOSE_DOCKERFILE_MISSING", f"build context '{context}' has no Dockerfile/Containerfile", f"{loc}.build")


_REMOTE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|git@)", re.I)


def remote_references(loaded: LoadedCompose) -> list[str]:
    """``include``/``extends`` entries Compose would download (git, OCI, http) – never handed to the engine check."""
    refs: list[str] = []
    for inc in _as_list(loaded.model.get("include")):
        values = inc if isinstance(inc, str) else (inc.get("path") if isinstance(inc, Mapping) else None)
        refs.extend(v for v in _as_list(values) if isinstance(v, str) and _REMOTE_RE.match(v))
    for svc in loaded.services.values():
        ext = svc.get("extends") if isinstance(svc, Mapping) else None
        if isinstance(ext, Mapping) and isinstance(ext.get("file"), str) and _REMOTE_RE.match(str(ext["file"])):
            refs.append(str(ext["file"]))
    return refs


# ============================================================================================ engines
@dataclass(frozen=True)
class ComposeEngine:
    engine: Engine  # container engine behind the compose CLI
    argv: tuple[str, ...]  # ("docker", "compose") | ("docker-compose",) | ("podman-compose",) | ("podman", "compose")
    version: str
    host: str = LOCAL_HOST
    runtime_ok: bool = False
    runtime_error: str | None = None

    @property
    def label(self) -> str:
        return f"{' '.join(self.argv)} {self.version}".strip()

    @property
    def schema_validation(self) -> bool:
        """``docker compose config`` validates against the Compose Specification schema; podman-compose does not."""
        return self.argv[0].startswith("docker")


_CANDIDATES: tuple[tuple[Engine, tuple[str, ...], tuple[str, ...]], ...] = (
    ("docker", ("docker", "compose"), ("version", "--short")),
    ("docker", ("docker-compose",), ("version", "--short")),
    ("podman", ("podman-compose",), ("version",)),
    ("podman", ("podman", "compose"), ("version",)),
)
_RUNTIME_PROBE: dict[str, list[str]] = {
    "docker": ["docker", "info", "--format", "{{.ServerVersion}}"],
    "podman": ["podman", "info", "--format", "{{.Host.OCIRuntime.Name}}"],
}
_VERSION_RE = re.compile(r"v?(\d+\.\d+(?:\.\d+)?)")


class ComposeEngines:
    """Detects the Compose CLIs on a host (cached) and whether their container engine is usable."""

    def __init__(self, executor: HostExecutor, *, timeout: float = 30.0, cache_seconds: float = 300.0) -> None:
        self.executor = executor
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, list[ComposeEngine]]] = {}
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._cache.clear()

    async def detect(self, host: str | None = None, *, ctx: InfraContext | None = None) -> list[ComposeEngine]:
        target = self.executor.check_host(host)
        async with self._lock:
            cached = self._cache.get(target)
            if cached and time.monotonic() - cached[0] < self.cache_seconds:
                return cached[1]
            found: list[ComposeEngine] = []
            runtime: dict[str, tuple[bool, str | None]] = {}
            have_podman_compose = False
            for engine, argv, version_args in _CANDIDATES:
                if argv == ("podman", "compose") and have_podman_compose:
                    continue  # 'podman compose' only delegates to podman-compose / docker-compose
                res = await self.executor.run([*argv, *version_args], host=host, timeout=self.timeout, ctx=ctx, audit=False)
                if not res.ok:
                    continue
                m = _VERSION_RE.search(res.stdout + " " + res.stderr)
                version = m.group(1) if m else ""
                if argv == ("podman-compose",):
                    have_podman_compose = True
                    pc = re.search(r"podman-compose version:?\s*v?(\d+\.\d+(?:\.\d+)?)", res.stdout + res.stderr)
                    version = pc.group(1) if pc else version
                if engine not in runtime:
                    probe = await self.executor.run(_RUNTIME_PROBE[engine], host=host, timeout=self.timeout, ctx=ctx, audit=False)
                    runtime[engine] = (probe.ok, None if probe.ok else probe.describe(300))
                ok, err = runtime[engine]
                found.append(ComposeEngine(engine, argv, version, target, ok, err))
            self._cache[target] = (time.monotonic(), found)
            return found

    async def for_validation(self, host: str | None = None, *, ctx: InfraContext | None = None) -> ComposeEngine | None:
        engines = await self.detect(host, ctx=ctx)
        schema = [e for e in engines if e.schema_validation]
        if schema:
            return schema[0]
        return engines[0] if engines else None

    async def for_runtime(
        self, host: str | None = None, *, prefer: Sequence[Engine] = ("podman", "docker"), ctx: InfraContext | None = None
    ) -> tuple[ComposeEngine | None, list[str]]:
        engines = await self.detect(host, ctx=ctx)
        reasons = [f"{e.label}: {e.runtime_error}" for e in engines if not e.runtime_ok]
        for wanted in prefer:
            for e in engines:
                if e.engine == wanted and e.runtime_ok:
                    return e, reasons
        if not engines:
            reasons.append("no compose implementation installed (docker compose, docker-compose, podman-compose, podman compose)")
        return None, reasons


# ============================================================================================ validator
EngineCheckStatus = Literal["passed", "failed", "skipped"]


@dataclass
class EngineCheck:
    status: EngineCheckStatus
    engine: str | None = None
    reason: str | None = None
    output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComposeValidationReport:
    path: str
    services: list[str]
    findings: list[Finding]
    engine_check: EngineCheck
    ports: list[ComposePort] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not has_errors(self.findings) and self.engine_check.status != "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "services": self.services,
            "findings": [f.to_dict() for f in self.findings],
            "engine_check": self.engine_check.to_dict(),
            "ports": [p.to_dict() for p in self.ports],
        }

    def render(self, limit: int = 12000) -> str:
        lines = [f"compose_validate {self.path}: {'OK' if self.ok else 'FAILED'} – services: {', '.join(self.services) or '-'}"]
        ec = self.engine_check
        lines.append(f"engine check: {ec.status}" + (f" ({ec.engine})" if ec.engine else "") + (f" – {ec.reason}" if ec.reason else ""))
        if ec.output and ec.status == "failed":
            lines.append(ec.output[-2000:])
        published = sorted({f"{p.host_ip + ':' if p.host_ip else ''}{p.published}/{p.protocol}" for p in self.ports if p.published})
        if published:
            lines.append("requested host ports: " + ", ".join(published))
        lines.extend(f.render() for f in self.findings)
        text = "\n".join(lines)
        return text if len(text) <= limit else text[: limit - 20] + "\n…[truncated]"


def _engine_findings(output: str) -> list[Finding]:
    out: list[Finding] = []
    for line in output.splitlines():
        low = line.lower()
        if "level=warning" in low or low.startswith("warn"):
            msg = re.sub(r'^.*?msg="?', "", line).rstrip('"')
            out.append(Finding("warning", "COMPOSE_ENGINE_WARNING", msg[:500]))
    return out


class ComposeValidator:
    def __init__(self, executor: HostExecutor, engines: ComposeEngines, *, timeout: float = DEFAULT_ENGINE_TIMEOUT) -> None:
        self.executor = executor
        self.engines = engines
        self.timeout = timeout

    async def engine_check(self, path: Path, *, ctx: InfraContext | None = None) -> tuple[EngineCheck, list[Finding]]:
        engine = await self.engines.for_validation(None, ctx=ctx)
        if engine is None:
            return EngineCheck("skipped", reason="no compose implementation installed (docker compose / podman-compose)"), []
        argv = [*engine.argv, "-f", str(path), "config"]
        if engine.schema_validation:
            argv.append("--quiet")
        res: ExecResult = await self.executor.run(argv, timeout=self.timeout, cwd=path.parent, ctx=ctx)
        output = (res.stderr + ("\n" + res.stdout if not engine.schema_validation and not res.ok else "")).strip()
        if res.timed_out:
            return EngineCheck("failed", engine.label, reason=f"timed out after {self.timeout:g} s"), []
        if res.ok:
            reason = None if engine.schema_validation else "podman-compose 'config' does not validate the schema"
            return EngineCheck("passed", engine.label, reason=reason, output=output[-4000:]), _engine_findings(res.stderr)
        return EngineCheck("failed", engine.label, reason=res.describe(800), output=output[-4000:]), []

    async def validate(
        self, path: Path, *, policy: ComposePolicy | None = None, engine: bool = True, ctx: InfraContext | None = None
    ) -> ComposeValidationReport:
        loaded = await asyncio.to_thread(load_compose, path)
        findings = await asyncio.to_thread(validate_structure, loaded, policy)
        ports, _ = service_ports(loaded.services)
        remote = remote_references(loaded)
        for ref in remote:
            findings.append(
                Finding("error", "COMPOSE_REMOTE_REFERENCE", f"remote resource '{ref}' is not fetched by the validator", "include")
            )
        if remote:
            check = EngineCheck("skipped", reason="remote include/extends references are never fetched (SSRF guard)")
        elif engine:
            check, extra = await self.engine_check(path, ctx=ctx)
            findings.extend(extra)
        else:
            check = EngineCheck("skipped", reason="engine check disabled")
        return ComposeValidationReport(str(path), [str(s) for s in loaded.services], findings, check, ports)


__all__ = [
    "MAX_COMPOSE_BYTES",
    "SANDBOX_LABEL",
    "ComposeEngine",
    "ComposeEngines",
    "ComposeLoader",
    "ComposePolicy",
    "ComposePort",
    "ComposeValidationReport",
    "ComposeValidator",
    "ComposeVolume",
    "EngineCheck",
    "LoadedCompose",
    "bind_addresses_overlap",
    "is_sensitive_host_path",
    "is_socket_path",
    "load_compose",
    "load_compose_text",
    "parse_port_entry",
    "parse_volume_entry",
    "remote_references",
    "resolve_bind_source",
    "service_ports",
    "service_volumes",
    "validate_structure",
]
