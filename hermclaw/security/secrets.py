"""Secret references (Bauplan §35, research 20261008-020-systemd-credentials).

Reference formats:
- ``cred:<name>``  – systemd credential: ``$CREDENTIALS_DIRECTORY/<name>``; fallback ``/etc/hermclaw/secrets/<name>``
- ``file:<path>``  – file content (must not be group/world readable in production)
- ``env:<NAME>``   – environment variable (only allowed outside production)
- ``literal:<v>``  – literal value (tests only)
Resolved secrets are registered with the default Redactor so they never appear in logs/events/prompts.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from hermclaw.core.errors import ConfigError
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.core.settings import get_settings

SECRETS_DIR = Path("/etc/hermclaw/secrets")


class SecretStore:
    def __init__(self, *, env: str | None = None, secrets_dir: Path = SECRETS_DIR) -> None:
        self.env = env or get_settings().env
        self.secrets_dir = secrets_dir

    def resolve(self, ref: str, *, required: bool = True) -> str | None:
        value = self._resolve(ref)
        if value is None:
            if required:
                raise ConfigError(f"secret '{ref.split(':', 1)[0]}:…' could not be resolved", code="SECRET_MISSING")
            return None
        value = value.strip()
        DEFAULT_REDACTOR.add_literal(value)
        return value

    def _resolve(self, ref: str) -> str | None:
        kind, _, rest = ref.partition(":")
        if kind == "cred":
            cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
            for base in ([Path(cred_dir)] if cred_dir else []) + [self.secrets_dir]:
                p = base / rest
                if p.is_file():
                    return p.read_text(encoding="utf-8")
            return None
        if kind == "file":
            p = Path(rest)
            if not p.is_file():
                return None
            if self.env == "production":
                mode = p.stat().st_mode
                if mode & (stat.S_IRWXG | stat.S_IRWXO):
                    raise ConfigError(f"secret file {p} must not be group/world accessible (chmod 0400)", code="SECRET_PERMISSIONS")
            return p.read_text(encoding="utf-8")
        if kind == "env":
            if self.env == "production":
                raise ConfigError("env: secret references are not allowed in production", code="SECRET_POLICY")
            return os.environ.get(rest)
        if kind == "literal":
            if self.env == "production":
                raise ConfigError("literal secrets are not allowed in production", code="SECRET_POLICY")
            return rest
        raise ConfigError(f"unknown secret reference type '{kind}'", code="SECRET_REF_INVALID")


def resolve_secret(ref: str, *, required: bool = True) -> str | None:
    return SecretStore().resolve(ref, required=required)
