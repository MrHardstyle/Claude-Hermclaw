"""Runtime-controlled SSH/admin tools (Bauplan §29, P26): no free SSH from the coder shell.

* :mod:`~hermclaw.sshtools.registry` – allow-listed hosts (26.1)
* :mod:`~hermclaw.sshtools.keys` – key references, key/known_hosts checks, key generation (26.2)
* :mod:`~hermclaw.sshtools.policy` / :mod:`~hermclaw.sshtools.approvals` – command classes, approvals (26.3/26.5)
* :mod:`~hermclaw.sshtools.client` – OpenSSH runner with timeout/kill, output caps, redaction (26.7)
* :mod:`~hermclaw.sshtools.tools` – ``ssh_read``/``ssh_command``/``ssh_upload``/``ssh_service_status`` + audit (26.4–26.6)
* :mod:`~hermclaw.sshtools.provider` – adapter for the tool engine's operations-tool protocol
* :mod:`~hermclaw.sshtools.separation` – capability gate and sandbox isolation checks (26.8)
"""

from hermclaw.sshtools.client import SshResult, SshRunner
from hermclaw.sshtools.errors import SshConfigError, SshPolicyError, SshTimeoutError, SshTransportError
from hermclaw.sshtools.keys import KeyManager, authorized_keys_entry, generate_keypair
from hermclaw.sshtools.policy import SshClassification, SshCommandPolicy, SshDecision, Verdict, approval_token
from hermclaw.sshtools.registry import HostRegistry, SshHost
from hermclaw.sshtools.separation import assert_ssh_allowed, sandbox_isolation_issues
from hermclaw.sshtools.tools import SshCallContext, SshOutcome, SshToolResult, SshTools

__all__ = [
    "HostRegistry",
    "KeyManager",
    "SshCallContext",
    "SshClassification",
    "SshCommandPolicy",
    "SshConfigError",
    "SshDecision",
    "SshHost",
    "SshOutcome",
    "SshPolicyError",
    "SshResult",
    "SshRunner",
    "SshTimeoutError",
    "SshToolResult",
    "SshTools",
    "SshTransportError",
    "Verdict",
    "approval_token",
    "assert_ssh_allowed",
    "authorized_keys_entry",
    "generate_keypair",
    "sandbox_isolation_issues",
]
