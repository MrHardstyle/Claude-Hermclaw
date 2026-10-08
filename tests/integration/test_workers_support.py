"""Test-only support for the P07 worker tests (no tests in here).

- settings/token helpers for the daemons
- :class:`LocalTestRunner` – runs commands as real subprocesses in the workspace (stand-in for the
  podman sandbox of the sandbox component, used where the sandbox module is not under test)
- :func:`workspace_ops` – the sandbox component's tar helpers when present, else a tarfile-based fallback
- :class:`FakeOllama` – a real aiohttp HTTP server emulating the Ollama endpoints the model worker uses
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import os
import tarfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web
from fastapi import FastAPI

from hermclaw.contracts.common import WorkerKind
from hermclaw.contracts.worker import CommandRequest, CommandResult
from worker.common.settings import WorkerDaemonSettings
from worker.execution.app import WorkspaceOps

TOKEN = "s" * 40 + "-integration-worker-token"


def write_token(directory: Path, token: str = TOKEN) -> Path:
    path = directory / "worker-token"
    path.write_text(f"# test token\n{token}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def make_settings(tmp_path: Path, kind: WorkerKind, worker_id: str, **overrides: Any) -> WorkerDaemonSettings:
    values: dict[str, Any] = {
        "worker_id": worker_id,
        "kind": kind,
        "token_file": write_token(tmp_path),
        "data_dir": tmp_path / "data",
        "hostname": f"{worker_id}.test",
    }
    values.update(overrides)
    return WorkerDaemonSettings(**values)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[FastAPI]:
    """Run the app's lifespan (httpx.ASGITransport does not)."""
    async with app.router.lifespan_context(app):
        yield app


class LocalTestRunner:
    """Runs ``sh -c <command>`` in the workspace directory (test stand-in for the sandbox runner)."""

    def __init__(self) -> None:
        self.calls: list[CommandRequest] = []

    async def run(self, req: CommandRequest, workspace_dir: Path) -> CommandResult:
        self.calls.append(req)
        started = time.monotonic()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **req.env}
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", req.command, cwd=workspace_dir, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=req.timeout_seconds)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            out, err = await proc.communicate()
        return CommandResult(
            request_id=req.request_id,
            exit_code=None if timed_out else proc.returncode,
            timed_out=timed_out,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            duration_ms=int((time.monotonic() - started) * 1000),
            sandbox="local",
        )


class FailingRunner:
    async def run(self, req: CommandRequest, workspace_dir: Path) -> CommandResult:
        raise RuntimeError("engine exploded")


# ------------------------------------------------------------------------------------------- tar helpers
def _fallback_extract(data: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        tf.extractall(dest, filter="data")


def _fallback_build(src: Path, paths: list[str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        if paths:
            for p in paths:
                full = src / p
                if not full.exists():
                    raise FileNotFoundError(p)
                tf.add(full, arcname=p)
        else:
            for child in sorted(src.iterdir()):
                tf.add(child, arcname=child.name)
    return buf.getvalue()


def _fallback_manifest(src: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(src.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[p.relative_to(src).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def workspace_ops() -> WorkspaceOps:
    try:
        from worker.execution.app import default_workspace_ops

        return default_workspace_ops()
    except ImportError:
        return WorkspaceOps(extract=_fallback_extract, build_tar=_fallback_build, manifest=_fallback_manifest)


def make_tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def read_tar(data: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for m in tf.getmembers():
            if m.isfile():
                fh = tf.extractfile(m)
                assert fh is not None
                out[m.name.lstrip("./")] = fh.read()
    return out


# ------------------------------------------------------------------------------------------- fake Ollama
def _norm(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


@dataclass
class FakeOllama:
    """Real HTTP server (aiohttp) speaking the subset of the Ollama API used by the model worker."""

    installed: set[str] = field(default_factory=lambda: {"gemma4:26b", "qwen3:8b", "embeddinggemma:latest"})
    unload_polls: int = 2  # /api/ps calls until an unloaded model disappears (Ollama unloads asynchronously)
    stuck_unload: bool = False
    fail_load: str | None = None
    version: str = "0.32.12"
    resident: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_unload: dict[str, int] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    url: str = ""
    _runner: web.AppRunner | None = None

    def _ps_entry(self, name: str) -> dict[str, Any]:
        r = self.resident[name]
        return {
            "name": name,
            "model": name,
            "size": 5_000_000_000,
            "size_vram": 4_000_000_000,
            "digest": "sha256:abc",
            "details": {"family": "x"},
            "expires_at": "2026-10-08T12:00:00Z",
            "context_length": r["num_ctx"],
        }

    async def _version(self, _req: web.Request) -> web.Response:
        return web.json_response({"version": self.version})

    async def _tags(self, _req: web.Request) -> web.Response:
        return web.json_response({"models": [{"name": n, "model": n, "size": 1} for n in sorted(self.installed)]})

    async def _ps(self, _req: web.Request) -> web.Response:
        for name in list(self.pending_unload):
            if self.stuck_unload:
                continue
            self.pending_unload[name] -= 1
            if self.pending_unload[name] <= 0:
                self.pending_unload.pop(name)
                self.resident.pop(name, None)
        return web.json_response({"models": [self._ps_entry(n) for n in sorted(self.resident)]})

    async def _generate(self, req: web.Request) -> web.Response:
        body = await req.json()
        self.requests.append(("generate", body))
        name = _norm(str(body.get("model", "")))
        if name not in self.installed:
            return web.json_response({"error": f"model '{body.get('model')}' not found"}, status=404)
        if body.get("keep_alive") == 0 and "prompt" not in body:
            if name in self.resident:
                self.pending_unload[name] = self.unload_polls
            return web.json_response({"model": name, "done": True, "done_reason": "unload", "response": ""})
        if self.fail_load == name:
            return web.json_response({"error": "model requires more system memory (34.0 GiB) than is available"}, status=500)
        num_ctx = int(body.get("options", {}).get("num_ctx", 4096))
        self.resident[name] = {"num_ctx": num_ctx, "keep_alive": body.get("keep_alive")}
        self.pending_unload.pop(name, None)
        if body.get("prompt"):
            return web.json_response(
                {
                    "model": name,
                    "done": True,
                    "response": "OK",
                    "eval_count": 2,
                    "prompt_eval_count": 5,
                    "total_duration": 250_000_000,
                    "load_duration": 50_000_000,
                }
            )
        return web.json_response({"model": name, "done": True, "done_reason": "load", "response": ""})

    async def start(self) -> FakeOllama:
        app = web.Application()
        app.router.add_get("/api/version", self._version)
        app.router.add_get("/api/tags", self._tags)
        app.router.add_get("/api/ps", self._ps)
        app.router.add_post("/api/generate", self._generate)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None
        port = server.sockets[0].getsockname()[1]  # type: ignore[union-attr,attr-defined]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
