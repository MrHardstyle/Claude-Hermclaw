"""Host-wide lock for backup runs, prunes and restore tests.

``flock`` on ``<staging_dir>/.hermclaw-backup.lock``: one backup/prune/restore test at a time on this host – a prune
must not delete the set a restore test is reading, two timers must not dump concurrently. The lock is released by
the kernel when the process dies (no stale lock files). ``flock`` locks belong to the open file description, so two
acquisitions inside one process conflict as well.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

from hermclaw.backup.errors import BACKUP_LOCKED, BackupError

POLL_SECONDS = 0.25


def _open_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _holder(fd: int) -> str:
    with contextlib.suppress(OSError):
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 200).decode("ascii", "replace").strip()
    return ""


def _mark(fd: int, what: str) -> None:
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"pid={os.getpid()} op={what}\n".encode("ascii", "replace"))


@contextlib.asynccontextmanager
async def backup_lock(path: Path, *, wait_seconds: float, what: str) -> AsyncIterator[None]:
    """Hold the lock for the body; waits up to ``wait_seconds`` (0 = fail at once) and raises ``BACKUP_LOCKED``."""
    fd = await asyncio.to_thread(_open_lock, path)
    locked = False
    try:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while not (locked := _try_lock(fd)):
            if time.monotonic() >= deadline:
                holder = _holder(fd)
                raise BackupError(
                    f"another backup operation is running ({holder or 'unknown holder'}); try again later",
                    code=BACKUP_LOCKED,
                    details={"lock": str(path)},
                )
            await asyncio.sleep(POLL_SECONDS)
        _mark(fd, what)
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
