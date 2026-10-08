"""Minimal secret-reference resolver used by the Git engine (GitLab token, SSH key path).

Supported references (Bauplan §35, research 20261008-020):

* ``env:NAME``   – value of environment variable ``NAME``
* ``file:/path`` – content of an absolute file path (first line semantics: surrounding whitespace stripped)
* ``cred:name``  – systemd credential ``$CREDENTIALS_DIRECTORY/name``; fallback ``/etc/hermclaw/secrets/name``

Every resolved value is registered with the process-wide :data:`DEFAULT_REDACTOR` so it is masked in logs,
events and prompts. Error messages never contain secret values. A shared ``SecretStore`` will replace this
module later; the public functions keep their signatures so the swap is mechanical.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.gitops.errors import SecretRefError

DEFAULT_SECRETS_DIR = Path("/etc/hermclaw/secrets")
_CRED_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_SECRET_BYTES = 1_000_000


def _split(ref: str) -> tuple[str, str]:
    kind, sep, rest = ref.strip().partition(":")
    if not sep or not rest:
        raise SecretRefError("secret reference must look like 'env:NAME', 'file:/path' or 'cred:name'", details={"kind": kind})
    return kind, rest


def secret_file_path(ref: str, *, environ: Mapping[str, str] | None = None, secrets_dir: Path = DEFAULT_SECRETS_DIR) -> Path:
    """Return the file that backs a ``file:`` or ``cred:`` reference (used for SSH keys, which need a path)."""
    env = os.environ if environ is None else environ
    kind, rest = _split(ref)
    if kind == "file":
        path = Path(rest)
        if not path.is_absolute():
            raise SecretRefError("file: secret references must be absolute paths", details={"kind": kind})
        return path
    if kind == "cred":
        if not _CRED_NAME.match(rest):
            raise SecretRefError("invalid credential name in cred: reference", details={"kind": kind})
        cred_dir = env.get("CREDENTIALS_DIRECTORY")
        if cred_dir:
            candidate = Path(cred_dir) / rest
            if candidate.exists():
                return candidate
        return secrets_dir / rest
    raise SecretRefError(f"secret reference kind '{kind}' does not reference a file", details={"kind": kind})


def read_secret(ref: str, *, environ: Mapping[str, str] | None = None, secrets_dir: Path = DEFAULT_SECRETS_DIR) -> str:
    """Resolve a secret reference to its value (synchronous; files are tiny)."""
    env = os.environ if environ is None else environ
    kind, rest = _split(ref)
    if kind == "env":
        if not _ENV_NAME.match(rest):
            raise SecretRefError("invalid environment variable name in env: reference", details={"kind": kind})
        value = env.get(rest)
        if value is None or not value.strip():
            raise SecretRefError(f"environment variable '{rest}' referenced by secret ref is not set", details={"kind": kind})
        value = value.strip()
    elif kind in ("file", "cred"):
        path = secret_file_path(ref, environ=env, secrets_dir=secrets_dir)
        try:
            size = path.stat().st_size
            if size > MAX_SECRET_BYTES:
                raise SecretRefError("secret file is too large", details={"kind": kind, "path": str(path)})
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise SecretRefError("secret file referenced by secret ref does not exist", details={"kind": kind, "path": str(path)}) from exc
        except PermissionError as exc:
            raise SecretRefError("secret file referenced by secret ref is not readable", details={"kind": kind, "path": str(path)}) from exc
        except UnicodeDecodeError as exc:
            raise SecretRefError("secret file is not valid UTF-8", details={"kind": kind, "path": str(path)}) from exc
        if not value:
            raise SecretRefError("secret file referenced by secret ref is empty", details={"kind": kind, "path": str(path)})
    else:
        raise SecretRefError(f"unsupported secret reference kind '{kind}'", details={"kind": kind})
    DEFAULT_REDACTOR.add_literal(value)
    return value
