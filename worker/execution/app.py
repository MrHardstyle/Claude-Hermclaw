"""Execution worker daemon for host ``.222`` (P07 7.6).

Endpoints (everything except ``/health`` must be signed by the orchestrator, see :mod:`worker.common.auth`):

=====================================  ===================================================================
``GET    /health``                      liveness/readiness (unauthenticated)
``PUT    /v1/workspaces/{ws}``          upload a tar (``?mode=replace|merge``), extracted safely
``GET    /v1/workspaces/{ws}``          workspace info (files, bytes)
``GET    /v1/workspaces/{ws}/manifest`` ``path -> sha256`` manifest
``GET    /v1/workspaces/{ws}/archive``  tar of the workspace (``?paths=`` to restrict)
``POST   /v1/workspaces/{ws}/delete``   delete paths inside the workspace
``DELETE /v1/workspaces/{ws}``          remove the workspace
``POST   /v1/commands``                 run a command in the sandbox (idempotent per ``request_id``)
``GET    /v1/commands/{request_id}``    status/result of an earlier command
``POST   /v1/containers/recover``       remove leftover Hermclaw sandbox containers
``POST   /v1/selftest``                 end-to-end self check (workspace, disk, engine, sandbox run)
=====================================  ===================================================================

Workspace concurrency: commands, archive and manifest reads hold a shared lock of the workspace;
uploads and deletes an exclusive one, so a sandbox never sees a half-replaced tree.

The tar helpers live in :mod:`worker.execution.workspaces` and the sandbox in
:mod:`worker.execution.sandbox` (owned by the sandbox component); both are imported lazily and can be
injected via :func:`create_app` (tests, alternative runners).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

import httpx
from fastapi import Body, FastAPI, Query, Request, Response
from fastapi import Path as PathParam

from hermclaw.contracts.worker import CommandRequest, CommandResult, WorkspaceSyncManifest
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.workers.auth import TokenFile
from hermclaw.workers.schemas import (
    WORKSPACE_ID_PATTERN,
    CommandStatus,
    DeletePathsRequest,
    DeletePathsResult,
    RecoverResult,
    SelftestCheck,
    SelftestRequest,
    SelftestResult,
    WorkspaceInfo,
)
from worker.common.errors import DaemonError, bad_request, conflict, not_found, unavailable
from worker.common.heartbeat import HeartbeatExtras
from worker.common.server import create_daemon_app
from worker.common.settings import WorkerDaemonSettings
from worker.common.state import DaemonState
from worker.common.system import disk_free_mb

log = get_logger(__name__)

TAR_MEDIA_TYPE = "application/x-tar"
MAX_FINISHED_COMMANDS = 1000
MIN_FREE_DISK_MB = 1024
SELFTEST_MARKER = "hermclaw-selftest-ok"
WorkspaceParam = Annotated[str, PathParam(pattern=WORKSPACE_ID_PATTERN, max_length=128)]
RequestIdParam = Annotated[str, PathParam(min_length=1, max_length=200)]


class CommandRunner(Protocol):
    """Structural twin of ``worker.execution.sandbox.SandboxRunner``."""

    async def run(self, req: CommandRequest, workspace_dir: Path) -> CommandResult: ...


@dataclass(frozen=True)
class WorkspaceOps:
    """The three tar/manifest helpers of :mod:`worker.execution.workspaces`."""

    extract: Callable[[bytes, Path], None]
    build_tar: Callable[[Path, list[str] | None], bytes]
    manifest: Callable[[Path], dict[str, str]]


def default_workspace_ops() -> WorkspaceOps:
    from worker.execution.workspaces import build_tar, extract_tar_safely, manifest

    return WorkspaceOps(extract=extract_tar_safely, build_tar=build_tar, manifest=manifest)


def default_runner(settings: WorkerDaemonSettings) -> CommandRunner:
    from worker.execution.sandbox import make_sandbox

    runner: CommandRunner = make_sandbox(settings.sandbox)
    return runner


# ---------------------------------------------------------------------------------------------- locking
class RWLock:
    """Asyncio reader/writer lock with writer preference (no writer starvation)."""

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @property
    def idle(self) -> bool:
        return not self._writer and self._readers == 0 and self._waiting_writers == 0

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._cond:
            await self._cond.wait_for(lambda: not self._writer and self._waiting_writers == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._cond:
            self._waiting_writers += 1
            try:
                await self._cond.wait_for(lambda: not self._writer and self._readers == 0)
            finally:
                self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            async with self._cond:
                self._writer = False
                self._cond.notify_all()


# ---------------------------------------------------------------------------------------------- helpers
def _workspace_stats(path: Path) -> tuple[int, int]:
    files = size = 0
    for root, _dirs, names in os.walk(path, followlinks=False):
        for name in names:
            files += 1
            with contextlib.suppress(OSError):
                size += os.lstat(os.path.join(root, name)).st_size
    return files, size


def _safe_relative(raw: str) -> Path:
    if not raw or "\x00" in raw:
        raise bad_request("empty or invalid path", "WORKSPACE_PATH_INVALID", path=raw[:200])
    rel = Path(raw)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise bad_request("paths must be relative and stay inside the workspace", "WORKSPACE_PATH_INVALID", path=raw[:200])
    return rel


def _delete_paths(root: Path, paths: list[str]) -> tuple[list[str], list[str]]:
    real_root = root.resolve(strict=True)
    deleted: list[str] = []
    missing: list[str] = []
    for raw in paths:
        rel = _safe_relative(raw)
        target = root / rel
        # the parent must resolve inside the workspace (no symlinked directory escapes); the final
        # component itself is never followed (a symlink is removed, not its target)
        parent = target.parent.resolve(strict=False)
        if parent != real_root and real_root not in parent.parents:
            raise bad_request("path escapes the workspace", "WORKSPACE_PATH_INVALID", path=raw[:200])
        if not target.exists() and not target.is_symlink():
            missing.append(raw)
            continue
        if target.is_symlink() or not target.is_dir():
            target.unlink()
        else:
            shutil.rmtree(target)
        deleted.append(raw)
    return deleted, missing


def _fingerprint(req: CommandRequest) -> str:
    return hashlib.sha256(json.dumps(req.model_dump(mode="json"), sort_keys=True).encode("utf-8")).hexdigest()


async def _exec(*argv: str, timeout_seconds: float = 60.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return -1, "", f"timed out after {timeout_seconds:.0f} s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


@dataclass
class CommandEntry:
    request: CommandRequest
    fingerprint: str
    task: asyncio.Task[CommandResult]


@dataclass
class ExecutionService:
    settings: WorkerDaemonSettings
    state: DaemonState
    runner: CommandRunner
    ops: WorkspaceOps
    locks: dict[str, RWLock] = field(default_factory=dict)
    commands: OrderedDict[str, CommandEntry] = field(default_factory=OrderedDict)
    engine_version: str | None = None
    engine_error: str | None = None

    @property
    def root(self) -> Path:
        return self.settings.workspaces_dir

    @property
    def engine(self) -> Literal["podman", "docker", "local"]:
        return self.settings.sandbox.engine

    def workspace_dir(self, ws: str) -> Path:
        return self.root / ws

    def lock(self, ws: str) -> RWLock:
        lk = self.locks.get(ws)
        if lk is None:
            lk = self.locks[ws] = RWLock()
        return lk

    def require_workspace(self, ws: str) -> Path:
        path = self.workspace_dir(ws)
        if not path.is_dir():
            raise not_found(f"workspace '{ws}' does not exist", "WORKSPACE_NOT_FOUND", workspace=ws)
        return path

    @property
    def running(self) -> int:
        return sum(1 for e in self.commands.values() if not e.task.done())

    # ------------------------------------------------------------------ startup / probes
    async def probe_engine(self) -> None:
        if self.engine == "local":
            self.engine_version, self.engine_error = "local", None
            return
        exe = shutil.which(self.engine)
        if exe is None:
            self.engine_version, self.engine_error = None, f"sandbox engine '{self.engine}' not found"
            return
        rc, out, err = await _exec(exe, "--version", timeout_seconds=20)
        if rc != 0:
            self.engine_version, self.engine_error = None, f"'{self.engine} --version' failed: {(err or out).strip()[:200]}"
            return
        self.engine_version, self.engine_error = out.strip().split()[-1] if out.strip() else "unknown", None

    def workspaces_writable(self) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / f".probe-{uuid.uuid4().hex}"
            probe.write_bytes(b"")
            probe.unlink()
            return True
        except OSError:
            return False

    async def health_checks(self) -> dict[str, bool]:
        writable = await asyncio.to_thread(self.workspaces_writable)
        return {"workspaces_writable": writable, "sandbox_engine": self.engine_error is None}

    async def extras(self) -> HeartbeatExtras:
        await self.probe_engine()
        writable = await asyncio.to_thread(self.workspaces_writable)
        error = self.engine_error or (None if writable else f"workspaces directory {self.root} is not writable")
        versions = {f"sandbox.{self.engine}": self.engine_version} if self.engine_version else {}
        return HeartbeatExtras(service_versions=versions, readiness_error=error)

    # ------------------------------------------------------------------ workspaces
    async def upload(self, ws: str, data: bytes, mode: Literal["replace", "merge"]) -> WorkspaceInfo:
        if not self.state.accepting_work:
            raise unavailable("worker is draining", "WORKER_DRAINING")
        if not data:
            raise bad_request("empty upload", "WORKSPACE_UPLOAD_EMPTY", workspace=ws)
        loop = asyncio.get_running_loop()
        started = loop.time()
        target = self.workspace_dir(ws)
        async with self.lock(ws).write():
            await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)
            if mode == "merge":
                await asyncio.to_thread(target.mkdir, exist_ok=True)
                await self._extract(data, target, ws)
            else:
                incoming = self.root / f".incoming-{ws}-{uuid.uuid4().hex[:8]}"
                await asyncio.to_thread(incoming.mkdir)
                try:
                    await self._extract(data, incoming, ws)
                    await asyncio.to_thread(self._swap, incoming, target)
                finally:
                    await asyncio.to_thread(shutil.rmtree, incoming, True)
            files, size = await asyncio.to_thread(_workspace_stats, target)
        return WorkspaceInfo(workspace=ws, exists=True, files=files, bytes=size, mode=mode, duration_ms=int((loop.time() - started) * 1000))

    async def _extract(self, data: bytes, dest: Path, ws: str) -> None:
        try:
            await asyncio.to_thread(self.ops.extract, data, dest)
        except DaemonError:
            raise
        except Exception as exc:  # the extraction helper rejects unsafe/corrupt archives with exceptions
            code = getattr(exc, "code", None) or "WORKSPACE_ARCHIVE_INVALID"
            raise bad_request(f"archive rejected: {exc}"[:500], str(code), workspace=ws) from exc

    def _swap(self, incoming: Path, target: Path) -> None:
        trash = self.root / f".trash-{target.name}-{uuid.uuid4().hex[:8]}"
        if target.exists():
            target.rename(trash)
        incoming.rename(target)
        shutil.rmtree(trash, ignore_errors=True)

    async def info(self, ws: str) -> WorkspaceInfo:
        path = self.require_workspace(ws)
        async with self.lock(ws).read():
            files, size = await asyncio.to_thread(_workspace_stats, path)
        return WorkspaceInfo(workspace=ws, exists=True, files=files, bytes=size)

    async def manifest(self, ws: str) -> WorkspaceSyncManifest:
        path = self.require_workspace(ws)
        async with self.lock(ws).read():
            files = await asyncio.to_thread(self.ops.manifest, path)
        return WorkspaceSyncManifest(workspace=ws, files=dict(files))

    async def archive(self, ws: str, paths: list[str] | None) -> bytes:
        path = self.require_workspace(ws)
        for p in paths or []:
            _safe_relative(p)
        async with self.lock(ws).read():
            try:
                return await asyncio.to_thread(self.ops.build_tar, path, paths or None)
            except FileNotFoundError as exc:
                raise not_found(f"path not found in workspace: {exc.filename or exc}", "WORKSPACE_PATH_NOT_FOUND", workspace=ws) from exc
            except ValueError as exc:
                raise bad_request(str(exc)[:500], "WORKSPACE_PATH_INVALID", workspace=ws) from exc

    async def delete_paths(self, ws: str, paths: list[str]) -> DeletePathsResult:
        path = self.require_workspace(ws)
        async with self.lock(ws).write():
            deleted, missing = await asyncio.to_thread(_delete_paths, path, paths)
        return DeletePathsResult(workspace=ws, deleted=deleted, missing=missing)

    async def delete_workspace(self, ws: str) -> WorkspaceInfo:
        path = self.workspace_dir(ws)
        lk = self.lock(ws)
        async with lk.write():
            existed = path.exists()
            if existed:
                await asyncio.to_thread(shutil.rmtree, path)
        if lk.idle:
            self.locks.pop(ws, None)
        return WorkspaceInfo(workspace=ws, exists=False, mode="none")

    # ------------------------------------------------------------------ commands
    async def run_command(self, req: CommandRequest) -> CommandResult:
        fingerprint = _fingerprint(req)
        existing = self.commands.get(req.request_id)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise conflict("request_id was already used for a different command", "REQUEST_ID_CONFLICT", request_id=req.request_id)
            return await asyncio.shield(existing.task)
        if not self.state.accepting_work:
            raise unavailable("worker is draining", "WORKER_DRAINING")
        if self.running >= self.settings.max_concurrent_commands:
            raise DaemonError(
                "no free execution slot",
                code="WORKER_BUSY",
                status=503,
                details={"running": self.running, "max": self.settings.max_concurrent_commands},
            )
        ws_dir = self.require_workspace(req.workspace)
        task = asyncio.create_task(self._execute(req, ws_dir), name=f"command-{req.request_id}")
        self.commands[req.request_id] = CommandEntry(request=req, fingerprint=fingerprint, task=task)
        self._evict_finished()
        return await asyncio.shield(task)

    async def _execute(self, req: CommandRequest, ws_dir: Path) -> CommandResult:
        async with self.lock(req.workspace).read():
            with self.state.work(req.request_id, kind="command", job_id=req.job_id, step_id=req.step_id):
                if not await asyncio.to_thread(ws_dir.is_dir):
                    return CommandResult(request_id=req.request_id, exit_code=None, sandbox=self.engine, error="workspace disappeared")
                try:
                    result = await self.runner.run(req, ws_dir)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("sandbox runner failed", extra={"request_id": req.request_id})
                    result = CommandResult(
                        request_id=req.request_id,
                        exit_code=None,
                        sandbox=self.engine,
                        error=f"sandbox error: {type(exc).__name__}: {exc}"[:2000],
                    )
        return result.model_copy(
            update={
                "request_id": req.request_id,
                "stdout": DEFAULT_REDACTOR.text(result.stdout),
                "stderr": DEFAULT_REDACTOR.text(result.stderr),
                "error": DEFAULT_REDACTOR.text(result.error) if result.error else None,
            }
        )

    def _evict_finished(self) -> None:
        finished = [k for k, e in self.commands.items() if e.task.done()]
        for key in finished[: max(0, len(finished) - MAX_FINISHED_COMMANDS)]:
            self.commands.pop(key, None)

    def command_status(self, request_id: str) -> CommandStatus:
        entry = self.commands.get(request_id)
        if entry is None:
            raise not_found(f"unknown command '{request_id}'", "COMMAND_NOT_FOUND", request_id=request_id)
        if not entry.task.done():
            return CommandStatus(request_id=request_id, status="running")
        return CommandStatus(request_id=request_id, status="finished", result=entry.task.result())

    # ------------------------------------------------------------------ containers
    async def recover_containers(self, *, force: bool) -> RecoverResult:
        """Remove containers carrying ``WORKER_CONTAINER_LABEL`` (leftovers of crashed runs)."""
        if self.engine == "local":
            return RecoverResult(engine="local")
        if self.running and not force:
            raise conflict("commands are running; use force=true to remove their containers too", "COMMANDS_RUNNING")
        exe = shutil.which(self.engine)
        if exe is None:
            raise unavailable(f"sandbox engine '{self.engine}' not found", "SANDBOX_ENGINE_MISSING")
        label = self.settings.container_label
        rc, out, err = await _exec(exe, "ps", "-a", "--filter", f"label={label}", "--format", "{{.Names}}")
        if rc != 0:
            raise DaemonError(f"listing containers failed: {err.strip()[:300]}", code="CONTAINER_LIST_FAILED", status=502)
        removed: list[str] = []
        errors: list[str] = []
        for name in sorted({n.strip() for n in out.splitlines() if n.strip()}):
            rc, _out, err = await _exec(exe, "rm", "-f", name, timeout_seconds=120)
            if rc == 0:
                removed.append(name)
            else:
                errors.append(f"{name}: {err.strip()[:200]}")
        if removed or errors:
            log.info("sandbox containers recovered", extra={"removed": removed, "errors": errors, "label": label})
        return RecoverResult(removed=removed, errors=errors, engine=self.engine)

    async def startup_recovery(self) -> None:
        await self.probe_engine()
        if self.engine_error is not None:
            log.error("sandbox engine unavailable", extra={"error": self.engine_error})
            return
        try:
            await self.recover_containers(force=True)
        except DaemonError as exc:
            log.warning("startup container recovery failed", extra={"code": exc.code, "error": exc.message})
        # leftovers of an interrupted replace-upload
        if self.root.is_dir():
            for p in self.root.iterdir():
                if p.name.startswith((".incoming-", ".trash-")):
                    await asyncio.to_thread(shutil.rmtree, p, True)

    # ------------------------------------------------------------------ selftest
    async def selftest(self) -> SelftestResult:
        loop = asyncio.get_running_loop()
        checks: list[SelftestCheck] = []

        async def timed(name: str, coro: Any) -> None:
            t0 = loop.time()
            try:
                ok, detail = await coro
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"[:300]
            checks.append(SelftestCheck(name=name, ok=ok, detail=detail, duration_ms=int((loop.time() - t0) * 1000)))

        async def writable() -> tuple[bool, str]:
            ok = await asyncio.to_thread(self.workspaces_writable)
            return ok, str(self.root)

        async def disk() -> tuple[bool, str]:
            free = await asyncio.to_thread(disk_free_mb, self.root)
            return free >= MIN_FREE_DISK_MB, f"{free} MiB free"

        async def engine() -> tuple[bool, str]:
            await self.probe_engine()
            return self.engine_error is None, self.engine_error or f"{self.engine} {self.engine_version}"

        async def sandbox_run() -> tuple[bool, str]:
            ws = f"selftest-{uuid.uuid4().hex[:12]}"
            path = self.workspace_dir(ws)
            await asyncio.to_thread(path.mkdir, parents=True)
            try:
                req = CommandRequest(
                    request_id=f"selftest-{uuid.uuid4().hex}",
                    job_id="selftest",
                    step_id="selftest",
                    workspace=ws,
                    command=f"echo {SELFTEST_MARKER}",
                    timeout_seconds=120,
                )
                result = await self._execute(req, path)
            finally:
                await asyncio.to_thread(shutil.rmtree, path, True)
            ok = result.exit_code == 0 and SELFTEST_MARKER in result.stdout
            return ok, result.error or f"exit {result.exit_code} in {result.duration_ms} ms ({result.sandbox})"

        await timed("workspaces_writable", writable())
        await timed("disk_free", disk())
        await timed("sandbox_engine", engine())
        if checks[-1].ok:
            await timed("sandbox_run", sandbox_run())
        return SelftestResult(ok=all(c.ok for c in checks), worker_id=self.settings.worker_id, checks=checks)


def _service(request: Request) -> ExecutionService:
    svc = request.app.state.execution
    assert isinstance(svc, ExecutionService)
    return svc


def create_app(
    settings: WorkerDaemonSettings,
    *,
    runner: CommandRunner | None = None,
    workspace_ops: WorkspaceOps | None = None,
    token_file: TokenFile | None = None,
    heartbeat: bool = True,
    heartbeat_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Execution worker app. ``runner``/``workspace_ops`` default to the sandbox component's modules."""
    state = DaemonState(settings.worker_id, settings.kind)
    svc = ExecutionService(
        settings=settings,
        state=state,
        runner=runner if runner is not None else default_runner(settings),
        ops=workspace_ops if workspace_ops is not None else default_workspace_ops(),
    )
    app = create_daemon_app(
        settings,
        title="Hermclaw execution worker",
        token_file=token_file,
        state=state,
        health_checks=svc.health_checks,
        extras=svc.extras,
        heartbeat=heartbeat,
        heartbeat_transport=heartbeat_transport,
        on_startup=(svc.startup_recovery,),
    )
    app.state.execution = svc

    @app.put("/v1/workspaces/{ws}", response_model=WorkspaceInfo)
    async def upload_workspace(ws: WorkspaceParam, request: Request, mode: Literal["replace", "merge"] = "replace") -> WorkspaceInfo:
        return await _service(request).upload(ws, await request.body(), mode)

    @app.get("/v1/workspaces/{ws}", response_model=WorkspaceInfo)
    async def workspace_info(ws: WorkspaceParam, request: Request) -> WorkspaceInfo:
        return await _service(request).info(ws)

    @app.get("/v1/workspaces/{ws}/manifest", response_model=WorkspaceSyncManifest)
    async def workspace_manifest(ws: WorkspaceParam, request: Request) -> WorkspaceSyncManifest:
        return await _service(request).manifest(ws)

    @app.get("/v1/workspaces/{ws}/archive")
    async def workspace_archive(ws: WorkspaceParam, request: Request, paths: Annotated[list[str] | None, Query()] = None) -> Response:
        data = await _service(request).archive(ws, paths)
        return Response(content=data, media_type=TAR_MEDIA_TYPE)

    @app.post("/v1/workspaces/{ws}/delete", response_model=DeletePathsResult)
    async def workspace_delete_paths(ws: WorkspaceParam, request: Request, body: DeletePathsRequest) -> DeletePathsResult:
        return await _service(request).delete_paths(ws, body.paths)

    @app.delete("/v1/workspaces/{ws}", response_model=WorkspaceInfo)
    async def delete_workspace(ws: WorkspaceParam, request: Request) -> WorkspaceInfo:
        return await _service(request).delete_workspace(ws)

    @app.post("/v1/commands", response_model=CommandResult)
    async def run_command(request: Request, body: Annotated[CommandRequest, Body()]) -> CommandResult:
        return await _service(request).run_command(body)

    @app.get("/v1/commands/{request_id}", response_model=CommandStatus)
    async def command_status(request_id: RequestIdParam, request: Request) -> CommandStatus:
        return _service(request).command_status(request_id)

    @app.post("/v1/containers/recover", response_model=RecoverResult)
    async def recover_containers(request: Request, force: bool = False) -> RecoverResult:
        return await _service(request).recover_containers(force=force)

    @app.post("/v1/selftest", response_model=SelftestResult)
    async def selftest(request: Request, body: Annotated[SelftestRequest | None, Body()] = None) -> SelftestResult:
        return await _service(request).selftest()

    return app
