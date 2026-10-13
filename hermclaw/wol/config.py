"""Wake-on-LAN host configuration checks (P10 10.1).

``hosts.yaml`` is parsed by :mod:`hermclaw.core.config` (types, MAC syntax). This module adds the semantic
checks the wake pipeline depends on, so an unusable host is reported at startup (``hermclaw doctor``,
orchestrator boot) instead of failing on its first wake:

* ``wake_on_lan.enabled`` needs a unicast NIC MAC, an IPv4 broadcast target, a UDP port, positive
  ``ping_timeout_seconds``/``service_timeout_seconds`` and ``max_attempts >= 1``;
* the probe targets (``address``, ``worker_api``, SSH port, ``services[].port``/``http_path``) must pass the
  same validation the probes apply at runtime;
* ``idle_sleep_command`` must be a single command line and, since a host that cannot be woken is never put
  to sleep, should come with Wake-on-LAN.

Issues are data (``severity`` ``error``/``warning``); :func:`assert_wake_config` raises for errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from hermclaw.core.config import HermclawConfig, HostConfig, HostsConfig
from hermclaw.core.errors import ConfigError
from hermclaw.wol.errors import WolError
from hermclaw.wol.magic import normalize_mac, validate_target
from hermclaw.wol.probes import InvalidTarget, build_http_url, validate_address, validate_base_url, validate_port
from hermclaw.workers.registry import DEFAULT_WORKER_PORT

WORKER_ROLES = frozenset({"execution_worker", "model_worker"})
MAX_REASONABLE_ATTEMPTS = 10


class IssueSeverity(StrEnum):
    error = "error"
    warning = "warning"


@dataclass(frozen=True)
class WakeConfigIssue:
    host_id: str
    field: str
    message: str
    severity: IssueSeverity = IssueSeverity.error

    def __str__(self) -> str:
        return f"{self.severity.value}: host '{self.host_id}' {self.field}: {self.message}"


def _hosts(config: HermclawConfig | HostsConfig) -> HostsConfig:
    return config.hosts if isinstance(config, HermclawConfig) else config


def validate_host_wake_config(host: HostConfig) -> list[WakeConfigIssue]:
    """All wake/readiness issues of one host (empty list = usable)."""
    issues: list[WakeConfigIssue] = []

    def err(field: str, message: str) -> None:
        issues.append(WakeConfigIssue(host.id, field, message))

    def warn(field: str, message: str) -> None:
        issues.append(WakeConfigIssue(host.id, field, message, IssueSeverity.warning))

    try:
        validate_address(host.address)
    except InvalidTarget as exc:
        err("address", str(exc))
    if host.role in WORKER_ROLES or host.worker_api:
        try:
            validate_base_url(host.worker_api or f"http://{host.address}:{DEFAULT_WORKER_PORT}")
        except InvalidTarget as exc:
            err("worker_api", str(exc))
    if host.ssh is not None:
        try:
            validate_port(host.ssh.port)
        except InvalidTarget as exc:
            err("ssh.port", str(exc))
    seen: set[str] = set()
    for i, svc in enumerate(host.services):
        field = f"services[{i}]"
        if svc.name in seen:
            warn(f"{field}.name", f"duplicate service name {svc.name!r}")
        seen.add(svc.name)
        try:
            validate_port(svc.port)
            if svc.http_path:
                build_http_url("127.0.0.1", svc.port, svc.http_path)
        except InvalidTarget as exc:
            err(field, str(exc))

    wol = host.wake_on_lan
    if wol.enabled:
        if not wol.mac:
            err("wake_on_lan.mac", "Wake-on-LAN is enabled but no MAC address is configured")
        else:
            try:
                normalize_mac(wol.mac)
            except WolError as exc:
                err("wake_on_lan.mac", exc.message)
        try:
            validate_target(wol.broadcast, wol.port)
        except WolError as exc:
            err("wake_on_lan.broadcast" if exc.details.get("reason") == "invalid_broadcast" else "wake_on_lan.port", exc.message)
        if wol.ping_timeout_seconds <= 0:
            err("wake_on_lan.ping_timeout_seconds", f"must be > 0, got {wol.ping_timeout_seconds}")
        if wol.service_timeout_seconds <= 0:
            err("wake_on_lan.service_timeout_seconds", f"must be > 0, got {wol.service_timeout_seconds}")
        if wol.max_attempts < 1:
            err("wake_on_lan.max_attempts", f"must be >= 1, got {wol.max_attempts}")
        elif wol.max_attempts > MAX_REASONABLE_ATTEMPTS:
            warn("wake_on_lan.max_attempts", f"{wol.max_attempts} packets per wake is unusually many")

    if host.idle_sleep_command is not None:
        cmd = host.idle_sleep_command
        if not cmd.strip() or any(ch in cmd for ch in ("\x00", "\n", "\r")):
            err("idle_sleep_command", "must be one non-empty command line")
        elif not (wol.enabled and wol.mac):
            warn("idle_sleep_command", "host has no usable Wake-on-LAN, idle sleep stays disabled for it")
    return issues


def validate_wake_config(config: HermclawConfig | HostsConfig) -> list[WakeConfigIssue]:
    """Issues of every configured host, in configuration order."""
    out: list[WakeConfigIssue] = []
    for host in _hosts(config).hosts:
        out.extend(validate_host_wake_config(host))
    return out


def assert_wake_config(config: HermclawConfig | HostsConfig) -> list[WakeConfigIssue]:
    """Raise :class:`ConfigError` if any host has an error-level issue; return the warnings otherwise."""
    issues = validate_wake_config(config)
    errors = [i for i in issues if i.severity == IssueSeverity.error]
    if errors:
        raise ConfigError(
            "invalid Wake-on-LAN host configuration: " + "; ".join(str(i) for i in errors),
            details={"issues": [{"host_id": i.host_id, "field": i.field, "message": i.message} for i in errors]},
        )
    return issues
