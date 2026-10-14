"""Stable error codes and exceptions of the SSH/admin tools (Bauplan §29, P26)."""

from __future__ import annotations

from hermclaw.core.errors import ConfigError, ExternalServiceError, PolicyViolation

# ----------------------------------------------------------------------------- registry / keys (26.1, 26.2)
SSH_HOST_UNKNOWN = "SSH_HOST_UNKNOWN"
SSH_HOST_NOT_ALLOWED = "SSH_HOST_NOT_ALLOWED"
SSH_HOST_INVALID = "SSH_HOST_INVALID"
SSH_KEY_MISSING = "SSH_KEY_MISSING"
SSH_KEY_PERMISSIONS = "SSH_KEY_PERMISSIONS"
SSH_KEY_INVALID = "SSH_KEY_INVALID"
SSH_KNOWN_HOSTS_MISSING = "SSH_KNOWN_HOSTS_MISSING"
SSH_KNOWN_HOSTS_INSECURE = "SSH_KNOWN_HOSTS_INSECURE"
SSH_KNOWN_HOSTS_NO_ENTRY = "SSH_KNOWN_HOSTS_NO_ENTRY"
SSH_RUNTIME_DIR_INSECURE = "SSH_RUNTIME_DIR_INSECURE"
# ----------------------------------------------------------------------------- command policy (26.3)
SSH_COMMAND_FORBIDDEN = "SSH_COMMAND_FORBIDDEN"
SSH_COMMAND_INVALID = "SSH_COMMAND_INVALID"
SSH_NOT_READ_ONLY = "SSH_NOT_READ_ONLY"
SSH_COMPOUND_NOT_ALLOWED = "SSH_COMPOUND_NOT_ALLOWED"
SSH_SUDO_NOT_ALLOWED = "SSH_SUDO_NOT_ALLOWED"
SSH_APPROVAL_REQUIRED = "SSH_APPROVAL_REQUIRED"
SSH_APPROVAL_INVALID = "SSH_APPROVAL_INVALID"
SSH_APPROVAL_PENDING = "SSH_APPROVAL_PENDING"
# ----------------------------------------------------------------------------- separation (26.8)
SSH_CAPABILITY_DENIED = "SSH_CAPABILITY_DENIED"
# ----------------------------------------------------------------------------- execution (26.4, 26.5, 26.7)
SSH_TIMEOUT = "SSH_TIMEOUT"
SSH_TRANSPORT_ERROR = "SSH_TRANSPORT_ERROR"
SSH_CLIENT_MISSING = "SSH_CLIENT_MISSING"
SSH_COMMAND_FAILED = "SSH_COMMAND_FAILED"
SSH_UPLOAD_INVALID = "SSH_UPLOAD_INVALID"
SSH_SERVICE_UNAVAILABLE = "SSH_SERVICE_UNAVAILABLE"
SSH_UNIT_INVALID = "SSH_UNIT_INVALID"

#: exit status the OpenSSH client uses for its own (connection/authentication) errors
SSH_TRANSPORT_EXIT = 255


class SshPolicyError(PolicyViolation):
    """A host, command, key or capability is not allowed by the SSH policy."""

    code = "SSH_POLICY_VIOLATION"


class SshConfigError(ConfigError):
    """SSH configuration (host entry, key file, known_hosts, runtime dir) is missing or insecure."""

    code = "SSH_CONFIG_INVALID"


class SshTransportError(ExternalServiceError):
    """The ssh client could not be started or failed before the remote command ran."""

    code = SSH_TRANSPORT_ERROR


class SshTimeoutError(TimeoutError):
    """The remote command exceeded its timeout; the local ssh client was killed."""

    code = SSH_TIMEOUT
