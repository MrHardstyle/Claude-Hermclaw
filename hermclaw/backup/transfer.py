"""Backup targets: transfer, listing, retrieval and deletion of backup sets (P33 33.4, target Terra ``.60``).

``LocalStore``  – a directory (mounted Unraid share): files are copied into ``<set>.partial``, every copy is
                  sha256-verified against the manifest, then the directory is renamed to ``<set>`` (atomic).
``RsyncStore``  – ``rsync -a --partial`` over SSH with a dedicated key (``BatchMode``, ``IdentitiesOnly``,
                  ``StrictHostKeyChecking=yes`` with Hermclaw's own known_hosts, no user ssh config). Data files go
                  first, then a ``--checksum --dry-run --itemize-changes --delete`` pass must report no difference,
                  then ``MANIFEST.json`` is sent (commit marker) and the whole set is verified again. Listing uses
                  ``rsync --list-only``, deletion an empty source with ``--delete`` and an include filter for exactly
                  that set – the backup key therefore needs nothing but rsync (``rrsync`` forced command works).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from hermclaw.backup import runner
from hermclaw.backup.config import BackupTarget, LocalTarget, RsyncEndpoint, RsyncTarget, resolve_rsync_endpoint
from hermclaw.backup.errors import (
    BACKUP_CONFIG_INVALID,
    BACKUP_NOT_FOUND,
    BACKUP_SET_INVALID,
    BACKUP_TOOL_MISSING,
    BACKUP_TRANSFER_FAILED,
    BACKUP_VERIFY_FAILED,
    BackupError,
)
from hermclaw.backup.manifest import MANIFEST, MANIFEST_SIG, PARTIAL_SUFFIX, Manifest, check_set_id, is_set_id, set_time, sha256_file
from hermclaw.core.config import HostsConfig
from hermclaw.core.logging import get_logger
from hermclaw.core.settings import get_settings
from hermclaw.security.secrets import SECRETS_DIR, SecretStore

log = get_logger(__name__)
_SAFE_PATH_RE = re.compile(r"^[^\s'\"\\]+$")
_CRED_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class SetListing:
    set_id: str
    complete: bool
    created_at: datetime
    location: str
    partial_dir: bool = False  # LocalStore: '<set>.partial' (interrupted transfer)


@dataclass(frozen=True)
class TransferResult:
    method: str
    location: str
    files: int
    bytes: int
    verified: bool
    duration_ms: int


class TargetStore(Protocol):
    def describe(self) -> str: ...

    async def upload(self, local_dir: Path, manifest: Manifest) -> TransferResult: ...

    async def list_sets(self) -> list[SetListing]: ...

    def materialize(self, set_id: str, work_dir: Path) -> contextlib.AbstractAsyncContextManager[Path]: ...

    async def put_file(self, set_id: str, local_file: Path, name: str) -> None: ...

    async def delete(self, listing: SetListing) -> None: ...


def open_store(target: BackupTarget, *, hosts: HostsConfig | None = None, env: str | None = None) -> TargetStore:
    if isinstance(target, LocalTarget):
        return LocalStore(target)
    return RsyncStore(target, resolve_rsync_endpoint(target, hosts), env=env)


def _set_files(manifest: Manifest, local_dir: Path) -> list[str]:
    names = [f.name for f in manifest.files]
    names.append(MANIFEST)
    if (local_dir / MANIFEST_SIG).is_file():
        names.append(MANIFEST_SIG)
    return names


# ================================================================================================ local
class LocalStore:
    def __init__(self, target: LocalTarget) -> None:
        self.target = target
        self.root = target.path

    def describe(self) -> str:
        return f"local:{self.root}"

    def _check_root(self, *, create: bool = False) -> None:
        if not self.root.is_dir():
            if create and self.target.create_missing:
                self.root.mkdir(parents=True, mode=0o700)
            else:
                raise BackupError(f"backup target {self.root} does not exist (share not mounted?)", code=BACKUP_TRANSFER_FAILED)
        if self.target.require_mount and not _under_mount(self.root):
            raise BackupError(
                f"backup target {self.root} is not on a mounted file system (share not mounted?)", code=BACKUP_TRANSFER_FAILED
            )

    def _set_path(self, set_id: str, *, partial: bool = False) -> Path:
        check_set_id(set_id)
        path = self.root / (set_id + (PARTIAL_SUFFIX if partial else ""))
        if path.parent != self.root:
            raise BackupError("set path escapes the target", code=BACKUP_SET_INVALID)
        return path

    async def upload(self, local_dir: Path, manifest: Manifest) -> TransferResult:
        try:
            return await asyncio.to_thread(self._upload_sync, local_dir, manifest)
        except OSError as exc:  # share full, unmounted underneath us, permissions …
            raise BackupError(f"copy to the backup target {self.root} failed: {exc.strerror or exc}", code=BACKUP_TRANSFER_FAILED) from None

    def _upload_sync(self, local_dir: Path, manifest: Manifest) -> TransferResult:
        started = time.monotonic()
        self._check_root(create=True)
        final = self._set_path(manifest.set_id)
        partial = self._set_path(manifest.set_id, partial=True)
        if final.exists():
            raise BackupError(f"backup set {manifest.set_id} already exists on the target", code=BACKUP_TRANSFER_FAILED)
        if partial.exists():
            shutil.rmtree(partial)
        partial.mkdir(mode=0o700)
        total = 0
        names = _set_files(manifest, local_dir)
        try:
            for name in names:
                _copy_private(local_dir / name, partial / name)
            for entry in manifest.files:  # read back what landed on the share
                digest, size = sha256_file(partial / entry.name)
                if digest != entry.sha256 or size != entry.size:
                    raise BackupError(f"{entry.name}: checksum mismatch after copy to the target", code=BACKUP_VERIFY_FAILED)
                total += size
            _fsync_dir(partial)
            partial.rename(final)
            _fsync_dir(self.root)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        return TransferResult(
            method="local",
            location=str(final),
            files=len(names),
            bytes=total,
            verified=True,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def list_sets(self) -> list[SetListing]:
        return await asyncio.to_thread(self._list_sync)

    def _list_sync(self) -> list[SetListing]:
        self._check_root()
        out: list[SetListing] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                continue
            name = entry.name
            partial = name.endswith(PARTIAL_SUFFIX)
            set_id = name.removesuffix(PARTIAL_SUFFIX)
            if not is_set_id(set_id):
                continue
            complete = not partial and (entry / MANIFEST).is_file()
            out.append(SetListing(set_id, complete, set_time(set_id), str(entry), partial_dir=partial))
        return out

    @contextlib.asynccontextmanager
    async def materialize(self, set_id: str, work_dir: Path) -> AsyncIterator[Path]:
        self._check_root()
        path = self._set_path(set_id)
        if path.is_symlink() or not path.is_dir():  # a symlinked set could point anywhere – never followed
            raise BackupError(f"backup set {set_id} not found on {self.root}", code=BACKUP_NOT_FOUND)
        yield path

    async def put_file(self, set_id: str, local_file: Path, name: str) -> None:
        path = self._set_path(set_id)
        if path.is_symlink() or not path.is_dir() or "/" in name or name.startswith("."):
            raise BackupError(f"cannot write {name} into backup set {set_id}", code=BACKUP_SET_INVALID)
        await asyncio.to_thread(_copy_private, local_file, path / name, atomic=True)

    async def delete(self, listing: SetListing) -> None:
        path = self._set_path(listing.set_id, partial=listing.partial_dir)
        try:
            await asyncio.to_thread(shutil.rmtree, path)
        except OSError as exc:
            raise BackupError(f"deleting {path.name} failed: {exc.strerror or exc}", code=BACKUP_TRANSFER_FAILED) from None


def _under_mount(path: Path) -> bool:
    current = path.resolve()
    while current != current.parent:
        if os.path.ismount(current):
            return True
        current = current.parent
    return False


def _copy_private(src: Path, dest: Path, *, atomic: bool = False) -> None:
    target = dest.with_name(f".{dest.name}.tmp") if atomic else dest
    with src.open("rb") as fin:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fout:
            shutil.copyfileobj(fin, fout, 4 * 1024 * 1024)
            fout.flush()
            os.fsync(fout.fileno())
    if atomic:
        target.replace(dest)


def _fetch_dir(work_dir: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix="fetch-", dir=work_dir))


def _fsync_dir(path: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


# ================================================================================================ rsync
@contextlib.contextmanager
def ssh_key_file(ref: str, *, env: str | None = None) -> Iterator[Path]:
    """Path of the SSH private key for ``ref``. ``file:``/``cred:`` are used in place; other references are written
    to a private temporary file that is removed afterwards (dev/test only – SecretStore enforces that)."""
    kind, _, rest = ref.partition(":")
    if kind == "file":
        path = Path(rest)
        if not path.is_file():
            raise BackupError("backup SSH key file does not exist", code=BACKUP_CONFIG_INVALID)
        if not _SAFE_PATH_RE.match(str(path)):
            raise BackupError("backup SSH key path must not contain whitespace or quotes", code=BACKUP_CONFIG_INVALID)
        yield path
        return
    if kind == "cred":
        if not _CRED_NAME_RE.match(rest):
            raise BackupError("invalid credential name for the backup SSH key", code=BACKUP_CONFIG_INVALID)
        cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
        for base in ([Path(cred_dir)] if cred_dir else []) + [SECRETS_DIR]:
            candidate = base / rest
            if candidate.is_file() and _SAFE_PATH_RE.match(str(candidate)):
                yield candidate
                return
        raise BackupError(f"credential '{rest}' for the backup SSH key is not available", code=BACKUP_CONFIG_INVALID)
    value = SecretStore(env=env).resolve(ref)
    directory = Path(tempfile.mkdtemp(prefix="hermclaw-sshkey-"))
    try:
        directory.chmod(0o700)
        path = directory / "id"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write((value or "").rstrip("\n") + "\n")
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def ssh_command(endpoint: RsyncEndpoint, target: RsyncTarget, key: Path) -> str:
    """The ``-e`` command of rsync (split by rsync on spaces – all parts are validated to contain none)."""
    known_hosts = endpoint.known_hosts or Path("/etc/hermclaw/ssh/known_hosts")
    for part in (str(key), str(known_hosts), target.ssh_binary):
        if not _SAFE_PATH_RE.match(part):
            raise BackupError("ssh command parts must not contain whitespace or quotes", code=BACKUP_CONFIG_INVALID)
    options = [
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={known_hosts}",
        "GlobalKnownHostsFile=/dev/null",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        f"ConnectTimeout={target.connect_timeout_seconds}",
        "ServerAliveInterval=30",
        "ServerAliveCountMax=4",
        "LogLevel=ERROR",
    ]
    parts = [target.ssh_binary, "-F", "/dev/null", "-i", str(key), "-p", str(endpoint.port)]
    for option in options:
        parts += ["-o", option]
    return " ".join(parts)


_ITEM_RE = re.compile(r"^(?P<code>[<>ch.*][fdLDS*][^ ]*) (?P<name>.+)$")


def itemized_differences(output: str) -> list[str]:
    """Lines of ``rsync --itemize-changes --dry-run`` that mean content/existence differences (not just attributes)."""
    diffs: list[str] = []
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("*deleting"):
            diffs.append(line)
            continue
        match = _ITEM_RE.match(line)
        if not match:
            continue
        code = match.group("code")
        if code[0] in "<>ch":
            diffs.append(line)
    return diffs


class RsyncStore:
    def __init__(self, target: RsyncTarget, endpoint: RsyncEndpoint, *, env: str | None = None) -> None:
        self.target = target
        self.endpoint = endpoint
        self.env = env or get_settings().env

    def describe(self) -> str:
        return f"rsync:{self.endpoint.location()}"

    @contextlib.contextmanager
    def _transport(self) -> Iterator[list[str]]:
        """Common rsync arguments incl. ``-e ssh …`` for remote endpoints."""
        args = [self.target.rsync_binary, f"--timeout={self.target.io_timeout_seconds}"]
        if self.target.bwlimit_kbps:
            args.append(f"--bwlimit={self.target.bwlimit_kbps}")
        if self.target.remote_rsync_path:
            args.append(f"--rsync-path={self.target.remote_rsync_path}")
        if not self.endpoint.remote:
            yield args
            return
        if self.endpoint.key_ref is None:
            raise BackupError("rsync over SSH needs key_ref", code=BACKUP_CONFIG_INVALID)
        with ssh_key_file(self.endpoint.key_ref, env=self.env) as key:
            yield [*args, "-e", ssh_command(self.endpoint, self.target, key)]

    async def _rsync(self, args: list[str], what: str) -> runner.ProcResult:
        try:
            result = await runner.run(args, env=runner.base_env(), limit_seconds=self.target.command_timeout_seconds)
        except FileNotFoundError:
            raise BackupError(f"{self.target.rsync_binary} is not installed", code=BACKUP_TOOL_MISSING) from None
        if not result.ok:
            raise BackupError(
                f"rsync {what} failed (exit {result.returncode}): {result.error_text()}",
                code=BACKUP_TRANSFER_FAILED,
                details={"returncode": result.returncode},
            )
        return result

    def upload_argv(self, transport: list[str], local_dir: Path, set_id: str, *, data_only: bool) -> list[str]:
        argv = [*transport, "-a", "--partial", "--partial-dir=.rsync-partial", "--chmod=D0700,F0600"]
        if data_only:
            argv += [f"--exclude=/{MANIFEST}", f"--exclude=/{MANIFEST_SIG}"]
        return [*argv, "--", f"{local_dir}/", self.endpoint.location(set_id, slash=True)]

    def verify_argv(self, transport: list[str], local_dir: Path, set_id: str, *, data_only: bool) -> list[str]:
        argv = [*transport, "-a", "--checksum", "--dry-run", "--itemize-changes", "--delete", "--chmod=D0700,F0600"]
        if data_only:
            argv += [f"--exclude=/{MANIFEST}", f"--exclude=/{MANIFEST_SIG}"]
        return [*argv, "--", f"{local_dir}/", self.endpoint.location(set_id, slash=True)]

    async def _verify(self, transport: list[str], local_dir: Path, set_id: str, *, data_only: bool) -> None:
        result = await self._rsync(self.verify_argv(transport, local_dir, set_id, data_only=data_only), "checksum verification")
        diffs = itemized_differences(result.stdout)
        if diffs:
            raise BackupError(
                f"backup set {set_id} differs on the target after transfer: {'; '.join(diffs[:5])}",
                code=BACKUP_VERIFY_FAILED,
            )

    async def upload(self, local_dir: Path, manifest: Manifest) -> TransferResult:
        started = time.monotonic()
        set_id = check_set_id(manifest.set_id)
        if set_id in {s.set_id for s in await self.list_sets()}:
            raise BackupError(f"backup set {set_id} already exists on the target", code=BACKUP_TRANSFER_FAILED)
        with self._transport() as transport:
            await self._rsync(self.upload_argv(transport, local_dir, set_id, data_only=True), "upload")
            await self._verify(transport, local_dir, set_id, data_only=True)
            await self._rsync(self.upload_argv(transport, local_dir, set_id, data_only=False), "manifest upload")
            await self._verify(transport, local_dir, set_id, data_only=False)
        names = _set_files(manifest, local_dir)
        return TransferResult(
            method="rsync",
            location=self.endpoint.location(set_id),
            files=len(names),
            bytes=manifest.total_bytes(),
            verified=True,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def list_argv(self, transport: list[str]) -> list[str]:
        return [
            *transport,
            "--list-only",
            "-r",
            "--include=/*/",
            f"--include=/*/{MANIFEST}",
            "--exclude=*",
            "--",
            self.endpoint.location(slash=True),
        ]

    async def list_sets(self) -> list[SetListing]:
        with self._transport() as transport:
            result = await self._rsync(self.list_argv(transport), "listing")
        return parse_listing(result.stdout, self.endpoint)

    @contextlib.asynccontextmanager
    async def materialize(self, set_id: str, work_dir: Path) -> AsyncIterator[Path]:
        check_set_id(set_id)
        if not self.endpoint.remote:
            path = Path(self.endpoint.path) / set_id
            if not await asyncio.to_thread(path.is_dir):
                raise BackupError(f"backup set {set_id} not found on {self.endpoint.path}", code=BACKUP_NOT_FOUND)
            yield path
            return
        holder = await asyncio.to_thread(_fetch_dir, work_dir)
        local = holder / set_id
        try:
            with self._transport() as transport:
                argv = [*transport, "-a", "--chmod=D0700,F0600", "--", self.endpoint.location(set_id, slash=True), f"{local}/"]
                await self._rsync(argv, "fetch")
            yield local
        finally:
            shutil.rmtree(holder, ignore_errors=True)

    async def put_file(self, set_id: str, local_file: Path, name: str) -> None:
        check_set_id(set_id)
        with self._transport() as transport:
            argv = [*transport, "-a", "--chmod=F0600", "--", str(local_file), self.endpoint.location(f"{set_id}/{name}")]
            await self._rsync(argv, f"upload of {name}")

    def delete_argv(self, transport: list[str], empty_dir: Path, set_id: str) -> list[str]:
        return [
            *transport,
            "-r",
            "--delete",
            f"--include=/{set_id}/***",
            "--exclude=*",
            "--",
            f"{empty_dir}/",
            self.endpoint.location(slash=True),
        ]

    async def delete(self, listing: SetListing) -> None:
        set_id = check_set_id(listing.set_id)
        with tempfile.TemporaryDirectory(prefix="hermclaw-empty-") as empty, self._transport() as transport:
            await self._rsync(self.delete_argv(transport, Path(empty), set_id), f"deletion of {set_id}")


def parse_listing(output: str, endpoint: RsyncEndpoint) -> list[SetListing]:
    """``rsync --list-only -r`` lines → sets (complete when ``<set>/MANIFEST.json`` is listed)."""
    dirs: set[str] = set()
    with_manifest: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        name = fields[-1].rstrip("/")
        if fields[0].startswith("d") and is_set_id(name):
            dirs.add(name)
        elif fields[0].startswith("-") and name.endswith(f"/{MANIFEST}"):
            parent = name[: -len(MANIFEST) - 1]
            if is_set_id(parent):
                with_manifest.add(parent)
    return [SetListing(s, s in with_manifest, set_time(s), endpoint.location(s)) for s in sorted(dirs)]
