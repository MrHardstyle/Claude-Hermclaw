"""Minimal async Ollama client for the model worker (research 20261008-007).

Used endpoints:

- ``GET  /api/version``  – server version
- ``GET  /api/ps``       – resident models (``name``, ``size``, ``size_vram``, ``expires_at``, ``context_length``)
- ``GET  /api/tags``     – installed models
- ``POST /api/generate`` – load (empty prompt + ``options.num_ctx`` + ``keep_alive``), unload
  (``keep_alive: 0``) and the selftest probe (tiny prompt, ``num_predict``)

Responses are parsed leniently (unknown fields ignored) because Ollama adds fields between releases.
Generated text is never logged or returned - only counts/durations (no model output, no reasoning).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from hermclaw.contracts.worker import LoadedModel
from worker.common.errors import DaemonError

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class OllamaError(DaemonError):
    pass


def normalize_model(name: str) -> str:
    """``qwen3`` and ``qwen3:latest`` name the same model."""
    n = name.strip()
    return n if ":" in n.rsplit("/", 1)[-1] else f"{n}:latest"


def same_model(a: str, b: str) -> bool:
    return normalize_model(a) == normalize_model(b)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_ps(payload: Any) -> list[LoadedModel]:
    models = payload.get("models") if isinstance(payload, dict) else None
    out: list[LoadedModel] = []
    for m in models or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("model") or "").strip()
        if not name:
            continue
        ctx = m.get("context_length")
        out.append(
            LoadedModel(
                name=name,
                size_bytes=_int(m.get("size")),
                size_vram_bytes=_int(m.get("size_vram")),
                context_length=_int(ctx) if ctx is not None else None,
                expires_at=str(m["expires_at"]) if m.get("expires_at") else None,
            )
        )
    return out


class OllamaClient:
    def __init__(
        self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: httpx.Timeout = DEFAULT_TIMEOUT
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, *, json_body: dict[str, Any] | None = None, timeout_seconds: float | None = None) -> Any:
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if timeout_seconds is not None:
            kwargs["timeout"] = httpx.Timeout(timeout_seconds, connect=5.0)
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise OllamaError(f"Ollama {path} timed out", code="OLLAMA_TIMEOUT", status=504) from exc
        except httpx.TransportError as exc:
            raise OllamaError(
                f"Ollama unreachable at {self.base_url}: {type(exc).__name__}", code="OLLAMA_UNREACHABLE", status=503
            ) from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            message = str(data.get("error")) if isinstance(data, dict) and data.get("error") else resp.text[:300]
            if resp.status_code == 404:
                raise OllamaError(f"Ollama: {message}", code="MODEL_NOT_INSTALLED", status=404, details={"ollama_status": 404})
            raise OllamaError(f"Ollama {path} -> HTTP {resp.status_code}: {message}"[:500], code="OLLAMA_ERROR", status=502)
        return data

    async def version(self) -> str:
        data = await self._call("GET", "/api/version", timeout_seconds=5.0)
        return str(data.get("version", "unknown")) if isinstance(data, dict) else "unknown"

    async def ps(self) -> list[LoadedModel]:
        return parse_ps(await self._call("GET", "/api/ps", timeout_seconds=10.0))

    async def tags(self) -> list[str]:
        data = await self._call("GET", "/api/tags", timeout_seconds=15.0)
        models = data.get("models") if isinstance(data, dict) else None
        return sorted(
            {str(m.get("name") or m.get("model")) for m in models or [] if isinstance(m, dict) and (m.get("name") or m.get("model"))}
        )

    async def loaded(self, model: str) -> LoadedModel | None:
        for m in await self.ps():
            if same_model(m.name, model):
                return m
        return None

    async def load(self, model: str, *, num_ctx: int, keep_alive: str | int, timeout_seconds: float) -> None:
        body = {"model": model, "prompt": "", "stream": False, "keep_alive": keep_alive, "options": {"num_ctx": num_ctx}}
        await self._call("POST", "/api/generate", json_body=body, timeout_seconds=timeout_seconds)

    async def request_unload(self, model: str) -> None:
        await self._call("POST", "/api/generate", json_body={"model": model, "keep_alive": 0, "stream": False}, timeout_seconds=60.0)

    async def wait_unloaded(self, model: str, *, timeout_seconds: float, poll_seconds: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if await self.loaded(model) is None:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(poll_seconds)

    async def probe(self, model: str, *, num_ctx: int, timeout_seconds: float) -> dict[str, int]:
        """Tiny inference; returns only counters (never the generated text)."""
        body = {
            "model": model,
            "prompt": "Reply with OK.",
            "stream": False,
            "options": {"num_ctx": num_ctx, "num_predict": 8, "temperature": 0},
        }
        data = await self._call("POST", "/api/generate", json_body=body, timeout_seconds=timeout_seconds)
        if not isinstance(data, dict) or not data.get("done", False):
            raise OllamaError("Ollama probe did not finish", code="OLLAMA_PROBE_FAILED", status=502)
        return {
            "eval_count": _int(data.get("eval_count")),
            "prompt_eval_count": _int(data.get("prompt_eval_count")),
            "total_duration_ms": _int(data.get("total_duration")) // 1_000_000,
            "load_duration_ms": _int(data.get("load_duration")) // 1_000_000,
        }
