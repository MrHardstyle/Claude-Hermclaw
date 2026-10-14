"""Host registry for the SSH/admin tools (P26 26.1).

The registry is built from ``hosts.yaml`` (address, ``ssh`` section) and the allow-list
``policies.ssh.allowed_hosts``. Only hosts that are *both* configured with an ``ssh`` section *and*
allow-listed can be reached; everything else is a :class:`~hermclaw.sshtools.errors.SshPolicyError`
(``SSH_HOST_UNKNOWN`` / ``SSH_HOST_NOT_ALLOWED``). Entries are validated when the registry is built so a
typo in the allow-list or an address that could be mistaken for an ssh option fails at startup, not at the
first remote command.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from hermclaw.core.config import HermclawConfig, HostConfig, HostsConfig
from hermclaw.sshtools.errors import (
    SSH_HOST_INVALID,
    SSH_HOST_NOT_ALLOWED,
    SSH_HOST_UNKNOWN,
    SshConfigError,
    SshPolicyError,
)

HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*$")
_SECRET_REF_KINDS = ("file", "cred", "env", "literal")
_CRED_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")  # systemd credential / env var name (no path parts)
# ssh splits UserKnownHostsFile on whitespace and expands '%'/'~' tokens in file options: only plain absolute paths
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@+-]{1,1023}$")


def valid_address(address: str) -> bool:
    """IPv4/IPv6 literal or RFC 1123 host name (never something ssh could parse as an option)."""
    try:
        ipaddress.ip_address(address)
        return True
    except ValueError:
        return bool(_HOSTNAME_RE.match(address))


@dataclass(frozen=True)
class SshHost:
    """One reachable host with its SSH connection settings."""

    id: str
    address: str
    role: str
    user: str
    port: int
    key_ref: str
    known_hosts: Path
    allow_sudo_commands: tuple[str, ...] = ()

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.address}"

    @property
    def known_hosts_name(self) -> str:
        """Name under which the host key is listed in ``known_hosts`` (``[addr]:port`` for non-22 ports)."""
        return self.address if self.port == 22 else f"[{self.address}]:{self.port}"


def _to_host(cfg: HostConfig) -> SshHost:
    ssh = cfg.ssh
    if ssh is None:
        raise SshConfigError(f"host '{cfg.id}' is allow-listed for SSH but has no ssh section", code=SSH_HOST_INVALID)
    problems: list[str] = []
    if not HOST_ID_RE.match(cfg.id):
        problems.append("invalid host id")
    if not valid_address(cfg.address):
        problems.append(f"invalid address {cfg.address!r}")
    if not _USER_RE.match(ssh.user):
        problems.append(f"invalid ssh user {ssh.user!r}")
    if not 1 <= ssh.port <= 65535:
        problems.append(f"invalid ssh port {ssh.port}")
    kind, sep, rest = ssh.key_ref.partition(":")
    if not sep or kind not in _SECRET_REF_KINDS or not rest:
        problems.append("key_ref must be file:<path> or cred:<name> (env:/literal: only outside production)")
    elif kind == "file" and (not SAFE_PATH_RE.match(rest) or ".." in Path(rest).parts):
        problems.append("key_ref file: path must be absolute, without '..', whitespace, '%' or '~'")
    elif kind in ("cred", "env") and not _CRED_NAME_RE.match(rest):
        problems.append(f"key_ref {kind}: name must be a plain name (letters, digits, '._-'), not a path")
    known_hosts = Path(ssh.known_hosts)
    if not SAFE_PATH_RE.match(ssh.known_hosts or "") or ".." in known_hosts.parts:
        problems.append("known_hosts must be an absolute path without '..', whitespace, '%' or '~' (StrictHostKeyChecking=yes needs it)")
    sudo: list[str] = []
    for entry in ssh.allow_sudo_commands:
        stripped = entry.strip()
        if not stripped or any(ch in entry for ch in ("\x00", "\n", "\r")):
            problems.append("allow_sudo_commands entries must be single, non-empty command lines")
        else:
            sudo.append(stripped)  # matched byte-exactly (after an optional 'sudo [-n] ' prefix)
    if problems:
        raise SshConfigError(f"SSH settings of host '{cfg.id}' are invalid: {'; '.join(problems)}", code=SSH_HOST_INVALID)
    return SshHost(
        id=cfg.id,
        address=cfg.address,
        role=cfg.role,
        user=ssh.user,
        port=ssh.port,
        key_ref=ssh.key_ref,
        known_hosts=known_hosts,
        allow_sudo_commands=tuple(sudo),
    )


class HostRegistry:
    """Allow-listed SSH hosts. ``get`` is the single gate every SSH call passes."""

    def __init__(self, hosts: HostsConfig | Iterable[HostConfig], allowed_hosts: Iterable[str]) -> None:
        configured = list(hosts.hosts if isinstance(hosts, HostsConfig) else hosts)
        self._configured: dict[str, HostConfig] = {h.id: h for h in configured}
        self._hosts: dict[str, SshHost] = {}
        for host_id in dict.fromkeys(allowed_hosts):
            cfg = self._configured.get(host_id)
            if cfg is None:
                raise SshConfigError(f"policies.ssh.allowed_hosts names unknown host '{host_id}'", code=SSH_HOST_UNKNOWN)
            self._hosts[host_id] = _to_host(cfg)

    @classmethod
    def from_config(cls, config: HermclawConfig) -> HostRegistry:
        return cls(config.hosts, config.policies.ssh.allowed_hosts)

    def get(self, host_id: str) -> SshHost:
        host = self._hosts.get(host_id)
        if host is not None:
            return host
        shown = host_id if HOST_ID_RE.match(host_id) else repr(host_id[:40])
        if host_id in self._configured:
            raise SshPolicyError(f"host '{shown}' is not in policies.ssh.allowed_hosts", code=SSH_HOST_NOT_ALLOWED, details={"host": shown})
        raise SshPolicyError(f"unknown host '{shown}'", code=SSH_HOST_UNKNOWN, details={"host": shown})

    def is_allowed(self, host_id: str) -> bool:
        return host_id in self._hosts

    def hosts(self) -> list[SshHost]:
        return list(self._hosts.values())

    def ids(self) -> list[str]:
        return list(self._hosts)


__all__ = ["HOST_ID_RE", "SAFE_PATH_RE", "HostRegistry", "SshHost", "valid_address"]
