"""Backup events (``backup.*``) and structured log entries.

Events are best effort: a backup must still be written (and reported in the log/journal) when the event store is
unreachable – e.g. a restore test against a broken production database. Payloads go through the event store's
redaction; they never contain connection passwords or file contents.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR

log = get_logger("hermclaw.backup")

BACKUP_STARTED = "backup.started"
BACKUP_COMPLETED = "backup.completed"
BACKUP_FAILED = "backup.failed"
BACKUP_PRUNED = "backup.pruned"
BACKUP_VERIFIED = "backup.verified"
BACKUP_VERIFY_FAILED = "backup.verify.failed"
RESTORE_TEST_PASSED = "backup.restore_test.passed"
RESTORE_TEST_FAILED = "backup.restore_test.failed"
RESTORE_FULL_COMPLETED = "backup.restore.completed"
RESTORE_FULL_FAILED = "backup.restore.failed"
SOURCE_TYPE = "backup"

_LEVEL = {Severity.info: logging.INFO, Severity.warning: logging.WARNING, Severity.error: logging.ERROR}


async def emit(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    event_type: str,
    *,
    set_id: str | None,
    payload: dict[str, Any],
    severity: Severity = Severity.info,
) -> bool:
    """Log the event and append it to the event store when a sessionmaker is given. Returns True if stored."""
    extra = DEFAULT_REDACTOR.obj({"event_type": event_type, "set_id": set_id, **_flat(payload)})
    log.log(_LEVEL.get(severity, logging.INFO), event_type, extra=extra)
    if sessionmaker is None:
        return False
    from hermclaw.events.store import append_event

    try:
        async with sessionmaker() as session:
            await append_event(session, event_type, source_type=SOURCE_TYPE, source_id=set_id, severity=severity, payload=payload)
            await session.commit()
    except Exception as exc:  # the event store is optional for backup runs
        log.warning("backup event not stored", extra={"event_type": event_type, "error": type(exc).__name__})
        return False
    return True


_RESERVED = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime", "event_type", "set_id"}


def _flat(payload: dict[str, Any]) -> dict[str, Any]:
    """Scalar payload fields as log ``extra`` (names that collide with LogRecord attributes get a ``p_`` prefix)."""
    return {(f"p_{k}" if k in _RESERVED else k): v for k, v in payload.items() if not isinstance(v, dict | list)}
