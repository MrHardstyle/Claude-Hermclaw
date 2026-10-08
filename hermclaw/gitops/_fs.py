"""Synchronous filesystem helpers; the engine calls them through ``asyncio.to_thread``."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path


def exists(path: Path) -> bool:
    return path.exists()


def is_dir(path: Path) -> bool:
    return path.is_dir()


def is_symlink(path: Path) -> bool:
    return path.is_symlink()


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return None


def symlinked_entries(directory: Path, nested: tuple[str, ...] = ()) -> list[str]:
    """Names of symlinks directly inside ``directory`` plus any of the ``nested`` relative paths that are symlinks."""
    found: list[str] = []
    try:
        with os.scandir(directory) as it:
            found = sorted(e.name for e in it if e.is_symlink())
    except OSError:
        return found
    found += [rel for rel in nested if (directory / rel).is_symlink()]
    return found


def write_text_nofollow(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` refusing symlinks at the target and its parent (no writes outside the tree)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise OSError(f"refusing to write through symlinked directory {path.parent}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def ensure_dir(path: Path, mode: int = 0o750) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)


def _make_writable_and_retry(func: object, path: str, _exc: BaseException) -> None:
    with contextlib.suppress(OSError):
        parent = os.path.dirname(path)
        os.chmod(parent, os.stat(parent).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
    if callable(func):
        func(path)


def remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.exists():
        shutil.rmtree(path, onexc=_make_writable_and_retry)


def rename(src: Path, dst: Path) -> None:
    src.rename(dst)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    shutil.copyfile(src, dst)
    return True


def remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


def is_within(child: Path, root: Path, *, min_depth: int = 1) -> bool:
    """True if ``child`` (after resolving symlinks) is strictly inside ``root`` at least ``min_depth`` levels deep."""
    try:
        resolved = child.resolve()
        base = root.resolve()
    except OSError:
        return False
    if resolved == base or not resolved.is_relative_to(base):
        return False
    return len(resolved.relative_to(base).parts) >= min_depth


def remove_dir_if_empty(path: Path) -> bool:
    try:
        path.rmdir()
    except OSError:
        return False
    return True


def same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False
