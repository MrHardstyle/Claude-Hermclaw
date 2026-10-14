"""Sandbox vs. host separation for SSH/admin tools (P26 26.8, Bauplan §27/§29).

* SSH tools are only callable from steps whose capability is ``ssh`` or ``deploy``
  (:func:`assert_ssh_allowed`); the coder of an ``implement`` step has no remote access at all and in
  particular no free SSH from its sandbox shell.
* The execution sandbox never receives SSH material: keys live outside the job workspaces (which are what
  gets mounted into the sandbox), materialised ``cred:`` keys exist only for the lifetime of one ssh process
  in a private 0700 runtime dir on the orchestrator, ssh runs with a minimal environment, and agent sockets
  (``SSH_AUTH_SOCK``) or ``GIT_SSH*`` overrides must never be part of the sandbox environment.
  :func:`sandbox_isolation_issues` checks these invariants for a configuration (startup self-check, tests).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from hermclaw.core.config import HermclawConfig
from hermclaw.sshtools.errors import SSH_CAPABILITY_DENIED, SshPolicyError

SSH_CAPABILITIES = frozenset({"ssh", "deploy"})
SSH_ENV_VARS = frozenset(
    {"SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT"}
)


def ssh_allowed(step_capability: str | None) -> bool:
    return step_capability in SSH_CAPABILITIES


def assert_ssh_allowed(step_capability: str | None) -> None:
    """Raise :class:`SshPolicyError` (``SSH_CAPABILITY_DENIED``) unless the step may use SSH tools."""
    if not ssh_allowed(step_capability):
        raise SshPolicyError(
            f"SSH tools are only available to steps with capability {sorted(SSH_CAPABILITIES)}, not {step_capability!r}",
            code=SSH_CAPABILITY_DENIED,
            details={"capability": step_capability},
        )


def scrub_ssh_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Copy of ``env`` without agent sockets / ssh command overrides (for any process near the sandbox)."""
    return {k: v for k, v in env.items() if k not in SSH_ENV_VARS and not k.startswith("SSH_")}


def _key_path(key_ref: str) -> Path | None:
    kind, _, rest = key_ref.partition(":")
    return Path(rest) if kind == "file" and rest else None


def sandbox_isolation_issues(config: HermclawConfig, *, workspaces_dir: Path, extra_paths: Iterable[Path] = ()) -> list[str]:
    """Violations of the sandbox/host separation for ``config`` (empty list = fine)."""
    issues: list[str] = []
    leaked = sorted(v for v in config.policies.sandbox.env_allowlist if v in SSH_ENV_VARS or v.startswith("SSH_"))
    if leaked:
        issues.append(f"policies.sandbox.env_allowlist passes SSH variables into the sandbox: {', '.join(leaked)}")
    root = workspaces_dir.resolve()
    paths: list[tuple[str, Path]] = []
    for host in config.hosts.hosts:
        if host.ssh is None:
            continue
        key = _key_path(host.ssh.key_ref)
        if key is not None:
            paths.append((f"key of host '{host.id}'", key))
        if host.ssh.known_hosts:
            paths.append((f"known_hosts of host '{host.id}'", Path(host.ssh.known_hosts)))
    paths += [("runtime path", Path(p)) for p in extra_paths]
    for label, path in paths:
        resolved = path.resolve()
        if resolved == root or root in resolved.parents:
            issues.append(f"{label} ({path}) lies inside the sandbox-mounted workspaces dir {workspaces_dir}")
    capabilities = {c.name for c in config.capabilities.capabilities}
    for name in SSH_CAPABILITIES & capabilities:
        cap = config.capabilities.get(name)
        if cap.worker_kind == "execution":
            issues.append(f"capability '{name}' runs on the execution worker; SSH tools must run on the orchestrator")
    return issues


__all__ = ["SSH_CAPABILITIES", "SSH_ENV_VARS", "assert_ssh_allowed", "sandbox_isolation_issues", "scrub_ssh_environment", "ssh_allowed"]
