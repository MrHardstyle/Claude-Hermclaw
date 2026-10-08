"""In-process + cross-process locks for mirrors and workspaces (git's own ``*.lock`` files fail fast)."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

from hermclaw.gitops.errors import GitLockTimeout


class KeyedLocks:
    """One :class:`asyncio.Lock` per key (mirror path, workspace id)."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


def _open_lock_file(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def _try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextlib.asynccontextmanager
async def file_lock(path: Path, *, wait_seconds: float = 300.0, poll: float = 0.05) -> AsyncIterator[None]:
    """Exclusive ``flock`` on ``path`` without blocking the event loop."""
    fd = await asyncio.to_thread(_open_lock_file, path)
    try:
        deadline = time.monotonic() + wait_seconds
        while not _try_flock(fd):
            if time.monotonic() > deadline:
                raise GitLockTimeout(f"timed out waiting for lock {path.name}", details={"lock": str(path), "timeout": wait_seconds})
            await asyncio.sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.asynccontextmanager
async def combined_lock(locks: KeyedLocks, key: str, lock_file: Path, *, wait_seconds: float = 300.0) -> AsyncIterator[None]:
    lock = locks.get(key)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=wait_seconds)
    except TimeoutError as exc:
        raise GitLockTimeout(f"timed out waiting for in-process lock {key}", details={"lock": key, "timeout": wait_seconds}) from exc
    try:
        async with file_lock(lock_file, wait_seconds=wait_seconds):
            yield
    finally:
        lock.release()
