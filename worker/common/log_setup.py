"""Structured JSON logging for the worker daemons (redaction via :mod:`hermclaw.core.redaction`).

The worker token is registered with the global redactor when the token file is loaded
(:func:`hermclaw.workers.auth.validate_tokens`), so it can never appear in a log line.
"""

from __future__ import annotations

from hermclaw.core.logging import configure_logging
from worker.common.settings import WorkerDaemonSettings


def service_name(settings: WorkerDaemonSettings) -> str:
    return f"hermclaw-worker-{settings.kind.value}"


def configure_daemon_logging(settings: WorkerDaemonSettings) -> None:
    configure_logging(level=settings.log_level, service=service_name(settings), json_output=settings.log_json)
