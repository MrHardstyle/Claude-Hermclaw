"""Egress gateway of the execution worker: registry-only network for dependency installs (D-014).

Layout (rootless podman, user ``hermclaw`` on the execution worker)::

    install sandbox ──(internal network, no route)──▶ hermclaw-egress-proxy ──(outbound network)──▶ allow-listed registries

* ``policies.sandbox.egress.internal_network`` is created with ``--internal --disable-dns``: a container on it has no
  route outside, it can only reach the proxy at the fixed ``proxy_ip`` (no aardvark-dns needed).
* The proxy container runs ``egress_proxy.py`` (standard library) with the generic toolchain image's ``python3``,
  read-only, without capabilities, as ``nobody``, and is the only container on both networks.
* :meth:`EgressGateway.ensure` creates what is missing and recreates the proxy when its configuration changed
  (label ``hermclaw.egress.config``); it runs before every install, so a reboot or a crashed proxy heals itself.

The proxy is not a sandbox command container: it carries its own label (``hermclaw.egress=proxy``) and never the
managed label, so container recovery does not kill it.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from hermclaw.core.config import EgressPolicy
from hermclaw.core.errors import ConfigError
from hermclaw.core.logging import get_logger

log = get_logger(__name__)

PROXY_CONTAINER = "hermclaw-egress-proxy"  # default of policies.sandbox.egress.proxy_container
PROXY_SCRIPT = Path(__file__).with_name("egress_proxy.py")
PROXY_MOUNT = "/opt/egress_proxy.py"
LABEL_EGRESS = "hermclaw.egress"
LABEL_CONFIG = "hermclaw.egress.config"
RESOLV_CONF = Path("/etc/resolv.conf")

EngineExec = Callable[..., Awaitable[tuple[int, str, str]]]


def host_nameservers(resolv_conf: Path = RESOLV_CONF) -> list[str]:
    """Non-loopback nameservers of the host, IPv4 first (the rootless network namespace may lack IPv6)."""
    try:
        lines = resolv_conf.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            try:
                ip = ipaddress.ip_address(parts[1].split("%", 1)[0])
            except ValueError:
                continue
            if not ip.is_loopback and not ip.is_link_local and ip not in found:
                found.append(ip)
    return [str(ip) for ip in sorted(found, key=lambda ip: ip.version)]


@dataclass(frozen=True)
class EgressStatus:
    ready: bool
    proxy_url: str
    detail: str


class EgressGateway:
    def __init__(
        self,
        policy: EgressPolicy,
        *,
        exec_fn: EngineExec,
        executable: str = "podman",
        script: Path = PROXY_SCRIPT,
        resolv_conf: Path = RESOLV_CONF,
    ) -> None:
        self.policy, self.exec_fn, self.executable = policy, exec_fn, executable
        self.script, self.resolv_conf = script, resolv_conf
        self._lock = asyncio.Lock()

    @property
    def proxy_url(self) -> str:
        return self.policy.proxy_url

    def nameservers(self) -> list[str]:
        servers = list(self.policy.dns) or host_nameservers(self.resolv_conf)
        if not servers:
            raise ConfigError(
                "egress proxy: no DNS resolver (set policies.sandbox.egress.dns or a non-loopback nameserver in /etc/resolv.conf)",
                code="EGRESS_DNS_MISSING",
            )
        return servers

    def network_argvs(self) -> list[tuple[str, list[str]]]:
        p, create = self.policy, [self.executable, "network", "create", "--disable-dns"]
        return [
            (p.internal_network, [*create, "--internal", f"--subnet={p.internal_subnet}", p.internal_network]),
            (p.outbound_network, [*create, f"--subnet={p.outbound_subnet}", p.outbound_network]),
        ]

    def proxy_args(self) -> list[str]:
        args = ["--listen", f"0.0.0.0:{self.policy.proxy_port}"]
        for host in self.policy.allowed_hosts:
            args += ["--allow", host.strip()]
        return args

    def config_fingerprint(self) -> str:
        p = self.policy
        material = json.dumps(
            [self.proxy_args(), p.proxy_ip, p.proxy_image, p.internal_network, p.outbound_network, self.nameservers()], sort_keys=True
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def proxy_argv(self) -> list[str]:
        p = self.policy
        dns = [f"--dns={server}" for server in self.nameservers()]
        return [
            self.executable,
            "run",
            "--detach",
            f"--name={self.policy.proxy_container}",
            f"--label={LABEL_EGRESS}=proxy",
            f"--label={LABEL_CONFIG}={self.config_fingerprint()}",
            f"--network={p.internal_network}:ip={p.proxy_ip}",
            f"--network={p.outbound_network}",
            *dns,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--memory=256m",
            "--pids-limit=128",
            "--http-proxy=false",
            "--restart=on-failure",
            "--log-driver=k8s-file",
            "--log-opt=max-size=10mb",
            f"--volume={self.script}:{PROXY_MOUNT}:ro",
            "--entrypoint=python3",
            p.proxy_image,
            "-u",
            PROXY_MOUNT,
            *self.proxy_args(),
        ]

    async def _exec(self, argv: Sequence[str], limit_seconds: float = 60.0) -> tuple[int, str, str]:
        return await self.exec_fn(list(argv), timeout_seconds=limit_seconds)

    async def _proxy_state(self) -> tuple[bool, str | None]:
        """(running, config label) of the proxy container; (False, None) when it does not exist."""
        fmt = '{{.State.Running}} {{index .Config.Labels "' + LABEL_CONFIG + '"}}'
        rc, out, _err = await self._exec([self.executable, "container", "inspect", "--format", fmt, self.policy.proxy_container])
        if rc != 0:
            return False, None
        running, _, label = out.strip().partition(" ")
        return running == "true", (label or None)

    async def ensure(self) -> str:
        """Networks and a running, current proxy container; returns the proxy URL for the install sandbox."""
        if not self.policy.enabled:
            raise ConfigError("dependency installs are disabled (policies.sandbox.egress.enabled: false)", code="EGRESS_DISABLED")
        if not self.script.is_file():
            raise ConfigError(f"egress proxy script missing: {self.script}", code="EGRESS_PROXY_MISSING")
        async with self._lock:
            for name, create in self.network_argvs():
                rc, _out, _err = await self._exec([self.executable, "network", "exists", name])
                if rc != 0:
                    rc, _out, err = await self._exec(create)
                    if rc != 0 and "already exists" not in err:
                        raise ConfigError(f"cannot create podman network {name}: {err.strip()[:300]}", code="EGRESS_NETWORK_FAILED")
                    log.info("egress network created", extra={"network": name})
            running, label = await self._proxy_state()
            wanted = self.config_fingerprint()
            if running and label == wanted:
                return self.proxy_url
            if label is not None or running:
                await self._exec([self.executable, "rm", "-f", "-t", "0", self.policy.proxy_container])
            rc, _out, err = await self._exec(self.proxy_argv(), limit_seconds=300.0)
            if rc != 0:
                raise ConfigError(f"cannot start the egress proxy: {err.strip()[:300]}", code="EGRESS_PROXY_FAILED")
            log.info("egress proxy started", extra={"proxy": self.proxy_url, "allowed_hosts": self.policy.allowed_hosts})
            await self._wait_listening()
            return self.proxy_url

    async def _wait_listening(self, attempts: int = 20) -> None:
        for _ in range(attempts):
            rc, out, _err = await self._exec([self.executable, "logs", self.policy.proxy_container], limit_seconds=15.0)
            if rc == 0 and '"decision": "listening"' in out:
                return
            running, _label = await self._proxy_state()
            if not running:
                break
            await asyncio.sleep(0.25)
        _rc, out, err = await self._exec([self.executable, "logs", "--tail", "20", self.policy.proxy_container], limit_seconds=15.0)
        raise ConfigError(f"egress proxy did not come up: {(err or out).strip()[-300:]}", code="EGRESS_PROXY_FAILED")

    async def status(self) -> EgressStatus:
        if not self.policy.enabled:
            return EgressStatus(False, self.proxy_url, "disabled")
        running, label = await self._proxy_state()
        if not running:
            return EgressStatus(False, self.proxy_url, "proxy not running (started on the first install)")
        try:
            current = label == self.config_fingerprint()
        except ConfigError as exc:
            return EgressStatus(False, self.proxy_url, exc.message)
        return EgressStatus(current, self.proxy_url, "running" if current else "running with an outdated configuration")

    async def decisions(self, since: str | None = None, limit: int = 200) -> list[dict[str, object]]:
        """The proxy's JSON decision lines (``podman logs``), newest last; unparsable lines are skipped."""
        argv = [self.executable, "logs", "--tail", str(limit)]
        if since:
            argv += ["--since", since]
        rc, out, _err = await self._exec([*argv, self.policy.proxy_container], limit_seconds=15.0)
        if rc != 0:
            return []
        lines: list[dict[str, object]] = []
        for raw in out.splitlines():
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if isinstance(item, dict):
                lines.append(item)
        return lines


__all__ = ["PROXY_CONTAINER", "EgressGateway", "EgressStatus", "host_nameservers"]
