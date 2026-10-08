"""Daemon-side errors: a :class:`HermclawError` with an explicit HTTP status (``{"error": {...}}`` answers)."""

from __future__ import annotations

from typing import Any

from hermclaw.core.errors import HermclawError


class DaemonError(HermclawError):
    code = "WORKER_DAEMON_ERROR"

    def __init__(self, message: str, *, code: str, status: int = 500, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code=code, details=details)
        self.http_status = status


def not_found(message: str, code: str, **details: Any) -> DaemonError:
    return DaemonError(message, code=code, status=404, details=details)


def conflict(message: str, code: str, **details: Any) -> DaemonError:
    return DaemonError(message, code=code, status=409, details=details)


def bad_request(message: str, code: str, **details: Any) -> DaemonError:
    return DaemonError(message, code=code, status=400, details=details)


def unavailable(message: str, code: str, **details: Any) -> DaemonError:
    return DaemonError(message, code=code, status=503, details=details)
