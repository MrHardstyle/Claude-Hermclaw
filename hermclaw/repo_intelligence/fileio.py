"""Safe, bounded file access inside a workspace root (no symlink following, no escapes)."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

from hermclaw.repo_intelligence.paths import matches_any


def lstat_regular(root: Path, rel: str) -> os.stat_result | None:
    """``lstat`` of a regular, non-symlink file whose parents are real directories inside ``root``."""
    full = root / rel
    try:
        st = os.lstat(full)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    parent = os.path.realpath(full.parent)
    root_real = os.path.realpath(root)
    if parent != root_real and not parent.startswith(root_real + os.sep):
        return None
    return st


def read_bytes(root: Path, rel: str, limit: int) -> bytes | None:
    """Up to ``limit`` bytes of a regular file (``O_NOFOLLOW``); ``None`` when unreadable or not a plain file."""
    if lstat_regular(root, rel) is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(root / rel, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        remaining = limit
        while remaining > 0:
            block = os.read(fd, min(remaining, 1 << 20))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")


def read_text(root: Path, rel: str, limit: int) -> str | None:
    data = read_bytes(root, rel, limit)
    return None if data is None else decode_text(data)


def walk_files(root: Path, skip_dirs: tuple[str, ...], max_files: int) -> Iterator[str]:
    """Plain directory walk (non-git workspace without ripgrep): no symlinked directories, skip heavy dirs."""
    count = 0
    root_s = str(root)
    for dirpath, dirnames, filenames in os.walk(root_s, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs and not os.path.islink(os.path.join(dirpath, d)))
        rel_dir = os.path.relpath(dirpath, root_s)
        for name in sorted(filenames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}".replace(os.sep, "/")
            yield rel
            count += 1
            if count >= max_files:
                return


def is_sensitive(path: str, globs: tuple[str, ...]) -> bool:
    return matches_any(path, globs)
