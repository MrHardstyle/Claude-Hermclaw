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
    """One :class:`asyncio.Lock` per key (mirror path, workspace id); entries are dropped when nobody uses them."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def __len__(self) -> int:
        return len(self._locks)

    @contextlib.asynccontextmanager
    async def hold(self, key: str, *, wait_seconds: float) -> AsyncIterator[None]:
        lock = self.get(key)
        self._users[key] = self._users.get(key, 0) + 1
        try:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=wait_seconds)
            except TimeoutError as exc:
                raise GitLockTimeout(
                    f"timed out waiting for in-process lock {key}", details={"lock": key, "timeout": wait_seconds}
                ) from exc
            try:
                yield
            finally:
                lock.release()
        finally:
            remaining = self._users[key] - 1
            if remaining:
                self._users[key] = remaining
            else:
                del self._users[key]
                self._locks.pop(key, None)


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
    """In-process lock first (cheap, fair), then the cross-process ``flock``."""
    async with locks.hold(key, wait_seconds=wait_seconds), file_lock(lock_file, wait_seconds=wait_seconds):
        yield
