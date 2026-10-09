"""Workspace sync helpers of the execution worker (P18, decision D-005).

The orchestrator on ``.225`` ships a workspace snapshot as a tar stream to ``.222``; after sandbox commands
it fetches a manifest (``path -> sha256``) and the changed files back. The workspace content is
*untrusted* in both directions (sandbox commands can create arbitrary symlinks, FIFOs, huge files), so:

* :func:`extract_tar_safely` validates the whole archive before writing anything – absolute paths, ``..``
  components, device files/FIFOs, duplicate or conflicting entries, links escaping ``dest`` and size/count
  limits are rejected. Files are written through directory file descriptors with ``O_NOFOLLOW`` (a
  pre-existing symlink is never written through), hardlinks are materialised as copies and symlinks are
  created last and re-checked with ``realpath`` (no file is ever written through an archive symlink).
  Permission bits are kept without setuid/setgid/sticky and group/other write; ownership is ignored.
* :func:`build_tar` and :func:`manifest` walk with :func:`os.fwalk` (no symlink is followed, also not in
  intermediate components) and open files with ``O_NOFOLLOW | O_NONBLOCK``; symlinks are archived/hashed
  as links, FIFOs/sockets/devices are skipped. ``.git`` is excluded by default (risk R-017: the runtime's
  Git never takes repository internals back from the sandbox).

Errors: :class:`UnsafeArchiveError` / :class:`WorkspaceLimitExceeded` (codes ``WORKSPACE_ARCHIVE_UNSAFE`` /
``WORKSPACE_LIMIT_EXCEEDED``); :func:`build_tar` raises :class:`ValueError` for invalid/unsafe ``paths``
and :class:`FileNotFoundError` for missing ones (mapped to 400/404 by the daemon).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import os
import posixpath
import stat
import tarfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from hermclaw.core.errors import ValidationFailed

DEFAULT_EXCLUDES: tuple[str, ...] = (".git",)
MAX_MEMBERS = 200_000
MAX_TOTAL_BYTES = 4 * 1024**3
MAX_FILE_BYTES = 1024**3
MAX_PATH_CHARS = 4096
MAX_NAME_BYTES = 255
_CHUNK = 1024 * 1024

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafeArchiveError(ValidationFailed):
    """The archive (or the destination it would be written to) is unsafe or corrupt."""

    code = "WORKSPACE_ARCHIVE_UNSAFE"


class WorkspaceLimitExceeded(ValidationFailed, ValueError):
    """A size/count limit of an archive or workspace was exceeded."""

    code = "WORKSPACE_LIMIT_EXCEEDED"


# ---------------------------------------------------------------------------------------------- paths
def normalize_member_path(name: str) -> str:
    """Normalise a relative archive/workspace path (``./a//b`` -> ``a/b``).

    Raises :class:`UnsafeArchiveError` for empty, absolute, ``..``-containing or overlong paths."""
    if not name or "\x00" in name:
        raise UnsafeArchiveError(f"invalid path {name[:200]!r}")
    if name.startswith("/"):
        raise UnsafeArchiveError(f"absolute path not allowed: {name[:200]!r}")
    if len(name) > MAX_PATH_CHARS:
        raise UnsafeArchiveError(f"path too long ({len(name)} characters)")
    parts: list[str] = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafeArchiveError(f"path traversal not allowed: {name[:200]!r}")
        if len(part.encode("utf-8", "surrogateescape")) > MAX_NAME_BYTES:
            raise UnsafeArchiveError(f"path component too long in {name[:200]!r}")
        parts.append(part)
    if not parts:
        raise UnsafeArchiveError(f"path refers to the archive root: {name[:200]!r}")
    return "/".join(parts)


def safe_relative_path(path: str) -> str:
    """:func:`normalize_member_path` raising :class:`ValueError` (for request parameters)."""
    try:
        return normalize_member_path(path)
    except UnsafeArchiveError as exc:
        raise ValueError(exc.message) from exc


def _is_excluded(parts: Iterable[str], exclude: Iterable[str]) -> bool:
    excluded = set(exclude)
    return any(part in excluded for part in parts)


# ---------------------------------------------------------------------------------------------- extraction
@dataclass
class _Entry:
    kind: str  # "file" | "dir" | "symlink" | "hardlink"
    path: str
    member: tarfile.TarInfo
    link: str = ""  # symlink target (as stored) or normalised hardlink target path


def _plan(tf: tarfile.TarFile, *, max_members: int, max_total_bytes: int, max_file_bytes: int) -> list[_Entry]:
    """Validate every member lexically before anything is written."""
    entries: list[_Entry] = []
    by_path: dict[str, _Entry] = {}
    kinds: dict[str, str] = {}
    total = 0
    count = 0
    while True:
        try:
            member = tf.next()
        except tarfile.TarError as exc:
            raise UnsafeArchiveError(f"corrupt tar archive: {exc}") from exc
        if member is None:
            break
        count += 1
        if count > max_members:
            raise WorkspaceLimitExceeded(f"archive has more than {max_members} members")
        if member.isdir() and not member.name.startswith("/") and all(p in ("", ".") for p in member.name.split("/")):
            continue  # "./" – the archive root itself
        path = normalize_member_path(member.name)
        if path in kinds:
            raise UnsafeArchiveError(f"duplicate archive member {path!r}")
        if member.isreg():
            if member.size > max_file_bytes:
                raise WorkspaceLimitExceeded(f"{path!r} is larger than {max_file_bytes} bytes")
            total += member.size
            if total > max_total_bytes:
                raise WorkspaceLimitExceeded(f"archive content exceeds {max_total_bytes} bytes")
            entry = _Entry("file", path, member)
        elif member.isdir():
            entry = _Entry("dir", path, member)
        elif member.issym():
            target = member.linkname
            if not target or "\x00" in target or len(target) > MAX_PATH_CHARS:
                raise UnsafeArchiveError(f"invalid symlink target for {path!r}")
            if target.startswith("/"):
                raise UnsafeArchiveError(f"absolute symlink target not allowed: {path!r} -> {target[:200]!r}")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            if resolved == ".." or resolved.startswith("../"):
                raise UnsafeArchiveError(f"symlink escapes the workspace: {path!r} -> {target[:200]!r}")
            entry = _Entry("symlink", path, member, link=target)
        elif member.islnk():
            target_path = normalize_member_path(member.linkname)
            target_kind = kinds.get(target_path)
            if target_kind not in ("file", "hardlink"):
                raise UnsafeArchiveError(f"hardlink {path!r} must point to a regular file earlier in the archive")
            source = by_path[target_path]
            size = source.member.size
            total += size
            if total > max_total_bytes:
                raise WorkspaceLimitExceeded(f"archive content exceeds {max_total_bytes} bytes")
            entry = _Entry("hardlink", path, member, link=source.link if source.kind == "hardlink" else target_path)
        else:
            raise UnsafeArchiveError(f"unsupported member type for {path!r} (devices, FIFOs and sockets are not allowed)")
        kinds[path] = entry.kind
        by_path[path] = entry
        entries.append(entry)
    # no entry may live below a non-directory entry (file/symlink) of the same archive
    for entry in entries:
        parts = entry.path.split("/")
        for i in range(1, len(parts)):
            ancestor = "/".join(parts[:i])
            kind = kinds.get(ancestor)
            if kind is not None and kind != "dir":
                raise UnsafeArchiveError(f"{entry.path!r} lies below the {kind} {ancestor!r}")
    return entries


class _DirCache:
    """Opens (and creates) directories below ``root_fd`` without following symlinks; caches the last one."""

    def __init__(self, root_fd: int, *, create: bool) -> None:
        self.root_fd = root_fd
        self.create = create
        self._key: tuple[str, ...] | None = None
        self._fd: int | None = None

    def open(self, parts: tuple[str, ...]) -> int:
        if not parts:
            return self.root_fd
        if self._key == parts and self._fd is not None:
            return self._fd
        self.close()
        fd = os.dup(self.root_fd)
        try:
            for i, part in enumerate(parts):
                if self.create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, 0o755, dir_fd=fd)
                try:
                    nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    where = "/".join(parts[: i + 1])
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise UnsafeArchiveError(f"refusing to write through symlink or non-directory {where!r}") from exc
                    raise
                os.close(fd)
                fd = nxt
        except BaseException:
            os.close(fd)
            raise
        self._key, self._fd = parts, fd
        return fd

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
        self._key, self._fd = None, None


def _lstat_at(name: str, dir_fd: int) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _clear_for(name: str, dir_fd: int, *, path: str, kind: str) -> None:
    """Make room for a file/symlink entry: replace files and symlinks, refuse to replace directories."""
    st = _lstat_at(name, dir_fd)
    if st is None:
        return
    if stat.S_ISDIR(st.st_mode):
        raise UnsafeArchiveError(f"cannot replace directory {path!r} with a {kind}")
    os.unlink(name, dir_fd=dir_fd)


def _preflight(root: Path, entries: list[_Entry]) -> None:
    """Refuse (before writing) entries whose parent chain in ``root`` contains a symlink or a non-directory,
    and file/link entries that would replace an existing directory."""
    checked: set[str] = set()
    for entry in entries:
        parts = entry.path.split("/")
        chain = parts if entry.kind == "dir" else parts[:-1]
        complete = True
        for i in range(1, len(chain) + 1):
            prefix = "/".join(chain[:i])
            if prefix in checked:
                continue
            try:
                st = os.lstat(root / prefix)
            except FileNotFoundError:
                complete = False
                break
            if not stat.S_ISDIR(st.st_mode):
                raise UnsafeArchiveError(f"refusing to write through symlink or non-directory {prefix!r}")
            checked.add(prefix)
        if entry.kind != "dir" and complete:
            try:
                st = os.lstat(root / entry.path)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode):
                raise UnsafeArchiveError(f"cannot replace directory {entry.path!r} with a {entry.kind}")


def _file_mode(member: tarfile.TarInfo) -> int:
    return (member.mode & 0o755) | 0o600


def _write_file(tf: tarfile.TarFile, dirs: _DirCache, entry: _Entry, source: tarfile.TarInfo) -> None:
    parts = tuple(entry.path.split("/"))
    pfd = dirs.open(parts[:-1])
    _clear_for(parts[-1], pfd, path=entry.path, kind="file")
    fileobj = tf.extractfile(source)
    if fileobj is None:
        raise UnsafeArchiveError(f"cannot read archive member {entry.path!r}")
    mode = _file_mode(entry.member if entry.kind == "file" else source)
    fd = os.open(parts[-1], _WRITE_FLAGS, mode, dir_fd=pfd)
    try:
        with fileobj:
            while chunk := fileobj.read(_CHUNK):
                _write_all(fd, chunk)
        os.fchmod(fd, mode)
        mtime = int(entry.member.mtime)
        os.utime(fd, (mtime, mtime))
    except tarfile.TarError as exc:
        raise UnsafeArchiveError(f"corrupt archive member {entry.path!r}: {exc}") from exc
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def extract_tar_safely(
    data: bytes,
    dest: Path,
    *,
    max_members: int = MAX_MEMBERS,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> None:
    """Extract a (optionally gzip/bzip2/xz compressed) tar into ``dest`` (created if missing).

    Existing files/symlinks at member paths are replaced (merge semantics); existing directories are kept.
    The archive is validated completely before the first write."""
    if dest.is_symlink():
        raise UnsafeArchiveError("destination must not be a symlink")
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")  # noqa: SIM115 - closed below
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise UnsafeArchiveError(f"not a valid tar archive: {exc}") from exc
    with tf:
        try:
            entries = _plan(tf, max_members=max_members, max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes)
        except (EOFError, OSError) as exc:  # truncated/corrupt compressed streams
            raise UnsafeArchiveError(f"corrupt tar archive: {exc}") from exc
        _preflight(root, entries)
        by_path = {e.path: e for e in entries}
        root_fd = os.open(root, _DIR_FLAGS)
        dirs = _DirCache(root_fd, create=True)
        symlinks: list[_Entry] = []
        try:
            for entry in entries:
                if entry.kind == "dir":
                    fd = dirs.open(tuple(entry.path.split("/")))
                    os.fchmod(fd, (entry.member.mode & 0o755) | 0o700)
                elif entry.kind == "file":
                    _write_file(tf, dirs, entry, entry.member)
                elif entry.kind == "hardlink":
                    _write_file(tf, dirs, entry, by_path[entry.link].member)
                else:
                    symlinks.append(entry)
            # symlinks last: no archive member is ever written through an archive symlink
            for entry in symlinks:
                parts = tuple(entry.path.split("/"))
                pfd = dirs.open(parts[:-1])
                _clear_for(parts[-1], pfd, path=entry.path, kind="symlink")
                os.symlink(entry.link, parts[-1], dir_fd=pfd)
        except (EOFError, tarfile.TarError) as exc:
            raise UnsafeArchiveError(f"corrupt tar archive: {exc}") from exc
        finally:
            dirs.close()
            os.close(root_fd)
        _verify_symlinks(root, symlinks)


def _verify_symlinks(root: Path, symlinks: list[_Entry]) -> None:
    """Chains of individually harmless links (``a -> .``, ``b -> a/..``) can still escape: re-check them."""
    escaping = [e.path for e in symlinks if not Path(os.path.realpath(root / e.path)).is_relative_to(root)]
    for path in escaping:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(root / path)
    if escaping:
        raise UnsafeArchiveError(f"symlink resolves outside the workspace: {escaping[0]!r}")


# ---------------------------------------------------------------------------------------------- walking
@dataclass(frozen=True)
class _Node:
    path: str  # relative posix path
    kind: str  # "file" | "dir" | "symlink"
    name: str
    dir_fd: int
    st: os.stat_result


def _open_root(src: Path) -> int:
    try:
        return os.open(src, _DIR_FLAGS)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError(f"workspace {src} is not a directory") from exc
        raise


def _walk(top_fd: int, top: str, prefix: str, exclude: tuple[str, ...]) -> Iterator[_Node]:
    """Yield nodes below ``top`` (relative to ``top_fd``) without following any symlink."""
    for dirpath, dirnames, filenames, dfd in os.fwalk(top, dir_fd=top_fd, follow_symlinks=False):
        rel = dirpath[len(top) :].lstrip("/")  # fwalk yields "<top>/<sub>/..."
        base = posixpath.join(prefix, rel) if rel else prefix
        keep: list[str] = []
        for name in sorted(dirnames):
            if name in exclude:
                continue
            st = _lstat_at(name, dfd)
            if st is None:
                continue
            path = posixpath.join(base, name) if base else name
            if stat.S_ISLNK(st.st_mode):
                yield _Node(path, "symlink", name, dfd, st)
            elif stat.S_ISDIR(st.st_mode):
                keep.append(name)
                yield _Node(path, "dir", name, dfd, st)
        dirnames[:] = keep
        for name in sorted(filenames):
            if name in exclude:
                continue
            st = _lstat_at(name, dfd)
            if st is None:
                continue
            path = posixpath.join(base, name) if base else name
            if stat.S_ISLNK(st.st_mode):
                yield _Node(path, "symlink", name, dfd, st)
            elif stat.S_ISREG(st.st_mode):
                yield _Node(path, "file", name, dfd, st)
            # FIFOs, sockets and devices are skipped


def _open_regular(node: _Node) -> int | None:
    """Open a regular file without following symlinks; ``None`` when it changed type or vanished."""
    try:
        fd = os.open(node.name, _READ_FLAGS, dir_fd=node.dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ELOOP, errno.ENXIO, errno.EACCES):
            return None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def _iter_selected(root_fd: int, paths: list[str] | None, exclude: tuple[str, ...]) -> Iterator[_Node]:
    if paths is None:
        yield from _walk(root_fd, ".", "", exclude)
        return
    seen: set[str] = set()
    for raw in paths:
        path = safe_relative_path(raw)
        parts = path.split("/")
        if _is_excluded(parts, exclude):
            raise ValueError(f"path is excluded from workspace sync: {path!r}")
        dirs = _DirCache(root_fd, create=False)
        try:
            try:
                pfd = dirs.open(tuple(parts[:-1]))
            except UnsafeArchiveError as exc:
                raise ValueError(f"path traverses a symlink or file: {path!r}") from exc
            except FileNotFoundError as exc:
                raise FileNotFoundError(errno.ENOENT, "no such file or directory", path) from exc
            st = _lstat_at(parts[-1], pfd)
            if st is None:
                raise FileNotFoundError(errno.ENOENT, "no such file or directory", path)
            if stat.S_ISDIR(st.st_mode):
                if path not in seen:
                    seen.add(path)
                    yield _Node(path, "dir", parts[-1], pfd, st)
                for node in _walk(pfd, parts[-1], path, exclude):
                    if node.path not in seen:
                        seen.add(node.path)
                        yield node
            elif stat.S_ISLNK(st.st_mode) or stat.S_ISREG(st.st_mode):
                if path not in seen:
                    seen.add(path)
                    yield _Node(path, "symlink" if stat.S_ISLNK(st.st_mode) else "file", parts[-1], pfd, st)
        finally:
            dirs.close()


# ---------------------------------------------------------------------------------------------- build / manifest
def build_tar(
    src: Path,
    paths: list[str] | None = None,
    *,
    exclude: Iterable[str] = DEFAULT_EXCLUDES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    compress: bool = False,
) -> bytes:
    """Tar of the workspace ``src`` (or only of ``paths``, files or directories, relative to it).

    Entries are sorted, ownership is normalised to 0/0, symlinks are stored as links (never followed)."""
    excluded = tuple(exclude)
    buf = io.BytesIO()
    total = 0
    root_fd = _open_root(src)
    try:
        with tarfile.open(fileobj=buf, mode="w:gz" if compress else "w", format=tarfile.PAX_FORMAT) as tf:
            for node in _iter_selected(root_fd, paths, excluded):
                info = tarfile.TarInfo(node.path)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = int(node.st.st_mtime)
                if node.kind == "dir":
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tf.addfile(info)
                elif node.kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.mode = 0o777
                    info.linkname = os.readlink(node.name, dir_fd=node.dir_fd)
                    tf.addfile(info)
                else:
                    fd = _open_regular(node)
                    if fd is None:
                        continue
                    with os.fdopen(fd, "rb") as fh:
                        content = fh.read(max_total_bytes - total + 1)
                    total += len(content)
                    if total > max_total_bytes:
                        raise WorkspaceLimitExceeded(f"workspace archive exceeds {max_total_bytes} bytes")
                    info.type = tarfile.REGTYPE
                    info.mode = (node.st.st_mode & 0o755) | 0o600
                    info.size = len(content)
                    tf.addfile(info, io.BytesIO(content))
    finally:
        os.close(root_fd)
    return buf.getvalue()


def _symlink_digest(target: str) -> str:
    return hashlib.sha256(b"symlink\x00" + os.fsencode(target)).hexdigest()


def manifest(src: Path, *, exclude: Iterable[str] = DEFAULT_EXCLUDES) -> dict[str, str]:
    """``relative path -> sha256`` of every regular file and symlink (``sha256("symlink\\0" + target)``)."""
    excluded = tuple(exclude)
    out: dict[str, str] = {}
    root_fd = _open_root(src)
    try:
        for node in _walk(root_fd, ".", "", excluded):
            if node.kind == "symlink":
                try:
                    out[node.path] = _symlink_digest(os.readlink(node.name, dir_fd=node.dir_fd))
                except OSError:
                    continue
            elif node.kind == "file":
                fd = _open_regular(node)
                if fd is None:
                    continue
                digest = hashlib.sha256()
                with os.fdopen(fd, "rb") as fh:
                    while chunk := fh.read(_CHUNK):
                        digest.update(chunk)
                out[node.path] = digest.hexdigest()
    finally:
        os.close(root_fd)
    return dict(sorted(out.items()))


def diff_manifest(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str]]:
    """``(changed, deleted)``: added or modified paths of ``new`` and paths of ``old`` missing in ``new``."""
    changed = sorted(path for path, digest in new.items() if old.get(path) != digest)
    deleted = sorted(path for path in old if path not in new)
    return changed, deleted
