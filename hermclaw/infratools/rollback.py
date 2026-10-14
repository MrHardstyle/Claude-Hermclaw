"""Rollback artifacts and guarded config deployments (Bauplan §31 "rollback config", P28.6).

Before a configuration file is changed on a host, :meth:`RollbackStore.create` snapshots the current state of the
target into the artifact store:

* the previous content (byte exact) as a ``0600`` file below ``settings.artifacts_dir/<job id|adhoc>/rollback/<id>/``
  plus a ``manifest.json`` next to it (target, host, mode/owner, SHA-256, restore command) so an operator can restore
  by hand even without the database;
* an ``artifacts`` row ``kind=rollback`` carrying the same data (``metadata.infratools``) and the checksum.

A target that does not exist yet is recorded as ``existed=false`` – restoring it removes the file again.

:meth:`RollbackStore.restore` loads the row, refuses paths outside the artifact directory, **verifies the checksum
of the stored copy against the row and the manifest before touching the target**, writes the previous content
back (atomically on the local machine, via ``install`` remotely), verifies the target's checksum afterwards and
optionally runs the reload command.

:class:`ConfigDeployer` is the deploy helper that enforces the order: a ``deployments`` row is claimed for the target
(PostgreSQL advisory lock – one deployment per target at a time), the rollback artifact is created and referenced
by ``deployments.rollback_artifact_id`` **before** the new content is written; a failing post-check (e.g.
``nginx -t``) or reload restores the previous state automatically (status ``rolled_back``). Events
``deployment.started`` / ``deployment.finished`` are appended.

Remote hosts are reached through :class:`~hermclaw.infratools.process.HostExecutor` (SSH tools in production). File
content is read with ``cat`` (unredacted, checksummed against ``sha256sum``; ``base64`` as fallback when the
transport altered the bytes) and written with ``install -m <mode> /dev/stdin <path>``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.events.store import append_event
from hermclaw.infratools.errors import (
    INFRA_COMMAND_FAILED,
    INFRA_DEPLOY_FAILED,
    INFRA_INVALID_ARGUMENT,
    INFRA_PATH_INVALID,
    INFRA_ROLLBACK_CORRUPT,
    INFRA_ROLLBACK_FAILED,
    INFRA_ROLLBACK_INVALID,
    INFRA_ROLLBACK_UNKNOWN,
    INFRA_STORE_UNAVAILABLE,
    InfraToolError,
)
from hermclaw.infratools.process import LOCAL_HOST, ExecResult, HostExecutor, InfraContext
from hermclaw.persistence.models import Artifact, Deployment

log = get_logger(__name__)

ROLLBACK_KIND = "rollback"
SOURCE_TYPE = "infratools"
MAX_ROLLBACK_BYTES = 8 * 1024 * 1024
#: where configuration deployments may write unless the caller configures other roots
DEFAULT_DEPLOY_ROOTS: tuple[str, ...] = (
    "/etc/nginx",
    "/etc/hermclaw",
    "/etc/systemd/system",
    "/etc/containers/systemd",
    "/etc/ollama",
    "/opt/hermclaw",
    "/srv/hermclaw",
    "/var/www",
)
STALE_DEPLOYMENT = timedelta(hours=1)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")

DeploymentStatus = Literal["started", "succeeded", "failed", "rolled_back", "rollback_failed"]


# ============================================================================================ paths
def validate_target_path(path: str, allowed_roots: Sequence[str]) -> str:
    """Absolute, normalised POSIX path below one of ``allowed_roots`` (lexically; local symlinks are checked later)."""
    if not isinstance(path, str) or not path or any(c in path for c in ("\x00", "\n", "\r")):
        raise InfraToolError("target path must be a non-empty single-line string", code=INFRA_PATH_INVALID)
    pure = PurePosixPath(path)
    if not pure.is_absolute():
        raise InfraToolError(f"target path '{path}' must be absolute", code=INFRA_PATH_INVALID)
    if any(part in ("..", ".") for part in pure.parts) or "//" in path or path.endswith("/"):
        raise InfraToolError(f"target path '{path}' must be normalised (no '.', '..', '//' or trailing '/')", code=INFRA_PATH_INVALID)
    if not any(_under(pure, PurePosixPath(root)) for root in allowed_roots):
        raise InfraToolError(
            f"target path '{path}' is outside the allowed deployment roots ({', '.join(allowed_roots) or 'none configured'})",
            code=INFRA_PATH_INVALID,
        )
    return str(pure)


def _under(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path != root and root in path.parents


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ============================================================================================ target files
@dataclass(frozen=True)
class FileSnapshot:
    existed: bool
    content: bytes = b""
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None

    @property
    def sha256(self) -> str:
        return _sha256(self.content)


_STAT_FORMAT = "%F|%a|%u|%g|%s"


class TargetFiles:
    """Read/write/remove/checksum a configuration file on the local machine or a registered host."""

    def __init__(self, executor: HostExecutor, *, sudo: bool = False, timeout: float = 60.0, max_bytes: int = MAX_ROLLBACK_BYTES) -> None:
        self.executor = executor
        self.sudo = sudo  # remote only: prefix mutating commands with 'sudo -n'
        self.timeout = timeout
        self.max_bytes = max_bytes

    # ------------------------------------------------------------------------------------------ snapshot
    async def snapshot(self, host: str | None, path: str, *, ctx: InfraContext | None = None) -> FileSnapshot:
        if HostExecutor.is_local(host):
            return await asyncio.to_thread(self._snapshot_local, path)
        return await self._snapshot_remote(str(host), path, ctx)

    def _snapshot_local(self, path: str) -> FileSnapshot:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                raise InfraToolError(f"parent directory of '{path}' does not exist", code=INFRA_PATH_INVALID) from None
            return FileSnapshot(existed=False)
        except OSError as exc:
            raise InfraToolError(f"cannot stat '{path}': {exc.strerror}", code=INFRA_PATH_INVALID) from None
        if stat.S_ISLNK(st.st_mode):
            raise InfraToolError(f"'{path}' is a symbolic link – deploy to the file it points to", code=INFRA_PATH_INVALID)
        if not stat.S_ISREG(st.st_mode):
            raise InfraToolError(f"'{path}' is not a regular file", code=INFRA_PATH_INVALID)
        if st.st_size > self.max_bytes:
            raise InfraToolError(f"'{path}' is larger than {self.max_bytes} bytes", code=INFRA_ROLLBACK_FAILED)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            content = handle.read(self.max_bytes + 1)
        if len(content) > self.max_bytes:
            raise InfraToolError(f"'{path}' is larger than {self.max_bytes} bytes", code=INFRA_ROLLBACK_FAILED)
        return FileSnapshot(True, content, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)

    async def _stat_remote(self, host: str, path: str, ctx: InfraContext | None) -> tuple[str, int, int, int, int] | None:
        res = await self.executor.run(["stat", "-c", _STAT_FORMAT, "--", path], host=host, timeout=self.timeout, ctx=ctx)
        if not res.ok:
            if "no such file" in (res.stderr + (res.error or "")).lower():
                return None
            raise InfraToolError(f"cannot stat '{path}' on {host}: {res.describe(300)}", code=INFRA_COMMAND_FAILED)
        parts = res.stdout.strip().split("|")
        if len(parts) != 5 or not all(p.isdigit() for p in parts[2:]):
            raise InfraToolError(f"unexpected stat output for '{path}' on {host}", code=INFRA_COMMAND_FAILED)
        try:
            mode = int(parts[1], 8)
        except ValueError:
            raise InfraToolError(f"unexpected stat mode for '{path}' on {host}", code=INFRA_COMMAND_FAILED) from None
        return parts[0], mode, int(parts[2]), int(parts[3]), int(parts[4])

    async def _snapshot_remote(self, host: str, path: str, ctx: InfraContext | None) -> FileSnapshot:
        info = await self._stat_remote(host, path, ctx)
        if info is None:
            parent = str(PurePosixPath(path).parent)
            pinfo = await self._stat_remote(host, parent, ctx)
            if pinfo is None or pinfo[0] != "directory":
                raise InfraToolError(f"parent directory of '{path}' does not exist on {host}", code=INFRA_PATH_INVALID)
            return FileSnapshot(existed=False)
        kind, mode, uid, gid, size = info
        if kind == "symbolic link":
            raise InfraToolError(f"'{path}' on {host} is a symbolic link – deploy to the file it points to", code=INFRA_PATH_INVALID)
        if not kind.startswith("regular"):
            raise InfraToolError(f"'{path}' on {host} is not a regular file ({kind})", code=INFRA_PATH_INVALID)
        if size > self.max_bytes:
            raise InfraToolError(f"'{path}' on {host} is larger than {self.max_bytes} bytes", code=INFRA_ROLLBACK_FAILED)
        expected = await self.checksum(host, path, ctx=ctx)
        content = await self._read_remote(host, path, size, expected, ctx)
        return FileSnapshot(True, content, mode, uid, gid)

    async def _read_remote(self, host: str, path: str, size: int, expected: str | None, ctx: InfraContext | None) -> bytes:
        res = await self.executor.run(["cat", "--", path], host=host, timeout=self.timeout, ctx=ctx, raw_stdout=True)
        if res.ok:
            data = res.stdout.encode("utf-8")
            if len(data) == size and _sha256(data) == expected:
                return data
        # the transport decoded/redacted the bytes – transfer base64 instead (only [A-Za-z0-9+/=] on the wire)
        res = await self.executor.run(["base64", "-w", "0", "--", path], host=host, timeout=self.timeout, ctx=ctx, raw_stdout=True)
        if not res.ok:
            raise InfraToolError(f"cannot read '{path}' on {host}: {res.describe(300)}", code=INFRA_ROLLBACK_FAILED)
        try:
            data = base64.b64decode("".join(res.stdout.split()), validate=True)
        except (binascii.Error, ValueError):
            raise InfraToolError(f"'{path}' on {host}: base64 transfer is corrupt", code=INFRA_ROLLBACK_FAILED) from None
        if _sha256(data) != expected:
            raise InfraToolError(f"'{path}' on {host} changed while it was read (checksum mismatch)", code=INFRA_ROLLBACK_FAILED)
        return data

    # ------------------------------------------------------------------------------------------ write/remove
    async def write(
        self,
        host: str | None,
        path: str,
        content: bytes,
        *,
        mode: int,
        uid: int | None = None,
        gid: int | None = None,
        ctx: InfraContext | None = None,
    ) -> None:
        if HostExecutor.is_local(host):
            await asyncio.to_thread(_write_local_atomic, path, content, mode, uid, gid)
            return
        argv = [*self._sudo(), "install", "-m", f"{mode:04o}"]
        if self.sudo and uid is not None and gid is not None:
            argv += ["-o", str(uid), "-g", str(gid)]
        argv += ["/dev/stdin", path]
        res = await self.executor.run(argv, host=host, timeout=self.timeout, stdin=content, classification="mutate", ctx=ctx)
        if not res.ok:
            raise InfraToolError(f"writing '{path}' on {host} failed: {res.describe(300)}", code=INFRA_DEPLOY_FAILED)

    async def remove(self, host: str | None, path: str, *, ctx: InfraContext | None = None) -> None:
        if HostExecutor.is_local(host):
            await asyncio.to_thread(_remove_local, path)
            return
        res = await self.executor.run(
            [*self._sudo(), "rm", "-f", "--", path], host=host, timeout=self.timeout, classification="destructive", ctx=ctx
        )
        if not res.ok:
            raise InfraToolError(f"removing '{path}' on {host} failed: {res.describe(300)}", code=INFRA_DEPLOY_FAILED)

    async def checksum(self, host: str | None, path: str, *, ctx: InfraContext | None = None) -> str | None:
        """SHA-256 of the file, ``None`` when it does not exist."""
        if HostExecutor.is_local(host):
            return await asyncio.to_thread(_checksum_local, path)
        res = await self.executor.run(["sha256sum", "--", path], host=host, timeout=self.timeout, ctx=ctx)
        if not res.ok:
            if "no such file" in (res.stderr + (res.error or "")).lower():
                return None
            raise InfraToolError(f"cannot checksum '{path}' on {host}: {res.describe(300)}", code=INFRA_COMMAND_FAILED)
        digest = res.stdout.strip().split(" ", 1)[0].lstrip("\\")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InfraToolError(f"unexpected sha256sum output for '{path}' on {host}", code=INFRA_COMMAND_FAILED)
        return digest

    async def realpath(self, host: str, path: str, *, ctx: InfraContext | None = None) -> str:
        """Canonical path on a remote host (``readlink -f``) – detects symlinked directories below a deploy root."""
        res = await self.executor.run(["readlink", "-f", "--", path], host=host, timeout=self.timeout, ctx=ctx)
        real = res.stdout.strip()
        if not res.ok or not real.startswith("/"):
            raise InfraToolError(f"cannot resolve '{path}' on {host}: {res.describe(300)}", code=INFRA_PATH_INVALID)
        return real

    def _sudo(self) -> list[str]:
        return ["sudo", "-n"] if self.sudo else []


def _write_local_atomic(path: str, content: bytes, mode: int, uid: int | None, gid: int | None) -> None:
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        raise InfraToolError(f"parent directory of '{path}' does not exist", code=INFRA_PATH_INVALID)
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise InfraToolError(f"'{path}' is a symbolic link – refusing to replace it", code=INFRA_PATH_INVALID)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.hermclaw-{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
            if uid is not None and gid is not None and os.geteuid() == 0:
                os.fchown(handle.fileno(), uid, gid)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _remove_local(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def _checksum_local(path: str) -> str | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise InfraToolError(f"'{path}' is not a regular file", code=INFRA_PATH_INVALID)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


# ============================================================================================ artifacts
@dataclass(frozen=True)
class RollbackArtifact:
    id: uuid.UUID
    host: str
    target_path: str
    existed: bool
    sha256: str
    size: int
    file: str  # the stored previous content inside the artifact directory
    restore_command: str
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    deployment_kind: str = "config"
    reload_argv: tuple[str, ...] | None = None
    job_id: uuid.UUID | None = None
    created_at: str = ""

    def metadata(self) -> dict[str, Any]:
        return {
            "infratools": {
                "version": 1,
                "host": self.host,
                "target_path": self.target_path,
                "existed": self.existed,
                "sha256": self.sha256,
                "size": self.size,
                "mode": self.mode,
                "uid": self.uid,
                "gid": self.gid,
                "deployment_kind": self.deployment_kind,
                "reload_argv": list(self.reload_argv) if self.reload_argv else None,
                "restore_command": self.restore_command,
                "created_at": self.created_at,
            }
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["id"] = str(self.id)
        data["job_id"] = str(self.job_id) if self.job_id else None
        data["reload_argv"] = list(self.reload_argv) if self.reload_argv else None
        return data


@dataclass
class RestoreReport:
    artifact_id: uuid.UUID
    host: str
    target_path: str
    action: Literal["restored", "removed"]
    verified: bool
    reload: ExecResult | None = None
    deployment_id: uuid.UUID | None = None

    @property
    def ok(self) -> bool:
        return self.verified and (self.reload is None or self.reload.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "artifact_id": str(self.artifact_id),
            "host": self.host,
            "target_path": self.target_path,
            "action": self.action,
            "verified": self.verified,
            "reload": None if self.reload is None else {"ok": self.reload.ok, "detail": None if self.reload.ok else self.reload.describe()},
            "deployment_id": str(self.deployment_id) if self.deployment_id else None,
        }


def _check_argv(argv: Sequence[str] | None, what: str) -> tuple[str, ...] | None:
    if argv is None:
        return None
    items = tuple(str(a) for a in argv)
    if not items or any(not a or any(c in a for c in ("\x00", "\n", "\r")) for a in items):
        raise InfraToolError(f"{what} must be a non-empty argv list of single-line words", code=INFRA_INVALID_ARGUMENT)
    return items


def restore_command(host: str, target: str, file: str, *, existed: bool, mode: int | None, reload_argv: Sequence[str] | None) -> str:
    """Human-readable command that restores the snapshot by hand (shown in reports and the manifest)."""
    if existed:
        cmd = shlex.join(["install", "-m", f"{mode if mode is not None else 0o644:04o}", "--", file, target])
        if host != LOCAL_HOST:
            cmd = (
                f"{shlex.join(['install', '-m', f'{mode if mode is not None else 0o644:04o}', '/dev/stdin', target])} < {shlex.quote(file)}"
            )
    else:
        cmd = shlex.join(["rm", "-f", "--", target])
    if reload_argv:
        cmd += " && " + shlex.join(list(reload_argv))
    return f"{cmd}  # on host {host}" if host != LOCAL_HOST else cmd


class RollbackStore:
    """Creates, loads, verifies and restores ``kind=rollback`` artifacts."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        base_dir: Path,
        files: TargetFiles,
        *,
        allowed_roots: Sequence[str] = DEFAULT_DEPLOY_ROOTS,
        redactor: Redactor = DEFAULT_REDACTOR,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.base_dir = base_dir
        self.files = files
        self.allowed_roots = tuple(str(PurePosixPath(r)) for r in allowed_roots)
        self.redactor = redactor

    # ------------------------------------------------------------------------------------------ helpers
    def check_target(self, host: str | None, path: str) -> str:
        """Validated target path; locally the real parent directory must stay below an allowed root."""
        clean = validate_target_path(path, self.allowed_roots)
        if HostExecutor.is_local(host):
            parent = os.path.realpath(os.path.dirname(clean))
            roots = [os.path.realpath(r) for r in self.allowed_roots]
            if not any(parent == r or parent.startswith(r.rstrip("/") + "/") for r in roots):
                raise InfraToolError(
                    f"'{path}' resolves outside the allowed deployment roots (symlinked directory?)", code=INFRA_PATH_INVALID
                )
        return clean

    async def check_remote_parent(self, host: str, target: str, *, ctx: InfraContext | None = None) -> None:
        """The real parent directory on the host must stay below an allowed root (symlinked directories)."""
        parent = str(PurePosixPath(target).parent)
        real = await self.files.realpath(host, parent, ctx=ctx)
        if real != parent and not any(_under(PurePosixPath(real), PurePosixPath(r)) or real == r for r in self.allowed_roots):
            raise InfraToolError(
                f"'{target}' on {host} resolves to '{real}', outside the allowed deployment roots", code=INFRA_PATH_INVALID
            )

    def _artifact_dir(self, job_id: uuid.UUID | None, artifact_id: uuid.UUID) -> Path:
        return self.base_dir / (str(job_id) if job_id else "adhoc") / ROLLBACK_KIND / str(artifact_id)

    # ------------------------------------------------------------------------------------------ create
    async def create(
        self,
        target_path: str,
        *,
        host: str | None = None,
        deployment_kind: str = "config",
        reload_argv: Sequence[str] | None = None,
        ctx: InfraContext | None = None,
    ) -> RollbackArtifact:
        ctx = ctx or InfraContext()
        host_id = self.files.executor.check_host(host)
        target = await asyncio.to_thread(self.check_target, host_id, target_path)
        reload = _check_argv(reload_argv, "reload_argv")
        if host_id != LOCAL_HOST:
            await self.check_remote_parent(host_id, target, ctx=ctx)
        snap = await self.files.snapshot(None if host_id == LOCAL_HOST else host_id, target, ctx=ctx)
        artifact_id = uuid.uuid4()
        directory = self._artifact_dir(ctx.job_id, artifact_id)
        name = _SAFE_NAME.sub("_", PurePosixPath(target).name)[:120] or "target"
        file = directory / (f"{name}.orig" if snap.existed else f"{name}.absent")
        artifact = RollbackArtifact(
            id=artifact_id,
            host=host_id,
            target_path=target,
            existed=snap.existed,
            sha256=snap.sha256,
            size=len(snap.content),
            file=str(file),
            restore_command=restore_command(host_id, target, str(file), existed=snap.existed, mode=snap.mode, reload_argv=reload),
            mode=snap.mode,
            uid=snap.uid,
            gid=snap.gid,
            deployment_kind=deployment_kind[:64],
            reload_argv=reload,
            job_id=ctx.job_id,
            created_at=datetime.now(UTC).isoformat(),
        )
        await asyncio.to_thread(self._write_files, directory, file, snap.content, artifact)
        try:
            async with self.sessionmaker() as session, session.begin():
                session.add(
                    Artifact(
                        id=artifact_id,
                        job_id=ctx.job_id,
                        step_id=ctx.step_id,
                        kind=ROLLBACK_KIND,
                        name=f"rollback {host_id}:{target}"[:300],
                        path=str(file),
                        media_type="application/octet-stream",
                        size_bytes=artifact.size,
                        sha256=artifact.sha256,
                        metadata_=artifact.metadata(),
                    )
                )
        except Exception as exc:
            await asyncio.to_thread(_rmtree_quiet, directory)
            raise InfraToolError(f"rollback artifact could not be stored: {type(exc).__name__}", code=INFRA_STORE_UNAVAILABLE) from exc
        log.info("rollback artifact created", extra={"artifact_id": str(artifact_id), "host": host_id, "target": target})
        return artifact

    @staticmethod
    def _write_files(directory: Path, file: Path, content: bytes, artifact: RollbackArtifact) -> None:
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        for parent in (directory.parent, directory.parent.parent):
            with contextlib.suppress(OSError):
                parent.chmod(0o700)
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        manifest = {"id": str(artifact.id), **artifact.metadata()["infratools"]}
        fd = os.open(directory / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)

    # ------------------------------------------------------------------------------------------ load/verify
    async def get(self, artifact_id: str | uuid.UUID) -> RollbackArtifact:
        try:
            key = artifact_id if isinstance(artifact_id, uuid.UUID) else uuid.UUID(str(artifact_id).strip())
        except ValueError:
            raise InfraToolError("rollback artifact id must be a UUID", code=INFRA_ROLLBACK_INVALID) from None
        async with self.sessionmaker() as session:
            row = (await session.execute(select(Artifact).where(Artifact.id == key))).scalar_one_or_none()
        if row is None:
            raise InfraToolError(f"rollback artifact {key} does not exist", code=INFRA_ROLLBACK_UNKNOWN)
        meta = (row.metadata_ or {}).get("infratools")
        if row.kind != ROLLBACK_KIND or not isinstance(meta, dict):
            raise InfraToolError(f"artifact {key} is not a rollback artifact of the infra tools", code=INFRA_ROLLBACK_INVALID)
        try:
            reload = meta.get("reload_argv")
            artifact = RollbackArtifact(
                id=row.id,
                host=str(meta["host"]),
                target_path=str(meta["target_path"]),
                existed=bool(meta["existed"]),
                sha256=str(row.sha256),
                size=int(row.size_bytes),
                file=str(row.path),
                restore_command=str(meta.get("restore_command") or ""),
                mode=None if meta.get("mode") is None else int(meta["mode"]),
                uid=None if meta.get("uid") is None else int(meta["uid"]),
                gid=None if meta.get("gid") is None else int(meta["gid"]),
                deployment_kind=str(meta.get("deployment_kind") or "config"),
                reload_argv=tuple(str(a) for a in reload) if isinstance(reload, list) and reload else None,
                job_id=row.job_id,
                created_at=str(meta.get("created_at") or ""),
            )
        except (KeyError, TypeError, ValueError):
            raise InfraToolError(f"rollback artifact {key} has invalid metadata", code=INFRA_ROLLBACK_INVALID) from None
        if str(meta.get("sha256")) != artifact.sha256:
            raise InfraToolError(f"rollback artifact {key}: checksum in metadata and row differ", code=INFRA_ROLLBACK_CORRUPT)
        # a tampered row must not be able to point a restore at arbitrary paths
        validate_target_path(artifact.target_path, self.allowed_roots)
        return artifact

    async def verify(self, artifact: RollbackArtifact) -> bytes:
        """The stored previous content after checking path, size and SHA-256 (row, manifest and file must agree)."""
        return await asyncio.to_thread(self._verify_sync, artifact)

    def _verify_sync(self, artifact: RollbackArtifact) -> bytes:
        base = self.base_dir.resolve()
        file = Path(artifact.file)
        resolved = file.resolve()
        if base not in resolved.parents or file.is_symlink():
            raise InfraToolError("rollback artifact file lies outside the artifact directory", code=INFRA_ROLLBACK_INVALID)
        try:
            content = resolved.read_bytes()
        except OSError:
            raise InfraToolError(f"rollback artifact file of {artifact.id} is missing", code=INFRA_ROLLBACK_CORRUPT) from None
        if len(content) != artifact.size or _sha256(content) != artifact.sha256:
            raise InfraToolError(
                f"rollback artifact {artifact.id} is corrupt (checksum mismatch) – the target was not touched", code=INFRA_ROLLBACK_CORRUPT
            )
        manifest_path = resolved.parent / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise InfraToolError(f"rollback manifest of {artifact.id} is missing or invalid", code=INFRA_ROLLBACK_CORRUPT) from None
        if (
            manifest.get("sha256") != artifact.sha256
            or manifest.get("target_path") != artifact.target_path
            or manifest.get("host") != artifact.host
        ):
            raise InfraToolError(f"rollback manifest of {artifact.id} does not match the artifact row", code=INFRA_ROLLBACK_CORRUPT)
        return content

    # ------------------------------------------------------------------------------------------ restore
    async def restore(
        self,
        artifact_id: str | uuid.UUID,
        *,
        reload: bool = True,
        record: bool = True,
        ctx: InfraContext | None = None,
    ) -> RestoreReport:
        """Put the snapshot back (checksum verified first); ``record`` writes a ``deployments`` row ``kind=restore:<kind>``."""
        ctx = ctx or InfraContext()
        artifact = await self.get(artifact_id)
        content = await self.verify(artifact)
        report = await self._apply(artifact, content, reload=reload, ctx=ctx)
        if record:
            report.deployment_id = await self._record_restore(artifact, report, ctx)
        return report

    async def _apply(self, artifact: RollbackArtifact, content: bytes, *, reload: bool, ctx: InfraContext) -> RestoreReport:
        host = None if artifact.host == LOCAL_HOST else artifact.host
        if host is None:
            await asyncio.to_thread(self.check_target, None, artifact.target_path)
        else:
            await self.check_remote_parent(host, artifact.target_path, ctx=ctx)
        if artifact.existed:
            await self.files.write(
                host,
                artifact.target_path,
                content,
                mode=artifact.mode if artifact.mode is not None else 0o644,
                uid=artifact.uid,
                gid=artifact.gid,
                ctx=ctx,
            )
            current = await self.files.checksum(host, artifact.target_path, ctx=ctx)
            verified = current == artifact.sha256
            action: Literal["restored", "removed"] = "restored"
        else:
            await self.files.remove(host, artifact.target_path, ctx=ctx)
            verified = await self.files.checksum(host, artifact.target_path, ctx=ctx) is None
            action = "removed"
        result: ExecResult | None = None
        if reload and artifact.reload_argv:
            result = await self.files.executor.run(
                list(artifact.reload_argv), host=host, timeout=self.files.timeout, classification="mutate", ctx=ctx
            )
        log.info(
            "rollback artifact restored",
            extra={"artifact_id": str(artifact.id), "host": artifact.host, "target": artifact.target_path, "verified": verified},
        )
        return RestoreReport(artifact.id, artifact.host, artifact.target_path, action, verified, result)

    async def _record_restore(self, artifact: RollbackArtifact, report: RestoreReport, ctx: InfraContext) -> uuid.UUID:
        now = datetime.now(UTC)
        status: DeploymentStatus = "succeeded" if report.ok else "failed"
        row = Deployment(
            job_id=ctx.job_id,
            step_id=ctx.step_id,
            target=f"{artifact.host}:{artifact.target_path}"[:200],
            kind=f"restore:{artifact.deployment_kind}"[:64],
            status=status,
            manifest={"action": report.action, "verified": report.verified, "sha256": artifact.sha256, "actor": ctx.actor},
            rollback_artifact_id=artifact.id,
            finished_at=now,
        )
        async with self.sessionmaker() as session, session.begin():
            session.add(row)
            await session.flush()
            await append_event(
                session,
                EventType.DEPLOYMENT_FINISHED,
                source_type=SOURCE_TYPE,
                source_id=str(row.id),
                job_id=ctx.job_id,
                step_id=ctx.step_id,
                attempt_id=ctx.attempt_id,
                severity=Severity.info if report.ok else Severity.error,
                payload={
                    "deployment_id": str(row.id),
                    "action": "rollback_restore",
                    "status": status,
                    "target": row.target,
                    "rollback_artifact_id": str(artifact.id),
                    "verified": report.verified,
                },
            )
        return row.id


def _rmtree_quiet(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


# ============================================================================================ deployer
@dataclass
class DeploymentReport:
    deployment_id: uuid.UUID | None
    status: DeploymentStatus
    host: str
    target_path: str
    kind: str
    rollback: RollbackArtifact | None = None
    sha256: str = ""
    previous_sha256: str | None = None
    check: ExecResult | None = None
    reload: ExecResult | None = None
    restore: RestoreReport | None = None
    error: str | None = None
    code: str | None = None
    steps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        def res(r: ExecResult | None) -> dict[str, Any] | None:
            return None if r is None else {"ok": r.ok, "command": r.display, "detail": None if r.ok else r.describe(800)}

        return {
            "ok": self.ok,
            "deployment_id": str(self.deployment_id) if self.deployment_id else None,
            "status": self.status,
            "host": self.host,
            "target_path": self.target_path,
            "kind": self.kind,
            "rollback_artifact_id": str(self.rollback.id) if self.rollback else None,
            "restore_command": self.rollback.restore_command if self.rollback else None,
            "sha256": self.sha256,
            "previous_sha256": self.previous_sha256,
            "check": res(self.check),
            "reload": res(self.reload),
            "restore": self.restore.to_dict() if self.restore else None,
            "error": self.error,
            "code": self.code,
            "steps": self.steps,
        }


class ConfigDeployer:
    """Writes one configuration file with rollback artifact, post-check, reload and automatic restore."""

    def __init__(self, store: RollbackStore, *, stale_after: timedelta = STALE_DEPLOYMENT) -> None:
        self.store = store
        self.files = store.files
        self.executor = store.files.executor
        self.sessionmaker = store.sessionmaker
        self.stale_after = stale_after

    async def deploy(
        self,
        target_path: str,
        content: bytes | str,
        *,
        host: str | None = None,
        kind: str = "config",
        mode: int | None = None,
        check_argv: Sequence[str] | None = None,
        reload_argv: Sequence[str] | None = None,
        ctx: InfraContext | None = None,
    ) -> DeploymentReport:
        ctx = ctx or InfraContext()
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        if len(data) > self.files.max_bytes:
            raise InfraToolError(f"content larger than {self.files.max_bytes} bytes", code=INFRA_INVALID_ARGUMENT)
        if mode is not None and not 0 <= mode <= 0o7777:
            raise InfraToolError("mode must be an octal permission value (0..0o7777)", code=INFRA_INVALID_ARGUMENT)
        check = _check_argv(check_argv, "check_argv")
        reload = _check_argv(reload_argv, "reload_argv")
        host_id = self.executor.check_host(host)
        target = await asyncio.to_thread(self.store.check_target, host_id, target_path)
        remote = None if host_id == LOCAL_HOST else host_id
        report = DeploymentReport(None, "started", host_id, target, kind[:64], sha256=_sha256(data))

        report.deployment_id = await self._claim(report, ctx)
        report.steps.append("claimed")
        try:
            report.rollback = await self.store.create(target, host=remote, deployment_kind=kind, reload_argv=reload, ctx=ctx)
            report.previous_sha256 = report.rollback.sha256 if report.rollback.existed else None
            await self._attach_rollback(report)
        except InfraToolError as exc:
            await self._finish(report, "failed", ctx, error=f"rollback artifact could not be created: {exc.message}", code=exc.code)
            return report
        except BaseException as exc:  # nothing was written yet – just release the claim
            await asyncio.shield(self._finish(report, "failed", ctx, error=f"interrupted: {type(exc).__name__}", code=INFRA_DEPLOY_FAILED))
            raise
        report.steps.append("rollback_artifact")

        rollback = report.rollback
        try:
            await self.files.write(
                remote,
                target,
                data,
                mode=mode if mode is not None else (rollback.mode if rollback.mode is not None else 0o644),
                uid=rollback.uid,
                gid=rollback.gid,
                ctx=ctx,
            )
            report.steps.append("written")
            written = await self.files.checksum(remote, target, ctx=ctx)
            if written != report.sha256:
                return await self._roll_back(report, ctx, "the written file does not match the deployed content", INFRA_DEPLOY_FAILED)
            report.steps.append("verified")
            if check is not None:
                report.check = await self.executor.run(list(check), host=remote, timeout=self.files.timeout, ctx=ctx)
                if not report.check.ok:
                    return await self._roll_back(report, ctx, f"post-check failed: {report.check.describe(800)}", INFRA_DEPLOY_FAILED)
                report.steps.append("checked")
            if reload is not None:
                report.reload = await self.executor.run(
                    list(reload), host=remote, timeout=self.files.timeout, classification="mutate", ctx=ctx
                )
                if not report.reload.ok:
                    return await self._roll_back(report, ctx, f"reload failed: {report.reload.describe(800)}", INFRA_DEPLOY_FAILED)
                report.steps.append("reloaded")
        except InfraToolError as exc:
            return await self._roll_back(report, ctx, exc.message, exc.code)
        except BaseException as exc:  # cancellation or a bug: never leave a half-deployed config behind
            await asyncio.shield(self._roll_back(report, ctx, f"interrupted: {type(exc).__name__}", INFRA_DEPLOY_FAILED))
            raise
        await self._finish(report, "succeeded", ctx)
        return report

    # ------------------------------------------------------------------------------------------ steps
    async def _claim(self, report: DeploymentReport, ctx: InfraContext) -> uuid.UUID:
        """Insert the ``started`` row – refused while another deployment of the same target is running."""
        target_key = f"{report.host}:{report.target_path}"[:200]
        now = datetime.now(UTC)
        async with self.sessionmaker() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 2808))"), {"k": target_key})
            running = (
                await session.execute(
                    select(Deployment.id).where(
                        Deployment.target == target_key,
                        Deployment.status == "started",
                        Deployment.created_at > now - self.stale_after,
                    )
                )
            ).first()
            if running is not None:
                raise InfraToolError(f"deployment {running[0]} of {target_key} is still in progress", code=INFRA_DEPLOY_FAILED)
            row = Deployment(
                job_id=ctx.job_id,
                step_id=ctx.step_id,
                target=target_key,
                kind=report.kind,
                status="started",
                manifest={"host": report.host, "path": report.target_path, "sha256": report.sha256, "actor": ctx.actor},
            )
            session.add(row)
            await session.flush()
            await append_event(
                session,
                EventType.DEPLOYMENT_STARTED,
                source_type=SOURCE_TYPE,
                source_id=str(row.id),
                job_id=ctx.job_id,
                step_id=ctx.step_id,
                attempt_id=ctx.attempt_id,
                payload={"deployment_id": str(row.id), "target": target_key, "kind": report.kind, "sha256": report.sha256},
            )
            return row.id

    async def _attach_rollback(self, report: DeploymentReport) -> None:
        assert report.rollback is not None
        async with self.sessionmaker() as session, session.begin():
            row = await session.get(Deployment, report.deployment_id)
            if row is not None:
                row.rollback_artifact_id = report.rollback.id
                row.manifest = {**(row.manifest or {}), "previous_sha256": report.previous_sha256, "existed": report.rollback.existed}

    async def _roll_back(self, report: DeploymentReport, ctx: InfraContext, error: str, code: str | None) -> DeploymentReport:
        assert report.rollback is not None
        try:
            content = await self.store.verify(report.rollback)
            report.restore = await self.store._apply(report.rollback, content, reload=report.reload is not None, ctx=ctx)
            status: DeploymentStatus = "rolled_back" if report.restore.ok else "rollback_failed"
            if not report.restore.ok:
                error += " – restore could not be verified"
        except InfraToolError as exc:
            status = "rollback_failed"
            error += f" – restore failed: {exc.message}"
        report.steps.append(status)
        await self._finish(report, status, ctx, error=error, code=code)
        return report

    async def _finish(
        self, report: DeploymentReport, status: DeploymentStatus, ctx: InfraContext, *, error: str | None = None, code: str | None = None
    ) -> None:
        report.status = status
        report.error = self.store.redactor.text(error) if error else None
        report.code = code
        summary = {
            k: v for k, v in report.to_dict().items() if k in ("status", "rollback_artifact_id", "check", "reload", "restore", "error")
        }
        try:
            async with self.sessionmaker() as session, session.begin():
                row = await session.get(Deployment, report.deployment_id)
                if row is not None:
                    row.status = status
                    row.finished_at = datetime.now(UTC)
                    row.manifest = {**(row.manifest or {}), "result": summary}
                await append_event(
                    session,
                    EventType.DEPLOYMENT_FINISHED,
                    source_type=SOURCE_TYPE,
                    source_id=str(report.deployment_id),
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    attempt_id=ctx.attempt_id,
                    severity=Severity.info if status == "succeeded" else Severity.error,
                    payload={
                        "deployment_id": str(report.deployment_id),
                        "target": f"{report.host}:{report.target_path}",
                        "kind": report.kind,
                        "status": status,
                        "rollback_artifact_id": str(report.rollback.id) if report.rollback else None,
                        "error": report.error,
                    },
                )
        except Exception as exc:  # the file state is already final – report, do not mask the deployment result
            log.warning("deployment row could not be finished", extra={"deployment_id": str(report.deployment_id), "error": str(exc)[:300]})


__all__ = [
    "DEFAULT_DEPLOY_ROOTS",
    "MAX_ROLLBACK_BYTES",
    "ROLLBACK_KIND",
    "ConfigDeployer",
    "DeploymentReport",
    "DeploymentStatus",
    "FileSnapshot",
    "RestoreReport",
    "RollbackArtifact",
    "RollbackStore",
    "TargetFiles",
    "restore_command",
    "validate_target_path",
]
