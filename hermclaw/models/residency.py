"""Model residency – load/unload adapter for the model host (P08 8.10, Bauplan §4).

LiteLLM has no residency control, so loading/unloading talks to the model host directly through an injected
``ModelHostClient``:

* ``OllamaHostClient`` – Ollama HTTP API (``/api/ps``, ``/api/generate`` with ``keep_alive``; ``/api/embed`` for
  embedding models). Used until/unless the model worker daemon on ``.224`` is the access path.
* ``WorkerModelHostClient`` – adapter over ``hermclaw.workers.client.ModelWorkerClient`` (model worker daemon).

``ModelResidency.ensure_loaded(alias)`` implements steps 4–7 of the model switch protocol (Bauplan §4): unload the
other members of the alias' exclusive resource group, load with the profile's ``num_ctx``, verify via ``ps`` (model
resident, context length as requested) and emit ``model.load.started/finished`` / ``model.unloaded`` events.
*Whether* a switch may happen (leases, priorities, preemption) is decided by the resource manager – this module only
accepts an optional ``lease_id`` for traceability and never preempts on its own.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.contracts.worker import LoadedModel, ModelLoadRequest
from hermclaw.core.config import ModelProfileConfig, ModelsConfig
from hermclaw.core.errors import HermclawError, ModelError, ModelTimeout
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.events.store import append_event
from hermclaw.models.profiles import ProfileRegistry, normalize_model_tag, same_model, validate_http_url

log = get_logger(__name__)
SessionFactory = Callable[[], AsyncSession]
SOURCE_TYPE = "model_residency"

MODEL_CONTEXT_MISMATCH = "MODEL_CONTEXT_MISMATCH"
MODEL_LOAD_FAILED = "MODEL_LOAD_FAILED"
MODEL_UNLOAD_FAILED = "MODEL_UNLOAD_FAILED"
MODEL_HOST_UNAVAILABLE = "MODEL_HOST_UNAVAILABLE"


# ----------------------------------------------------------------------------------------------- host clients
@runtime_checkable
class ModelHostClient(Protocol):
    """Residency operations on one model host."""

    async def loaded_models(self) -> list[LoadedModel]: ...

    async def load(self, model: str, context_tokens: int, keep_alive: str) -> None: ...

    async def unload(self, model: str) -> None: ...


def parse_ps(payload: Any) -> list[LoadedModel]:
    """Parse Ollama ``/api/ps``."""
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


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class OllamaHostClient:
    """Direct Ollama access for residency control (no inference traffic – that goes through LiteLLM)."""

    def __init__(
        self,
        base_url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        load_timeout_seconds: float = 900.0,
        unload_wait_seconds: float = 60.0,
        poll_seconds: float = 0.5,
    ) -> None:
        self.base_url = validate_http_url(base_url, what="ollama base_url")
        self._own = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
        self.load_timeout_seconds = load_timeout_seconds
        self.unload_wait_seconds = unload_wait_seconds
        self.poll_seconds = poll_seconds

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()

    async def _call(self, method: str, path: str, *, body: dict[str, Any] | None = None, wait_seconds: float = 30.0) -> Any:
        try:
            resp = await self._client.request(method, f"{self.base_url}{path}", json=body, timeout=httpx.Timeout(wait_seconds, connect=5.0))
        except httpx.TimeoutException as exc:
            raise ModelTimeout(f"ollama {path} timed out after {wait_seconds:.0f}s", details={"base_url": self.base_url}) from exc
        except httpx.TransportError as exc:
            raise ModelError(
                f"ollama unreachable: {type(exc).__name__}", code=MODEL_HOST_UNAVAILABLE, details={"base_url": self.base_url}
            ) from exc
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", resp.text)
            except (ValueError, AttributeError):
                err = resp.text
            raise ModelError(
                f"ollama {path} failed (HTTP {resp.status_code}): {DEFAULT_REDACTOR.text(str(err))[:1000]}",
                code=MODEL_LOAD_FAILED if path != "/api/ps" else MODEL_HOST_UNAVAILABLE,
                details={"http_status": resp.status_code, "path": path},
            )
        try:
            return resp.json()
        except ValueError:
            return {}

    async def version(self) -> str:
        data = await self._call("GET", "/api/version", wait_seconds=10.0)
        return str(data.get("version", "")) if isinstance(data, dict) else ""

    async def installed_models(self) -> list[str]:
        data = await self._call("GET", "/api/tags", wait_seconds=15.0)
        models = data.get("models") if isinstance(data, dict) else None
        return [str(m.get("name") or m.get("model")) for m in models or [] if isinstance(m, dict) and (m.get("name") or m.get("model"))]

    async def loaded_models(self) -> list[LoadedModel]:
        return parse_ps(await self._call("GET", "/api/ps", wait_seconds=15.0))

    async def load(self, model: str, context_tokens: int, keep_alive: str) -> None:
        body = {"model": model, "prompt": "", "stream": False, "keep_alive": keep_alive, "options": {"num_ctx": context_tokens}}
        try:
            await self._call("POST", "/api/generate", body=body, wait_seconds=self.load_timeout_seconds)
        except ModelError as exc:
            # Embedding-only models reject /api/generate ("does not support generate") – load via /api/embed instead.
            if exc.details.get("http_status") == 400 and "generate" in exc.message.lower():
                embed = {"model": model, "input": [], "keep_alive": keep_alive, "options": {"num_ctx": context_tokens}}
                await self._call("POST", "/api/embed", body=embed, wait_seconds=self.load_timeout_seconds)
                return
            raise

    async def unload(self, model: str) -> None:
        await self._call("POST", "/api/generate", body={"model": model, "keep_alive": 0, "stream": False}, wait_seconds=60.0)
        deadline = time.monotonic() + self.unload_wait_seconds
        while time.monotonic() < deadline:
            if not any(same_model(m.name, model) for m in await self.loaded_models()):
                return
            await asyncio.sleep(self.poll_seconds)
        raise ModelError(f"model '{model}' still resident {self.unload_wait_seconds:.0f}s after unload", code=MODEL_UNLOAD_FAILED)


class _WorkerModelsApi(Protocol):
    async def loaded_models(self) -> list[LoadedModel]: ...

    async def load_model(
        self, request: ModelLoadRequest, *, exclusive: bool = ..., keep: Sequence[str] = ..., timeout_seconds: float | None = ...
    ) -> Any: ...

    async def unload_model(self, model: str, *, timeout_seconds: float | None = ...) -> Any: ...


class WorkerModelHostClient:
    """``ModelHostClient`` over the model worker daemon client (``ModelWorkerClient``)."""

    def __init__(self, worker: _WorkerModelsApi, *, load_timeout_seconds: float = 900.0) -> None:
        self.worker = worker
        self.load_timeout_seconds = load_timeout_seconds

    async def loaded_models(self) -> list[LoadedModel]:
        return list(await self.worker.loaded_models())

    async def load(self, model: str, context_tokens: int, keep_alive: str) -> None:
        # exclusive=False: group exclusivity is enforced by ModelResidency (it knows the resource groups).
        await self.worker.load_model(
            ModelLoadRequest(model=model, context_tokens=context_tokens, keep_alive=keep_alive),
            exclusive=False,
            timeout_seconds=self.load_timeout_seconds,
        )

    async def unload(self, model: str) -> None:
        await self.worker.unload_model(model)


# ----------------------------------------------------------------------------------------------- residency
@dataclass
class ResidencyResult:
    alias: str
    model: str
    host: str
    already_loaded: bool
    unloaded: list[str] = field(default_factory=list)
    context_length: int | None = None
    context_verified: bool = False
    duration_ms: int = 0
    resident_memory_gb: float = 0.0
    capacity_warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "model": self.model,
            "host": self.host,
            "already_loaded": self.already_loaded,
            "unloaded": self.unloaded,
            "context_length": self.context_length,
            "context_verified": self.context_verified,
            "duration_ms": self.duration_ms,
            "resident_memory_gb": self.resident_memory_gb,
            "capacity_warning": self.capacity_warning,
        }


@dataclass
class ResidentModel:
    host: str
    name: str
    aliases: list[str]
    context_length: int | None
    size_bytes: int
    size_vram_bytes: int
    expires_at: str | None


class ModelResidency:
    """Keeps the right model resident on its host according to the profile's resource group."""

    def __init__(
        self,
        models: ModelsConfig,
        host_client: ModelHostClient | Mapping[str, ModelHostClient],
        *,
        session_factory: SessionFactory | None = None,
        keep_alive: str = "10m",
        require_context_match: bool = True,
    ) -> None:
        self.models = models
        self.registry = ProfileRegistry(models)
        self._clients: Mapping[str, ModelHostClient] | None = host_client if isinstance(host_client, Mapping) else None
        self._default_client: ModelHostClient | None = None if isinstance(host_client, Mapping) else host_client
        self._session_factory = session_factory
        self.keep_alive = keep_alive
        self.require_context_match = require_context_match
        self._host_locks: dict[str, asyncio.Lock] = {}

    def client_for(self, host: str) -> ModelHostClient:
        if self._clients is not None:
            client = self._clients.get(host)
            if client is None:
                raise ModelError(f"no model host client configured for host '{host}'", code=MODEL_HOST_UNAVAILABLE)
            return client
        assert self._default_client is not None
        return self._default_client

    def _lock(self, host: str) -> asyncio.Lock:
        lock = self._host_locks.get(host)
        if lock is None:
            lock = self._host_locks[host] = asyncio.Lock()
        return lock

    def _aliases_for(self, model: str, host: str) -> list[str]:
        return [p.alias for p in self.registry.by_model(model, host=host)]

    async def status(self, host: str | None = None) -> list[ResidentModel]:
        hosts = [host] if host else self.registry.hosts()
        out: list[ResidentModel] = []
        for h in hosts:
            for m in await self.client_for(h).loaded_models():
                out.append(
                    ResidentModel(
                        host=h,
                        name=m.name,
                        aliases=self._aliases_for(m.name, h),
                        context_length=m.context_length,
                        size_bytes=m.size_bytes,
                        size_vram_bytes=m.size_vram_bytes,
                        expires_at=m.expires_at,
                    )
                )
        return out

    async def ensure_loaded(
        self,
        alias: str,
        *,
        lease_id: uuid.UUID | str | None = None,
        job_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
    ) -> ResidencyResult:
        """Make ``alias`` resident with its configured context. Idempotent; serialised per host in this process."""
        profile = self.registry.get(alias)
        client = self.client_for(profile.host)
        async with self._lock(profile.host):
            t0 = time.monotonic()
            loaded = await client.loaded_models()
            target = next((m for m in loaded if same_model(m.name, profile.model)), None)
            conflicts = self._conflicts(profile, loaded)
            if target is not None and not conflicts and self._context_ok(profile, target):
                return ResidencyResult(
                    alias=alias,
                    model=profile.model,
                    host=profile.host,
                    already_loaded=True,
                    context_length=target.context_length,
                    context_verified=target.context_length == profile.context_tokens,
                    resident_memory_gb=self._resident_gb([m.name for m in loaded], profile.host),
                )
            lease = str(lease_id) if lease_id else None
            unloaded: list[str] = []
            for other in conflicts:
                await self._unload(
                    client,
                    profile.host,
                    other,
                    reason=f"exclusive resource group '{profile.resource_group}' for {alias}",
                    lease_id=lease,
                    job_id=job_id,
                    step_id=step_id,
                )
                unloaded.append(other)
            if target is not None and not self._context_ok(profile, target):
                # resident with a different num_ctx – reload explicitly instead of relying on Ollama's implicit reload
                await self._unload(
                    client,
                    profile.host,
                    target.name,
                    reason=f"context {target.context_length} != {profile.context_tokens}",
                    lease_id=lease,
                    job_id=job_id,
                    step_id=step_id,
                )
                unloaded.append(target.name)
            remaining = [m.name for m in loaded if m.name not in unloaded]
            resident_gb = self._resident_gb(remaining, profile.host) + profile.memory_gb
            capacity_warning = None
            if resident_gb > self.models.model_host_capacity_gb:
                capacity_warning = f"resident models would use ~{resident_gb:.1f} GB > capacity {self.models.model_host_capacity_gb:.1f} GB"
                log.warning("model host capacity exceeded", extra={"alias": alias, "resident_gb": resident_gb})
            await self._event(
                EventType.MODEL_LOAD_STARTED,
                profile,
                {
                    "alias": alias,
                    "model": profile.model,
                    "host": profile.host,
                    "num_ctx": profile.context_tokens,
                    "keep_alive": self.keep_alive,
                    "resource_group": profile.resource_group,
                    "unloaded": unloaded,
                    "lease_id": lease,
                    "capacity_warning": capacity_warning,
                },
                job_id=job_id,
                step_id=step_id,
            )
            try:
                await client.load(profile.model, profile.context_tokens, self.keep_alive)
                after = await client.loaded_models()
                resident = next((m for m in after if same_model(m.name, profile.model)), None)
                if resident is None:
                    raise ModelError(f"'{profile.model}' is not resident after load", code=MODEL_LOAD_FAILED, details={"alias": alias})
                verified = resident.context_length == profile.context_tokens
                if resident.context_length is not None and not verified and self.require_context_match:
                    raise ModelError(
                        f"'{profile.model}' loaded with context {resident.context_length}, expected {profile.context_tokens}",
                        code=MODEL_CONTEXT_MISMATCH,
                        details={"alias": alias, "context_length": resident.context_length, "expected": profile.context_tokens},
                    )
            except HermclawError as exc:
                await self._event(
                    EventType.MODEL_LOAD_FINISHED,
                    profile,
                    {
                        "alias": alias,
                        "model": profile.model,
                        "host": profile.host,
                        "ok": False,
                        "error_code": exc.code,
                        "error": DEFAULT_REDACTOR.text(exc.message)[:1000],
                        "lease_id": lease,
                        "duration_ms": int((time.monotonic() - t0) * 1000),
                    },
                    severity=Severity.error,
                    job_id=job_id,
                    step_id=step_id,
                )
                raise
            if resident.context_length is None:
                log.warning("ollama did not report context_length; context not verified", extra={"alias": alias})
            result = ResidencyResult(
                alias=alias,
                model=profile.model,
                host=profile.host,
                already_loaded=False,
                unloaded=unloaded,
                context_length=resident.context_length,
                context_verified=verified,
                duration_ms=int((time.monotonic() - t0) * 1000),
                resident_memory_gb=self._resident_gb([m.name for m in after], profile.host),
                capacity_warning=capacity_warning,
            )
            await self._event(
                EventType.MODEL_LOAD_FINISHED,
                profile,
                {**result.as_dict(), "ok": True, "lease_id": lease},
                job_id=job_id,
                step_id=step_id,
                duration_ms=result.duration_ms,
            )
            return result

    async def unload(
        self,
        alias: str,
        *,
        lease_id: uuid.UUID | str | None = None,
        job_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        reason: str = "requested",
    ) -> bool:
        """Unload ``alias`` if resident. Returns whether something was unloaded."""
        profile = self.registry.get(alias)
        client = self.client_for(profile.host)
        async with self._lock(profile.host):
            loaded = await client.loaded_models()
            target = next((m for m in loaded if same_model(m.name, profile.model)), None)
            if target is None:
                return False
            await self._unload(
                client,
                profile.host,
                target.name,
                reason=reason,
                lease_id=str(lease_id) if lease_id else None,
                job_id=job_id,
                step_id=step_id,
            )
            return True

    async def unload_group(self, resource_group: str, *, host: str | None = None, reason: str = "requested") -> list[str]:
        """Unload all resident members of a resource group (e.g. before a video job takes the GPU)."""
        hosts = [host] if host else sorted({p.host for p in self.registry.group_members(resource_group)})
        out: list[str] = []
        for h in hosts:
            client = self.client_for(h)
            members = self.registry.group_members(resource_group, host=h)
            async with self._lock(h):
                for m in await client.loaded_models():
                    if any(same_model(m.name, p.model) for p in members):
                        await self._unload(client, h, m.name, reason=reason, lease_id=None, job_id=None, step_id=None)
                        out.append(m.name)
        return out

    # ------------------------------------------------------------------------------------------ internals
    def _context_ok(self, profile: ModelProfileConfig, model: LoadedModel) -> bool:
        return model.context_length is None or model.context_length == profile.context_tokens or not self.require_context_match

    def _conflicts(self, profile: ModelProfileConfig, loaded: list[LoadedModel]) -> list[str]:
        """Resident models of the same host that share the alias' resource group, if the alias is exclusive."""
        if not profile.exclusive:
            return []
        members = [
            p for p in self.registry.group_members(profile.resource_group, host=profile.host) if not same_model(p.model, profile.model)
        ]
        return [m.name for m in loaded if any(same_model(m.name, p.model) for p in members)]

    def _resident_gb(self, names: list[str], host: str) -> float:
        total = 0.0
        seen: set[str] = set()
        for name in names:
            norm = normalize_model_tag(name)
            if norm in seen:
                continue
            seen.add(norm)
            profiles = self.registry.by_model(name, host=host)
            total += max((p.memory_gb for p in profiles), default=0.0)
        return round(total, 3)

    async def _unload(
        self,
        client: ModelHostClient,
        host: str,
        model: str,
        *,
        reason: str,
        lease_id: str | None,
        job_id: uuid.UUID | None,
        step_id: uuid.UUID | None,
    ) -> None:
        t0 = time.monotonic()
        await client.unload(model)
        aliases = self._aliases_for(model, host)
        profile = self.registry.by_model(model, host=host)
        await self._event(
            EventType.MODEL_UNLOADED,
            profile[0] if profile else None,
            {
                "model": model,
                "aliases": aliases,
                "host": host,
                "reason": reason,
                "lease_id": lease_id,
                "duration_ms": int((time.monotonic() - t0) * 1000),
            },
            job_id=job_id,
            step_id=step_id,
        )

    async def _event(
        self,
        event_type: str,
        profile: ModelProfileConfig | None,
        payload: dict[str, Any],
        *,
        severity: Severity = Severity.info,
        job_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        duration_ms: int | None = None,
    ) -> None:
        log.info(event_type, extra={k: v for k, v in payload.items() if isinstance(v, str | int | float | bool)})
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            await append_event(
                session,
                event_type,
                source_type=SOURCE_TYPE,
                source_id=profile.alias if profile else str(payload.get("model", "")),
                job_id=job_id,
                step_id=step_id,
                severity=severity,
                payload={k: v for k, v in payload.items() if v is not None},
                duration_ms=duration_ms,
            )
            await session.commit()
