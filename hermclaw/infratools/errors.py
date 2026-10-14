"""Errors, stable codes and findings of the container/proxy tools (Bauplan §31, P28)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from hermclaw.core.errors import HermclawError

# ---------------------------------------------------------------------------------------------- codes
INFRA_INVALID_ARGUMENT = "INFRA_INVALID_ARGUMENT"
INFRA_PATH_INVALID = "INFRA_PATH_INVALID"
INFRA_HOST_UNKNOWN = "INFRA_HOST_UNKNOWN"
INFRA_REMOTE_UNAVAILABLE = "INFRA_REMOTE_UNAVAILABLE"
INFRA_ENGINE_UNAVAILABLE = "INFRA_ENGINE_UNAVAILABLE"
INFRA_COMMAND_FAILED = "INFRA_COMMAND_FAILED"
INFRA_TIMEOUT = "INFRA_TIMEOUT"
INFRA_OUTPUT_INVALID = "INFRA_OUTPUT_INVALID"
INFRA_CONTAINER_NOT_FOUND = "INFRA_CONTAINER_NOT_FOUND"
INFRA_COMPOSE_INVALID = "INFRA_COMPOSE_INVALID"
INFRA_COMPOSE_REFUSED = "INFRA_COMPOSE_REFUSED"
INFRA_SANDBOX_UNAVAILABLE = "INFRA_SANDBOX_UNAVAILABLE"
INFRA_SANDBOX_FAILED = "INFRA_SANDBOX_FAILED"
INFRA_NGINX_UNAVAILABLE = "INFRA_NGINX_UNAVAILABLE"
INFRA_NGINX_INVALID = "INFRA_NGINX_INVALID"
INFRA_PROXY_INVALID = "INFRA_PROXY_INVALID"
INFRA_PORT_COLLISION = "INFRA_PORT_COLLISION"
INFRA_ROLLBACK_UNKNOWN = "INFRA_ROLLBACK_UNKNOWN"
INFRA_ROLLBACK_INVALID = "INFRA_ROLLBACK_INVALID"
INFRA_ROLLBACK_CORRUPT = "INFRA_ROLLBACK_CORRUPT"
INFRA_ROLLBACK_FAILED = "INFRA_ROLLBACK_FAILED"
INFRA_DEPLOY_FAILED = "INFRA_DEPLOY_FAILED"
INFRA_STORE_UNAVAILABLE = "INFRA_STORE_UNAVAILABLE"


class InfraToolError(HermclawError):
    """Raised by the container/proxy tools; ``code`` is one of the ``INFRA_*`` constants."""

    code = "INFRA_ERROR"
    http_status = 400


# ---------------------------------------------------------------------------------------------- findings
Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class Finding:
    """One validation result. ``code`` is stable (e.g. ``COMPOSE_PRIVILEGED``); ``location`` is human readable."""

    severity: Severity
    code: str
    message: str
    location: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def render(self) -> str:
        where = self.location or ""
        if self.line is not None:
            where = f"{where}:{self.line}" if where else f"line {self.line}"
        prefix = f"[{self.severity}] {self.code}"
        return f"{prefix} ({where}): {self.message}" if where else f"{prefix}: {self.message}"


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)


__all__ = [
    "INFRA_COMMAND_FAILED",
    "INFRA_COMPOSE_INVALID",
    "INFRA_COMPOSE_REFUSED",
    "INFRA_CONTAINER_NOT_FOUND",
    "INFRA_DEPLOY_FAILED",
    "INFRA_ENGINE_UNAVAILABLE",
    "INFRA_HOST_UNKNOWN",
    "INFRA_INVALID_ARGUMENT",
    "INFRA_NGINX_INVALID",
    "INFRA_NGINX_UNAVAILABLE",
    "INFRA_OUTPUT_INVALID",
    "INFRA_PATH_INVALID",
    "INFRA_PORT_COLLISION",
    "INFRA_PROXY_INVALID",
    "INFRA_REMOTE_UNAVAILABLE",
    "INFRA_ROLLBACK_CORRUPT",
    "INFRA_ROLLBACK_FAILED",
    "INFRA_ROLLBACK_INVALID",
    "INFRA_ROLLBACK_UNKNOWN",
    "INFRA_SANDBOX_FAILED",
    "INFRA_SANDBOX_UNAVAILABLE",
    "INFRA_STORE_UNAVAILABLE",
    "INFRA_TIMEOUT",
    "Finding",
    "InfraToolError",
    "Severity",
    "has_errors",
]
