"""Remote URL validation and redaction.

Only plain transports are accepted (ssh, scp-like ``user@host:path``, https/http, file and absolute local
paths). ``transport::address`` forms (e.g. ``ext::sh -c …``) and option-looking values are rejected, and URLs
with embedded passwords/tokens are refused: credentials are always secret references, never part of a URL.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from hermclaw.gitops.errors import InvalidRemoteUrl

ALLOWED_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git", "https", "http", "file"})
_SCP_LIKE = re.compile(r"^(?:[A-Za-z0-9._~-]+@)?[A-Za-z0-9.-]+:(?!//)[^\s]+$")
_URL_IN_TEXT = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^:@/\s]+):(?P<pw>[^@/\s]+)@", re.I)


def validate_remote_url(url: str) -> str:
    value = url.strip()
    if not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise InvalidRemoteUrl("remote URL must be non-empty and contain no whitespace/control characters")
    if value.startswith("-"):
        raise InvalidRemoteUrl("remote URL must not start with '-'")
    if "::" in value.split("/", 1)[0]:
        raise InvalidRemoteUrl("transport::address remote helpers are not allowed")
    if "://" in value:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise InvalidRemoteUrl(f"remote URL scheme '{scheme}' is not allowed", details={"allowed": sorted(ALLOWED_SCHEMES)})
        if parts.password:
            raise InvalidRemoteUrl("credentials must not be embedded in remote URLs; use a secret reference")
        if scheme != "file" and not parts.hostname:
            raise InvalidRemoteUrl("remote URL has no host")
        if scheme == "file" and not parts.path.startswith("/"):
            raise InvalidRemoteUrl("file:// URLs must use an absolute path")
        return value
    if value.startswith("/"):
        return value
    if _SCP_LIKE.match(value):
        return value
    raise InvalidRemoteUrl("unsupported remote URL format (use ssh://, git@host:path, https://, file:// or an absolute path)")


def redact_url(url: str) -> str:
    """Remove any password/token from URLs inside ``url`` (also works on free text such as stderr)."""
    return _URL_IN_TEXT.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:***@", url)


def strip_userinfo(url: str) -> str:
    if "://" not in url:
        return url
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def project_path_from_url(url: str) -> str | None:
    """GitLab ``namespace/project`` derived from a remote URL (used when no project id is registered)."""
    value = url.strip()
    if "://" in value:
        path = urlsplit(value).path
    elif _SCP_LIKE.match(value):
        path = value.split(":", 1)[1]
    else:
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path or None
