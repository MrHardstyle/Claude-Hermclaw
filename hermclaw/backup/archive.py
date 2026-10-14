"""Tarballs of the config directory and the artifact store (P33 33.2/33.3).

Building: ``os.walk`` without following symlinks, sorted (deterministic), only regular files and directories –
symlinks and special files are skipped and reported. Exclusion patterns (gitignore-like): ``name/`` excludes a
directory of that name at any depth, a pattern with ``/`` matches the relative path, any other pattern the file
name; matching is case-insensitive. Files whose head looks like a private key are excluded (``sniff_private_keys``)
and, for config sources (``scan_secrets``), small text files that contain a plaintext secret (private key block,
token shapes, a password embedded in a database URL, ``password: <value>`` that is not a ``cred:``/``file:``/``env:``
reference). Explicit ``exclude_paths`` (files referenced as ``file:`` secrets) are always left out.

Checking: every member must be a relative, ``..``-free regular file or directory; the whole gzip stream is read to
its end (CRC + length check); ``records/*.jsonl`` members are counted and every line parsed as JSON. Extraction uses
the ``data`` extraction filter (PEP 706) after the check.
"""

from __future__ import annotations

import gzip
import json
import os
import stat
import tarfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import IO

from hermclaw.backup.errors import BACKUP_ARCHIVE_FAILED, BackupError
from hermclaw.core.redaction import SECRET_PATTERNS

PRIVATE_KEY_MARKERS = (b"PRIVATE KEY-----", b"PRIVATE KEY BLOCK-----", b"-----BEGIN OPENSSH PRIVATE KEY")
_SNIFF_BYTES = 64 * 1024
_CHUNK = 1024 * 1024
MAX_REPORTED = 500  # names listed per category in the manifest
MAX_SCAN_BYTES = 1024 * 1024  # text files up to this size are scanned for plaintext secrets
_REFERENCE_PREFIXES = ("cred:", "file:", "env:", "{{", "${", "<", "***", "%(")
_KEY_VALUE_PATTERN = 2  # index of the generic ``password: value`` shape in SECRET_PATTERNS


@dataclass(frozen=True)
class ArchiveSource:
    arc_root: str  # top-level directory inside the tarball ("config", "artifacts", "records", "credstore/<name>")
    path: Path
    excludes: tuple[str, ...] = ()
    sniff_private_keys: bool = True
    scan_secrets: bool = False
    required: bool = False


@dataclass
class ArchiveStats:
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    excluded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # symlinks / special files / unreadable
    missing: list[str] = field(default_factory=list)  # optional roots that do not exist
    per_root_files: dict[str, int] = field(default_factory=dict)  # top-level dir -> files


def is_excluded(rel: PurePosixPath, *, is_dir: bool, patterns: Iterable[str]) -> bool:
    rel_s = str(rel).lower()
    name = rel.name.lower()
    for raw in patterns:
        pattern = raw.strip().lower()
        if not pattern:
            continue
        if pattern.endswith("/"):
            dir_pattern = pattern.rstrip("/")
            if not is_dir:
                continue
            if "/" in dir_pattern:
                if fnmatchcase(rel_s, dir_pattern.lstrip("/")):
                    return True
            elif fnmatchcase(name, dir_pattern):
                return True
        elif "/" in pattern:
            if fnmatchcase(rel_s, pattern.lstrip("/")):
                return True
        elif fnmatchcase(name, pattern):
            return True
    return False


def looks_like_private_key(head: bytes) -> bool:
    return any(marker in head for marker in PRIVATE_KEY_MARKERS)


def plaintext_secret_reason(data: bytes) -> str | None:
    """Why ``data`` (a small text file) looks like it carries a plaintext secret, or None.

    Secret *references* (``cred:``/``file:``/``env:``) and template placeholders are not secrets.
    """
    if b"\x00" in data:
        return None  # binary (e.g. an encrypted credential blob)
    text = data.decode("utf-8", errors="replace")
    for index, pattern in enumerate(SECRET_PATTERNS):
        for match in pattern.finditer(text):
            if index == _KEY_VALUE_PATTERN:
                value = match.group(match.lastindex or 0).strip("\"'")
                if value.lower().startswith(_REFERENCE_PREFIXES) or value.lower() in {"null", "none", "true", "false"}:
                    continue
            return "plaintext secret" if index != 0 else "private key"
    return None


def _note(target: list[str], value: str) -> None:
    if len(target) < MAX_REPORTED:
        target.append(value)


def build_tarball(
    dest: Path,
    sources: list[ArchiveSource],
    *,
    compresslevel: int = 6,
    skip_paths: Iterable[Path] = (),
    exclude_paths: Iterable[Path] = (),
) -> ArchiveStats:
    """Write ``dest`` (gzip tar, mode 0600). Blocking – call through ``asyncio.to_thread``.

    ``skip_paths``: directories never descended into (backup staging, a local target below a source);
    ``exclude_paths``: files/directories excluded as secrets (reported under ``excluded``).
    """
    stats = ArchiveStats()
    skip = {p.resolve() for p in skip_paths}
    excluded = {p.resolve() for p in exclude_paths}
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw:
            with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=compresslevel, format=tarfile.PAX_FORMAT) as tar:
                for source in sources:
                    _add_source(tar, source, stats, skip, excluded)
            # the tar end blocks and the gzip trailer are written when the TarFile closes – sync afterwards
            raw.flush()
            os.fsync(raw.fileno())
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return stats


def _add_source(tar: tarfile.TarFile, source: ArchiveSource, stats: ArchiveStats, skip: set[Path], excluded: set[Path]) -> None:
    root = source.path
    if not root.is_dir() or root.is_symlink():
        if source.required:
            raise BackupError(f"backup source {root} is not a directory", code=BACKUP_ARCHIVE_FAILED)
        _note(stats.missing, str(root))
        _count_root(stats, source.arc_root, 0)
        return
    arc_root = PurePosixPath(source.arc_root)
    tar.addfile(_dir_info(str(arc_root), root.stat()))
    stats.dirs += 1
    count = 0

    def unreadable(exc: OSError) -> None:
        name = Path(exc.filename).relative_to(root).as_posix() if exc.filename else "?"
        _note(stats.skipped, f"{arc_root / name}/ (unreadable: {exc.strerror})")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=unreadable):
        current = Path(dirpath)
        rel_dir = PurePosixPath(current.relative_to(root).as_posix()) if current != root else PurePosixPath()
        keep: list[str] = []
        for name in sorted(dirnames):
            full = current / name
            rel = rel_dir / name
            if full.is_symlink():
                _note(stats.skipped, f"{arc_root / rel} (symlink)")
                continue
            resolved = full.resolve()
            if resolved in skip:
                _note(stats.skipped, f"{arc_root / rel} (backup staging/target)")
                continue
            if resolved in excluded:
                _note(stats.excluded, f"{arc_root / rel}/ (secret reference)")
                continue
            if is_excluded(rel, is_dir=True, patterns=source.excludes):
                _note(stats.excluded, f"{arc_root / rel}/")
                continue
            keep.append(name)
            tar.addfile(_dir_info(str(arc_root / rel), full.lstat()))
            stats.dirs += 1
        dirnames[:] = keep
        for name in sorted(filenames):
            full = current / name
            rel = rel_dir / name
            if is_excluded(rel, is_dir=False, patterns=source.excludes):
                _note(stats.excluded, str(arc_root / rel))
                continue
            if full.resolve() in excluded:
                _note(stats.excluded, f"{arc_root / rel} (secret reference)")
                continue
            added = _add_file(tar, full, str(arc_root / rel), source, stats)
            if added:
                count += 1
    _count_root(stats, source.arc_root, count)


def _count_root(stats: ArchiveStats, arc_root: str, count: int) -> None:
    """Regular files per top-level directory of the tarball (what ``inspect_tarball`` counts as ``root_files``)."""
    top = PurePosixPath(arc_root).parts[0]
    stats.per_root_files[top] = stats.per_root_files.get(top, 0) + count


def _dir_info(name: str, st: os.stat_result) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = stat.S_IMODE(st.st_mode)
    info.mtime = int(st.st_mtime)
    return info


def _add_file(tar: tarfile.TarFile, full: Path, arcname: str, source: ArchiveSource, stats: ArchiveStats) -> bool:
    try:
        lst = full.lstat()
    except OSError:
        _note(stats.skipped, f"{arcname} (vanished)")
        return False
    if stat.S_ISLNK(lst.st_mode):
        _note(stats.skipped, f"{arcname} (symlink)")
        return False
    if not stat.S_ISREG(lst.st_mode):
        _note(stats.skipped, f"{arcname} (special file)")
        return False
    try:
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
    except PermissionError:  # e.g. a root-only file in the config dir: reported in the manifest, not fatal
        _note(stats.skipped, f"{arcname} (permission denied)")
        return False
    except OSError as exc:
        raise BackupError(f"cannot read {arcname}: {exc.strerror}", code=BACKUP_ARCHIVE_FAILED) from None
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if source.sniff_private_keys or source.scan_secrets:
            head = handle.read(max(_SNIFF_BYTES, MAX_SCAN_BYTES) if source.scan_secrets else _SNIFF_BYTES)
            if source.sniff_private_keys and looks_like_private_key(head[:_SNIFF_BYTES]):
                _note(stats.excluded, f"{arcname} (private key)")
                return False
            if source.scan_secrets and st.st_size <= MAX_SCAN_BYTES:
                reason = plaintext_secret_reason(head)
                if reason:
                    _note(stats.excluded, f"{arcname} ({reason})")
                    return False
            handle.seek(0)
        info = tarfile.TarInfo(arcname)
        info.size = st.st_size
        info.mode = stat.S_IMODE(st.st_mode)
        info.mtime = int(st.st_mtime)
        try:
            tar.addfile(info, handle)
        except OSError as exc:  # file shrank while being read
            raise BackupError(f"{arcname} changed while being archived: {exc}", code=BACKUP_ARCHIVE_FAILED) from None
    stats.files += 1
    stats.bytes += st.st_size
    return True


# ================================================================================================ checking
@dataclass
class TarCheck:
    ok: bool = True
    members: int = 0
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    problems: list[str] = field(default_factory=list)
    root_files: dict[str, int] = field(default_factory=dict)  # top-level dir -> regular files below it
    jsonl_lines: dict[str, int] = field(default_factory=dict)  # records/<table>.jsonl -> lines

    def problem(self, text: str) -> None:
        self.ok = False
        _note(self.problems, text)


def member_name_problem(name: str) -> str | None:
    if not name or "\x00" in name:
        return "empty or NUL in member name"
    if name.startswith("/") or name.startswith("\\") or (len(name) > 1 and name[1] == ":"):
        return f"absolute member path {name!r}"
    if any(part == ".." for part in PurePosixPath(name).parts):
        return f"member path escapes the archive {name!r}"
    return None


def inspect_tarball(path: Path, *, jsonl_prefix: str | None = "records/") -> TarCheck:
    """Full read-through integrity and safety check (blocking)."""
    check = TarCheck()
    try:
        with gzip.open(path, "rb") as gz:
            with tarfile.open(fileobj=gz, mode="r|") as tar:
                for member in tar:
                    check.members += 1
                    problem = member_name_problem(member.name)
                    if problem:
                        check.problem(problem)
                        continue
                    top = PurePosixPath(member.name).parts[0] if PurePosixPath(member.name).parts else ""
                    if member.isdir():
                        check.dirs += 1
                        continue
                    if not member.isreg():
                        check.problem(f"member {member.name!r} is not a regular file or directory")
                        continue
                    check.files += 1
                    check.bytes += member.size
                    check.root_files[top] = check.root_files.get(top, 0) + 1
                    handle = tar.extractfile(member)
                    if handle is None:
                        check.problem(f"member {member.name!r} unreadable")
                        continue
                    if jsonl_prefix and member.name.startswith(jsonl_prefix) and member.name.endswith(".jsonl"):
                        lines, bad = _jsonl(handle)
                        check.jsonl_lines[member.name] = lines
                        if bad:
                            check.problem(f"{member.name}: {bad} invalid JSON line(s)")
                    else:
                        while handle.read(_CHUNK):
                            pass
            while gz.read(_CHUNK):  # trailing data up to the gzip trailer (CRC32 + ISIZE)
                pass
    except (OSError, EOFError, tarfile.TarError, zlib.error) as exc:
        check.problem(f"archive unreadable: {type(exc).__name__}: {exc}")
    return check


def _jsonl(handle: IO[bytes]) -> tuple[int, int]:
    lines = 0
    bad = 0
    buffer = b""
    while chunk := handle.read(_CHUNK):
        buffer += chunk
        *complete, buffer = buffer.split(b"\n")
        for line in complete:
            lines += 1
            bad += 0 if _json_ok(line) else 1
    if buffer:
        lines += 1
        bad += 0 if _json_ok(buffer) else 1
    return lines, bad


def _json_ok(line: bytes) -> bool:
    try:
        json.loads(line)
    except ValueError:
        return False
    return True


def extract_tarball(path: Path, dest: Path) -> int:
    """Extract after ``inspect_tarball`` succeeded; the ``data`` filter refuses anything unsafe. Returns file count."""
    check = inspect_tarball(path, jsonl_prefix=None)
    if not check.ok:
        raise BackupError(f"refusing to extract {path.name}: {'; '.join(check.problems[:3])}", code=BACKUP_ARCHIVE_FAILED)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            tar.extractall(dest, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise BackupError(f"extracting {path.name} failed: {exc}", code=BACKUP_ARCHIVE_FAILED) from None
    return sum(1 for p in dest.rglob("*") if p.is_file() and not p.is_symlink())
