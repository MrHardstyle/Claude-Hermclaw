"""Test-only support for the P08 model gateway tests (no tests in here).

- :class:`FakeOllama` – a real aiohttp HTTP server emulating the Ollama endpoints used by LiteLLM, the residency
  adapter and the health checker (``/api/chat``, ``/api/embed``, ``/api/generate``, ``/api/ps``, ``/api/tags``,
  ``/api/version``). It records every request body and keeps a residency state (loaded models + ``num_ctx``).
- :class:`FakeLiteLLM` – a real aiohttp server speaking the LiteLLM/OpenAI-compatible API with scripted answers,
  for gateway tests that do not need the real proxy.
- :func:`models_config` – a valid ``ModelsConfig`` following the fixed architecture.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

from hermclaw.core.config import LiteLLMConfig, ModelProfileConfig, ModelsConfig

ROOT = Path(__file__).resolve().parents[2]
MASTER_KEY = "sk-hermclaw-test-master-key-0123456789abcdef"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def profile(**kw: Any) -> ModelProfileConfig:
    return ModelProfileConfig.model_validate(kw)


def models_config(base_url: str = "http://127.0.0.1:4000", *, timeout: int = 60, **overrides: Any) -> ModelsConfig:
    profiles = [
        profile(alias="fast-router", role="fast", model="qwen3:8b", context_tokens=16384, max_output_tokens=2048, temperature=0.1,
                resource_group="small-model-224", exclusive=False, memory_gb=7, priority=30, timeout_seconds=timeout),
        profile(alias="planner-gemma", role="planner", model="gemma4:26b", context_tokens=32768, max_output_tokens=8192,
                memory_gb=19, priority=70, timeout_seconds=timeout),
        profile(alias="planner-gemma-fallback", role="planner_fallback", model="gemma4:12b", fallback_for="planner-gemma",
                context_tokens=32768, max_output_tokens=8192, memory_gb=10, priority=70, timeout_seconds=timeout),
        profile(alias="coder-main", role="coder", model="qwen3-coder:30b", context_tokens=32768, max_output_tokens=6144,
                memory_gb=23, priority=50, timeout_seconds=timeout),
        profile(alias="heavy-review", role="heavy", model="qwen3.8:27b", context_tokens=24576, max_output_tokens=4096,
                memory_gb=25, priority=60, timeout_seconds=timeout),
        profile(alias="embedding", role="embedding", kind="embedding", model="embeddinggemma-2:740m", context_tokens=2048,
                max_output_tokens=0, resource_group="small-model-224", exclusive=False, memory_gb=2, priority=20,
                embedding_dimensions=8, timeout_seconds=timeout),
    ]
    data: dict[str, Any] = {"litellm": LiteLLMConfig(base_url=base_url, api_key_ref="literal:" + MASTER_KEY,
                                                    request_timeout_seconds=900), "profiles": profiles}
    data.update(overrides)
    return ModelsConfig.model_validate(data)


# ----------------------------------------------------------------------------------------------- aiohttp runner
@asynccontextmanager
async def serve(app: web.Application, port: int | None = None) -> AsyncIterator[str]:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = port or free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


# ----------------------------------------------------------------------------------------------- fake Ollama
@dataclass
class ModelBehaviour:
    status: int = 200
    error: str = ""
    delay: float = 0.0
    contents: deque[str] = field(default_factory=deque)  # scripted answers; default answer when empty
    thinking: str = "PRIVATE-CHAIN-OF-THOUGHT do not store"


class FakeOllama:
    def __init__(self, installed: list[str] | None = None, *, report_context: bool = True, embed_dims: int = 8) -> None:
        self.installed = list(installed or [])
        self.loaded: dict[str, int | None] = {}
        self.requests: list[dict[str, Any]] = []
        self.behaviour: dict[str, ModelBehaviour] = {}
        self.report_context = report_context
        self.embed_dims = embed_dims
        self.default_content = '{"ok": true, "answer": "hello"}'
        self.context_override: dict[str, int] = {}  # simulate Ollama clamping num_ctx
        self.app = web.Application()
        self.app.add_routes([
            web.get("/api/version", self._version),
            web.get("/api/tags", self._tags),
            web.get("/api/ps", self._ps),
            web.post("/api/generate", self._generate),
            web.post("/api/chat", self._chat),
            web.post("/api/embed", self._embed),
            web.post("/api/show", self._show),
        ])

    def b(self, model: str) -> ModelBehaviour:
        return self.behaviour.setdefault(model, ModelBehaviour())

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if r["path"] == path]

    def _load(self, model: str, num_ctx: int | None) -> None:
        self.loaded[model] = self.context_override.get(model, num_ctx or 4096)

    async def _record(self, request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        self.requests.append({"path": request.path, "body": body})
        return body if isinstance(body, dict) else {}

    async def _version(self, request: web.Request) -> web.Response:
        return web.json_response({"version": "0.32.12"})

    async def _tags(self, request: web.Request) -> web.Response:
        return web.json_response({"models": [{"name": m, "model": m} for m in self.installed]})

    async def _ps(self, request: web.Request) -> web.Response:
        models = []
        for name, ctx in self.loaded.items():
            entry: dict[str, Any] = {"name": name, "model": name, "size": 1000, "size_vram": 500, "expires_at": "2026-10-08T12:00:00Z"}
            if self.report_context:
                entry["context_length"] = ctx
            models.append(entry)
        return web.json_response({"models": models})

    async def _show(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response({"capabilities": ["completion"], "model_info": {}, "details": {}})

    def _check(self, model: str) -> web.Response | None:
        if model not in self.installed:
            return web.json_response({"error": f"model '{model}' not found"}, status=404)
        beh = self.behaviour.get(model)
        if beh and beh.status != 200:
            return web.json_response({"error": beh.error or "boom"}, status=beh.status)
        return None

    async def _generate(self, request: web.Request) -> web.Response:
        body = await self._record(request)
        model = str(body.get("model", ""))
        if body.get("keep_alive") in (0, "0", "0s"):
            self.loaded.pop(model, None)
            return web.json_response({"model": model, "done": True, "done_reason": "unload", "response": ""})
        err = self._check(model)
        if err is not None:
            return err
        if "embed" in model:
            return web.json_response({"error": f'"{model}" does not support generate'}, status=400)
        self._load(model, (body.get("options") or {}).get("num_ctx"))
        return web.json_response({"model": model, "done": True, "done_reason": "load", "response": ""})

    async def _chat(self, request: web.Request) -> web.Response:
        body = await self._record(request)
        model = str(body.get("model", ""))
        err = self._check(model)
        if err is not None:
            return err
        beh = self.b(model)
        if beh.delay:
            await asyncio.sleep(beh.delay)
        self._load(model, (body.get("options") or {}).get("num_ctx"))
        content = beh.contents.popleft() if beh.contents else self.default_content
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if beh.thinking:
            message["thinking"] = beh.thinking
        return web.json_response({
            "model": model, "created_at": "2026-10-08T00:00:00Z", "message": message, "done": True, "done_reason": "stop",
            "prompt_eval_count": 42, "eval_count": 7, "total_duration": 1_000_000, "eval_duration": 500_000,
        })

    async def _embed(self, request: web.Request) -> web.Response:
        body = await self._record(request)
        model = str(body.get("model", ""))
        err = self._check(model)
        if err is not None:
            return err
        self._load(model, (body.get("options") or {}).get("num_ctx"))
        inputs = body.get("input")
        items = inputs if isinstance(inputs, list) else [inputs]
        vectors = [[round(0.01 * (i + 1) + 0.001 * d, 6) for d in range(self.embed_dims)] for i in range(len(items))]
        return web.json_response({"model": model, "embeddings": vectors, "prompt_eval_count": 3 * len(items)})


# ----------------------------------------------------------------------------------------------- fake LiteLLM
@dataclass
class Scripted:
    status: int = 200
    body: Any = None
    delay: float = 0.0


class FakeLiteLLM:
    """OpenAI-compatible fake: per-alias queue of scripted responses; records requests incl. headers."""

    def __init__(self, *, ready: bool = True, aliases: list[str] | None = None) -> None:
        self.queues: dict[str, deque[Scripted]] = {}
        self.requests: list[dict[str, Any]] = []
        self.ready = ready
        self.aliases = aliases
        self.app = web.Application()
        self.app.add_routes([
            web.post("/v1/chat/completions", self._chat),
            web.post("/v1/embeddings", self._embeddings),
            web.get("/health/liveliness", self._live),
            web.get("/health/readiness", self._ready),
            web.get("/v1/models", self._models),
        ])

    def script(self, alias: str, *items: Scripted) -> None:
        self.queues.setdefault(alias, deque()).extend(items)

    @staticmethod
    def completion(content: str | None, *, reasoning: str | None = None, finish: str = "stop", prompt: int = 10, completion: int = 5) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning is not None:
            msg["reasoning_content"] = reasoning
        return {"id": "x", "object": "chat.completion", "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
                "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}

    async def _respond(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append({"path": request.path, "body": body, "headers": dict(request.headers)})
        queue = self.queues.get(str(body.get("model")))
        item = queue.popleft() if queue else Scripted(body=self.completion('{"ok": true}'))
        if item.delay:
            await asyncio.sleep(item.delay)
        return web.json_response(item.body, status=item.status) if not isinstance(item.body, str) else web.Response(text=item.body, status=item.status)

    async def _chat(self, request: web.Request) -> web.Response:
        return await self._respond(request)

    async def _embeddings(self, request: web.Request) -> web.Response:
        return await self._respond(request)

    async def _live(self, request: web.Request) -> web.Response:
        return web.json_response("I'm alive!")

    async def _ready(self, request: web.Request) -> web.Response:
        if not self.ready:
            return web.json_response({"error": "not ready"}, status=503)
        return web.json_response({"status": "healthy", "db": "Not connected"})

    async def _models(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {MASTER_KEY}":
            return web.json_response({"error": {"message": "Authentication Error"}}, status=401)
        ids = self.aliases if self.aliases is not None else list(self.queues)
        return web.json_response({"object": "list", "data": [{"id": a, "object": "model"} for a in ids]})


# ----------------------------------------------------------------------------------------------- real LiteLLM proxy
def litellm_proxy_binary() -> str | None:
    """A ``litellm`` CLI whose interpreter has the proxy extras (``litellm[proxy]``) installed.

    ``HERMCLAW_TEST_LITELLM_BIN`` wins; otherwise the repo venv is used if its LiteLLM can import the proxy server."""
    explicit = os.environ.get("HERMCLAW_TEST_LITELLM_BIN")
    if explicit:
        return explicit if Path(explicit).exists() else None
    candidate = ROOT / ".venv" / "bin" / "litellm"
    if not candidate.exists():
        found = shutil.which("litellm")
        if not found:
            return None
        candidate = Path(found)
    python = candidate.parent / "python"
    probe = subprocess.run([str(python if python.exists() else sys.executable), "-c", "import backoff, litellm.proxy.proxy_server"],
                           capture_output=True, timeout=120)
    return str(candidate) if probe.returncode == 0 else None


@asynccontextmanager
async def litellm_proxy(binary: str, config_path: Path, *, startup_timeout: float = 120.0) -> AsyncIterator[str]:
    import httpx

    port = free_port()
    env = {**os.environ, "LITELLM_MASTER_KEY": MASTER_KEY, "LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_LOG": "ERROR",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "DATABASE_URL"):
        env.pop(var, None)
    log_path = config_path.with_suffix(".log")
    log_file = log_path.open("wb")
    proc = await asyncio.create_subprocess_exec(binary, "--config", str(config_path), "--host", "127.0.0.1", "--port", str(port),
                                                stdout=log_file, stderr=subprocess.STDOUT, env=env)
    url = f"http://127.0.0.1:{port}"
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + startup_timeout
        async with httpx.AsyncClient(trust_env=False) as client:
            while True:
                if proc.returncode is not None:
                    raise RuntimeError(f"litellm proxy exited early: {log_path.read_text(errors='replace')[-2000:]}")
                with contextlib.suppress(httpx.HTTPError):
                    if (await client.get(f"{url}/health/liveliness", timeout=2)).status_code == 200:
                        break
                if loop.time() > deadline:
                    raise RuntimeError(f"litellm proxy did not start: {log_path.read_text(errors='replace')[-2000:]}")
                await asyncio.sleep(0.5)
        yield url
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 15)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        log_file.close()


def dump(obj: Any) -> str:
    return json.dumps(obj, default=str, sort_keys=True)
