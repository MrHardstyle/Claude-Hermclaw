"""Model gateway health checks (P08 8.8) and invocation metrics (P08 8.11).

Health never triggers inference: LiteLLM's ``/health`` endpoint would send a real chat request to every model (and
thereby load it on ``.224``), so it is not used. Instead:

* ``GET /health/liveliness`` – proxy process alive (unauthenticated)
* ``GET /health/readiness``  – proxy ready to serve
* ``GET /v1/models``         – aliases registered in the proxy (needs the master key)
* Ollama ``/api/version``, ``/api/tags`` (installed models) and ``/api/ps`` (resident models) per model host

A profile is *available* when the proxy is ready, the alias is registered (if that could be checked) and its model is
installed on its host.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import Float, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.core.config import ModelProfileConfig, ModelsConfig
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.models.profiles import ProfileRegistry, same_model, validate_http_url
from hermclaw.models.residency import parse_ps
from hermclaw.persistence.models import ModelInvocation


# ----------------------------------------------------------------------------------------------- report models
class EndpointStatus(BaseModel):
    name: str
    url: str
    ok: bool
    status_code: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class OllamaHostStatus(BaseModel):
    host: str
    endpoint: EndpointStatus
    version: str | None = None
    installed: list[str] = Field(default_factory=list)
    loaded: list[dict[str, Any]] = Field(default_factory=list)


class ProfileHealth(BaseModel):
    alias: str
    role: str
    kind: str
    model: str
    host: str
    enabled: bool
    registered_in_proxy: bool | None = None
    installed: bool | None = None
    loaded: bool = False
    context_length: int | None = None
    context_matches: bool | None = None
    available: bool = False
    reason: str | None = None


class ModelHealthReport(BaseModel):
    checked_at: datetime
    healthy: bool
    litellm_liveliness: EndpointStatus
    litellm_readiness: EndpointStatus
    litellm_models: EndpointStatus | None = None
    ollama: list[OllamaHostStatus] = Field(default_factory=list)
    profiles: list[ProfileHealth] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)

    def profile(self, alias: str) -> ProfileHealth | None:
        return next((p for p in self.profiles if p.alias == alias), None)


# ----------------------------------------------------------------------------------------------- checker
class ModelHealthChecker:
    def __init__(
        self,
        models: ModelsConfig,
        *,
        ollama_urls: Mapping[str, str] | None = None,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.models = models
        self.registry = ProfileRegistry(models)
        self.base_url = validate_http_url(base_url or models.litellm.base_url, what="litellm base_url")
        self.ollama_urls = {h: validate_http_url(u, what=f"ollama url of {h}") for h, u in (ollama_urls or {}).items()}
        self._api_key = api_key
        if api_key:
            DEFAULT_REDACTOR.add_literal(api_key)
        self._own = http_client is None
        self._client = http_client or httpx.AsyncClient()
        self.timeout_seconds = timeout_seconds

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()

    async def _get(self, name: str, url: str, *, auth: bool = False) -> tuple[EndpointStatus, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"} if auth and self._api_key else {}
        t0 = time.monotonic()
        try:
            resp = await self._client.get(
                url, headers=headers, timeout=httpx.Timeout(self.timeout_seconds, connect=min(5.0, self.timeout_seconds))
            )
        except httpx.HTTPError as exc:
            return EndpointStatus(
                name=name, url=url, ok=False, error=f"{type(exc).__name__}", latency_ms=int((time.monotonic() - t0) * 1000)
            ), None
        latency = int((time.monotonic() - t0) * 1000)
        try:
            body: Any = resp.json()
        except ValueError:
            body = None
        ok = resp.status_code == 200
        error = None if ok else DEFAULT_REDACTOR.text(resp.text[:500])
        return EndpointStatus(name=name, url=url, ok=ok, status_code=resp.status_code, latency_ms=latency, error=error), body

    async def liveliness(self) -> EndpointStatus:
        status, _ = await self._get("litellm.liveliness", f"{self.base_url}/health/liveliness")
        return status

    async def readiness(self) -> EndpointStatus:
        status, body = await self._get("litellm.readiness", f"{self.base_url}/health/readiness")
        if status.ok and isinstance(body, dict):
            state = str(body.get("status", "")).lower()
            status.details = {k: v for k, v in body.items() if isinstance(v, str | int | float | bool)}
            if state and state not in ("healthy", "connected", "ok"):
                status.ok = False
                status.error = f"readiness status '{state}'"
        return status

    async def proxy_models(self) -> tuple[EndpointStatus, set[str] | None]:
        if not self._api_key:
            return EndpointStatus(name="litellm.models", url=f"{self.base_url}/v1/models", ok=False, error="no api key – not checked"), None
        status, body = await self._get("litellm.models", f"{self.base_url}/v1/models", auth=True)
        if not status.ok or not isinstance(body, dict) or not isinstance(body.get("data"), list):
            return status, None
        ids = {str(m.get("id")) for m in body["data"] if isinstance(m, dict) and m.get("id")}
        status.details = {"count": len(ids)}
        return status, ids

    async def ollama_host(self, host: str) -> OllamaHostStatus:
        url = self.ollama_urls[host]
        version_st, version = await self._get(f"ollama.{host}.version", f"{url}/api/version")
        if not version_st.ok:
            return OllamaHostStatus(host=host, endpoint=version_st)
        (tags_st, tags), (ps_st, ps) = await asyncio.gather(
            self._get(f"ollama.{host}.tags", f"{url}/api/tags"), self._get(f"ollama.{host}.ps", f"{url}/api/ps")
        )
        installed = [
            str(m.get("name") or m.get("model"))
            for m in (tags.get("models") or [] if isinstance(tags, dict) else [])
            if isinstance(m, dict) and (m.get("name") or m.get("model"))
        ]
        loaded = [m.model_dump() for m in parse_ps(ps)] if ps_st.ok else []
        endpoint = version_st
        if not tags_st.ok or not ps_st.ok:
            endpoint = endpoint.model_copy(update={"ok": False, "error": tags_st.error or ps_st.error})
        return OllamaHostStatus(
            host=host,
            endpoint=endpoint,
            version=str(version.get("version")) if isinstance(version, dict) else None,
            installed=installed,
            loaded=loaded,
        )

    def _profile_health(
        self,
        p: ModelProfileConfig,
        *,
        ready: bool,
        registered: set[str] | None,
        hosts: Mapping[str, OllamaHostStatus],
    ) -> ProfileHealth:
        ph = ProfileHealth(alias=p.alias, role=p.role, kind=p.kind, model=p.model, host=p.host, enabled=p.enabled)
        reasons: list[str] = []
        if not p.enabled:
            reasons.append("disabled")
        if not ready:
            reasons.append("litellm not ready")
        if registered is not None:
            ph.registered_in_proxy = p.alias in registered
            if not ph.registered_in_proxy:
                reasons.append("alias not registered in litellm")
        host = hosts.get(p.host)
        if host is None:
            reasons.append(f"no ollama url for host '{p.host}'" if p.host not in self.ollama_urls else "host not checked")
        elif not host.endpoint.ok:
            reasons.append(f"ollama on '{p.host}' unreachable")
        else:
            ph.installed = any(same_model(name, p.model) for name in host.installed)
            if not ph.installed:
                reasons.append(f"model '{p.model}' not installed on '{p.host}'")
            resident = next((m for m in host.loaded if same_model(str(m.get("name", "")), p.model)), None)
            if resident is not None:
                ph.loaded = True
                ctx = resident.get("context_length")
                ph.context_length = int(ctx) if isinstance(ctx, int) else None
                ph.context_matches = None if ph.context_length is None else ph.context_length == p.context_tokens
        ph.available = not reasons
        ph.reason = "; ".join(reasons) or None
        return ph

    async def check(self) -> ModelHealthReport:
        hosts = sorted(h for h in self.registry.hosts() if h in self.ollama_urls)
        live, ready, (models_st, registered), host_states = await asyncio.gather(
            self.liveliness(), self.readiness(), self.proxy_models(), asyncio.gather(*(self.ollama_host(h) for h in hosts))
        )
        by_host = {s.host: s for s in host_states}
        proxy_ready = live.ok and ready.ok
        profiles = [self._profile_health(p, ready=proxy_ready, registered=registered, hosts=by_host) for p in self.models.profiles]
        issues: list[str] = []
        if not live.ok:
            issues.append(f"litellm not alive: {live.error or live.status_code}")
        if not ready.ok:
            issues.append(f"litellm not ready: {ready.error or ready.status_code}")
        for h in host_states:
            if not h.endpoint.ok:
                issues.append(f"ollama on {h.host} unhealthy: {h.endpoint.error or h.endpoint.status_code}")
        issues.extend(f"{p.alias}: {p.reason}" for p in profiles if p.enabled and not p.available)
        healthy = proxy_ready and all(p.available for p in profiles if p.enabled)
        return ModelHealthReport(
            checked_at=datetime.now(UTC),
            healthy=healthy,
            litellm_liveliness=live,
            litellm_readiness=ready,
            litellm_models=models_st,
            ollama=list(host_states),
            profiles=profiles,
            issues=issues,
        )


# ----------------------------------------------------------------------------------------------- metrics (8.11)
@dataclass(frozen=True)
class AliasMetrics:
    alias: str
    calls: int
    succeeded: int
    invalid: int
    failed: int
    timeouts: int
    cancelled: int
    in_flight: int
    fallback_calls: int
    repair_calls: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_chars: int
    avg_latency_ms: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    p99_latency_ms: float | None
    max_latency_ms: int | None

    @property
    def finished(self) -> int:
        return self.calls - self.in_flight

    @property
    def error_rate(self) -> float:
        """Technical failures (failed + timeout) per finished call."""
        return (self.failed + self.timeouts) / self.finished if self.finished else 0.0

    @property
    def invalid_rate(self) -> float:
        return self.invalid / self.finished if self.finished else 0.0

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data.update({"finished": self.finished, "error_rate": round(self.error_rate, 4), "invalid_rate": round(self.invalid_rate, 4)})
        return data


def _count(status: str) -> Any:
    return func.count().filter(ModelInvocation.status == status)


async def invocation_metrics(
    session: AsyncSession,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    alias: str | None = None,
    job_id: uuid.UUID | None = None,
    purpose: str | None = None,
) -> list[AliasMetrics]:
    """Aggregate ``model_invocations`` per alias: volume, outcome counts, latency percentiles, tokens."""
    mi = ModelInvocation
    lat = cast(mi.latency_ms, Float)
    stmt = (
        select(
            mi.alias,
            func.count().label("calls"),
            _count("succeeded"),
            _count("invalid"),
            _count("failed"),
            _count("timeout"),
            _count("cancelled"),
            _count("started"),
            func.count().filter(mi.fallback_used.is_(True)),
            func.count().filter(mi.repair_attempt > 0),
            func.coalesce(func.sum(mi.prompt_tokens), 0),
            func.coalesce(func.sum(mi.completion_tokens), 0),
            func.coalesce(func.sum(mi.reasoning_chars), 0),
            func.avg(lat),
            func.percentile_cont(0.5).within_group(lat),
            func.percentile_cont(0.95).within_group(lat),
            func.percentile_cont(0.99).within_group(lat),
            func.max(mi.latency_ms),
        )
        .group_by(mi.alias)
        .order_by(mi.alias)
    )
    if since is not None:
        stmt = stmt.where(mi.started_at >= since)
    if until is not None:
        stmt = stmt.where(mi.started_at < until)
    if alias is not None:
        stmt = stmt.where(mi.alias == alias)
    if job_id is not None:
        stmt = stmt.where(mi.job_id == job_id)
    if purpose is not None:
        stmt = stmt.where(mi.purpose == purpose)
    out: list[AliasMetrics] = []
    for row in (await session.execute(stmt)).all():

        def _f(v: Any) -> float | None:
            return None if v is None else round(float(v), 3)

        out.append(
            AliasMetrics(
                alias=row[0],
                calls=int(row[1]),
                succeeded=int(row[2]),
                invalid=int(row[3]),
                failed=int(row[4]),
                timeouts=int(row[5]),
                cancelled=int(row[6]),
                in_flight=int(row[7]),
                fallback_calls=int(row[8]),
                repair_calls=int(row[9]),
                prompt_tokens=int(row[10]),
                completion_tokens=int(row[11]),
                reasoning_chars=int(row[12]),
                avg_latency_ms=_f(row[13]),
                p50_latency_ms=_f(row[14]),
                p95_latency_ms=_f(row[15]),
                p99_latency_ms=_f(row[16]),
                max_latency_ms=None if row[17] is None else int(row[17]),
            )
        )
    return out


async def error_breakdown(session: AsyncSession, *, since: datetime | None = None, alias: str | None = None) -> dict[str, dict[str, int]]:
    """``{alias: {error_code: count}}`` for failed/timeout/invalid invocations."""
    mi = ModelInvocation
    stmt = select(mi.alias, mi.error_code, func.count()).where(mi.error_code.is_not(None)).group_by(mi.alias, mi.error_code)
    if since is not None:
        stmt = stmt.where(mi.started_at >= since)
    if alias is not None:
        stmt = stmt.where(mi.alias == alias)
    out: dict[str, dict[str, int]] = {}
    for a, code, n in (await session.execute(stmt)).all():
        out.setdefault(str(a), {})[str(code)] = int(n)
    return out
