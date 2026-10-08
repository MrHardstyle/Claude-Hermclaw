"""Typed HTTP clients for the worker daemons (P07).

- :class:`ExecutionWorkerClient` – execution worker ``.222`` (workspaces, sandbox commands, container recovery)
- :class:`ModelWorkerClient`     – model worker ``.224`` (Ollama residency, GPU telemetry, selftest)

Every request is HMAC-signed (:class:`hermclaw.workers.auth.WorkerRequestSigner`; a retry gets a fresh
nonce). Only idempotent ``GET`` requests are retried (transport errors and HTTP 502/503/504, exponential
backoff); ``POST``/``PUT``/``DELETE`` are never retried automatically - a ``POST /v1/commands`` with the same
``request_id`` is idempotent on the daemon side, so callers may re-submit deliberately.

Errors are typed (:mod:`hermclaw.workers.errors`): ``WorkerUnreachable``, ``WorkerTimeout``,
``WorkerAuthFailed``, ``WorkerBusy``, ``WorkerRemoteError`` (carries the daemon's error code),
``WorkerProtocolError``. The client emits no events itself - callers record the observable action
(``command.run``, ``model.load.*`` ...) with their job/step context.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Self, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from hermclaw.contracts.worker import CommandRequest, CommandResult, GpuInfo, LoadedModel, ModelLoadRequest, WorkspaceSyncManifest
from hermclaw.core.errors import HermclawError
from hermclaw.workers.auth import TokenStore, WorkerRequestSigner
from hermclaw.workers.errors import (
    WorkerAuthFailed,
    WorkerBusy,
    WorkerProtocolError,
    WorkerRemoteError,
    WorkerTimeout,
    WorkerUnreachable,
)
from hermclaw.workers.schemas import (
    CommandStatus,
    DaemonHealth,
    DeletePathsRequest,
    DeletePathsResult,
    GpuResponse,
    ModelLoadResult,
    ModelsResponse,
    ModelUnloadResult,
    RecoverResult,
    SelftestRequest,
    SelftestResult,
    WorkerInfo,
    WorkspaceInfo,
)

M = TypeVar("M", bound=BaseModel)

RETRY_STATUSES = frozenset({502, 503, 504})
COMMAND_TIMEOUT_MARGIN_SECONDS = 30.0
TAR_CONTENT_TYPE = "application/x-tar"


class WorkerClient:
    """Base client: signing, timeouts, GET retries, error mapping, health."""

    def __init__(
        self,
        base_url: str,
        *,
        worker_id: str,
        token: str | Callable[[], str],
        timeout_seconds: float = 30.0,
        connect_timeout_seconds: float = 5.0,
        get_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
        include_bearer: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id
        self.get_retries = max(0, get_retries)
        self.retry_backoff_seconds = retry_backoff_seconds
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            auth=WorkerRequestSigner(worker_id, token, include_bearer=include_bearer),
            timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds),
            transport=transport,
            headers={"User-Agent": "hermclaw-orchestrator/worker-client"},
        )

    @classmethod
    def for_worker(cls, info: WorkerInfo, token_store: TokenStore, **kwargs: Any) -> Self:
        """Client for a registry entry; the token is looked up per request (rotation-safe)."""
        if not info.api_url:
            raise WorkerUnreachable(f"worker '{info.id}' has no api_url", details={"worker_id": info.id})

        def current() -> str:
            tokens = token_store.tokens_for(info.id)
            if not tokens:
                raise WorkerAuthFailed(f"no credential configured for worker '{info.id}'", details={"worker_id": info.id})
            return tokens[0]

        current()  # fail fast when no credential is configured
        return cls(info.api_url, worker_id=info.id, token=current, **kwargs)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------------------------- plumbing
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        content: bytes | None = None,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        accept_statuses: Sequence[int] = (),
    ) -> httpx.Response:
        attempts = 1 + (self.get_retries if method == "GET" else 0)
        kwargs: dict[str, Any] = {"params": params, "headers": dict(headers or {})}
        if json_body is not None:
            kwargs["content"] = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            kwargs["headers"]["Content-Type"] = "application/json"
        elif content is not None:
            kwargs["content"] = content
        if timeout_seconds is not None:
            kwargs["timeout"] = httpx.Timeout(timeout_seconds, connect=self._http.timeout.connect)
        last_exc: HermclawError | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self.retry_backoff_seconds * (2 ** (attempt - 1)))
            try:
                resp = await self._http.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                kind = "connect" if isinstance(exc, httpx.ConnectTimeout) else "read"
                err_cls = WorkerUnreachable if kind == "connect" else WorkerTimeout
                last_exc = err_cls(
                    f"worker {self.worker_id} {method} {path}: {kind} timeout",
                    details={"worker_id": self.worker_id, "path": path, "attempt": attempt + 1},
                )
                continue
            except httpx.TransportError as exc:
                last_exc = WorkerUnreachable(
                    f"worker {self.worker_id} {method} {path}: {type(exc).__name__}",
                    details={"worker_id": self.worker_id, "path": path, "attempt": attempt + 1},
                )
                continue
            if resp.status_code in RETRY_STATUSES and attempt + 1 < attempts:
                last_exc = self._error_for(resp, method, path)
                continue
            if resp.status_code >= 400 and resp.status_code not in accept_statuses:
                raise self._error_for(resp, method, path)
            return resp
        assert last_exc is not None
        raise last_exc

    def _error_for(self, resp: httpx.Response, method: str, path: str) -> HermclawError:
        remote_code: str | None = None
        message = resp.reason_phrase or "error"
        details: dict[str, Any] = {"worker_id": self.worker_id, "path": path, "method": method}
        try:
            body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                remote_code = str(err.get("code") or "") or None
                message = str(err.get("message") or message)
                if isinstance(err.get("details"), dict):
                    details["remote_details"] = err["details"]
            elif isinstance(body, dict) and "detail" in body:
                message = str(body["detail"])[:500]
        except (ValueError, json.JSONDecodeError):
            pass
        text = f"worker {self.worker_id} {method} {path} -> HTTP {resp.status_code}: {message}"
        if resp.status_code in (401, 403):
            return WorkerAuthFailed(text, details={**details, "remote_code": remote_code, "status_code": resp.status_code})
        if resp.status_code == 503 and remote_code == "WORKER_BUSY":
            return WorkerBusy(text, status_code=resp.status_code, remote_code=remote_code, details=details)
        return WorkerRemoteError(text, status_code=resp.status_code, remote_code=remote_code, details=details)

    def _parse(self, resp: httpx.Response, model: type[M]) -> M:
        try:
            return model.model_validate_json(resp.content)
        except ValidationError as exc:
            raise WorkerProtocolError(
                f"worker {self.worker_id}: response does not match {model.__name__}",
                details={"errors": [e["msg"] for e in exc.errors()[:5]], "status_code": resp.status_code},
            ) from exc

    # ------------------------------------------------------------------------------------- common
    async def health(self) -> DaemonHealth:
        return self._parse(await self._request("GET", "/health"), DaemonHealth)

    async def selftest(self, request: SelftestRequest | None = None, *, timeout_seconds: float | None = 300.0) -> SelftestResult:
        body = (request or SelftestRequest()).model_dump(mode="json")
        return self._parse(await self._request("POST", "/v1/selftest", json_body=body, timeout_seconds=timeout_seconds), SelftestResult)


class ExecutionWorkerClient(WorkerClient):
    """Client for the execution worker daemon (``worker.execution.app``)."""

    async def upload_workspace(
        self, workspace: str, tar_bytes: bytes, *, mode: str = "replace", timeout_seconds: float | None = 600.0
    ) -> WorkspaceInfo:
        if mode not in ("replace", "merge"):
            raise ValueError("mode must be 'replace' or 'merge'")
        resp = await self._request(
            "PUT",
            f"/v1/workspaces/{workspace}",
            content=tar_bytes,
            params={"mode": mode},
            headers={"Content-Type": TAR_CONTENT_TYPE},
            timeout_seconds=timeout_seconds,
        )
        return self._parse(resp, WorkspaceInfo)

    async def download_workspace(
        self, workspace: str, *, paths: Sequence[str] | None = None, timeout_seconds: float | None = 600.0
    ) -> bytes:
        params = [("paths", p) for p in paths] if paths else None
        resp = await self._request("GET", f"/v1/workspaces/{workspace}/archive", params=params, timeout_seconds=timeout_seconds)
        ctype = resp.headers.get("content-type", "")
        if not ctype.startswith(TAR_CONTENT_TYPE):
            raise WorkerProtocolError(f"worker {self.worker_id}: expected a tar archive, got '{ctype}'")
        return resp.content

    async def workspace_manifest(self, workspace: str) -> WorkspaceSyncManifest:
        return self._parse(await self._request("GET", f"/v1/workspaces/{workspace}/manifest"), WorkspaceSyncManifest)

    async def workspace_info(self, workspace: str) -> WorkspaceInfo:
        return self._parse(await self._request("GET", f"/v1/workspaces/{workspace}"), WorkspaceInfo)

    async def delete_paths(self, workspace: str, paths: Sequence[str]) -> DeletePathsResult:
        body = DeletePathsRequest(paths=list(paths)).model_dump(mode="json")
        return self._parse(await self._request("POST", f"/v1/workspaces/{workspace}/delete", json_body=body), DeletePathsResult)

    async def delete_workspace(self, workspace: str) -> WorkspaceInfo:
        return self._parse(await self._request("DELETE", f"/v1/workspaces/{workspace}"), WorkspaceInfo)

    async def run_command(self, request: CommandRequest) -> CommandResult:
        """Run a sandboxed command; the HTTP timeout is the command timeout plus a safety margin."""
        http_timeout = float(request.timeout_seconds) + COMMAND_TIMEOUT_MARGIN_SECONDS
        body = request.model_dump(mode="json")
        resp = await self._request("POST", "/v1/commands", json_body=body, timeout_seconds=http_timeout)
        result = self._parse(resp, CommandResult)
        if result.request_id != request.request_id:
            raise WorkerProtocolError(f"worker {self.worker_id}: result for '{result.request_id}' instead of '{request.request_id}'")
        return result

    async def command_status(self, request_id: str) -> CommandStatus | None:
        """Status of an earlier command (e.g. after a dropped connection). ``None`` if unknown."""
        resp = await self._request("GET", f"/v1/commands/{request_id}", accept_statuses=(404,))
        if resp.status_code == 404:
            return None
        return self._parse(resp, CommandStatus)

    async def recover_containers(self, *, force: bool = False) -> RecoverResult:
        resp = await self._request("POST", "/v1/containers/recover", params={"force": str(force).lower()}, timeout_seconds=120.0)
        return self._parse(resp, RecoverResult)


class ModelWorkerClient(WorkerClient):
    """Client for the model worker daemon (``worker.model.app``)."""

    async def models(self) -> ModelsResponse:
        return self._parse(await self._request("GET", "/v1/models"), ModelsResponse)

    async def loaded_models(self) -> list[LoadedModel]:
        return (await self.models()).loaded

    async def load_model(
        self,
        request: ModelLoadRequest,
        *,
        exclusive: bool = False,
        keep: Sequence[str] = (),
        timeout_seconds: float | None = 900.0,
    ) -> ModelLoadResult:
        """Load ``request.model`` with ``num_ctx=request.context_tokens``. ``exclusive`` unloads every other
        resident model except ``keep`` first (large-model residency policy is decided by the resource manager)."""
        params: list[tuple[str, str]] = [("exclusive", str(exclusive).lower())] + [("keep", k) for k in keep]
        body = request.model_dump(mode="json")
        resp = await self._request("POST", "/v1/models/load", json_body=body, params=params, timeout_seconds=timeout_seconds)
        return self._parse(resp, ModelLoadResult)

    async def unload_model(self, model: str, *, timeout_seconds: float | None = 180.0) -> ModelUnloadResult:
        resp = await self._request("POST", "/v1/models/unload", json_body={"model": model}, timeout_seconds=timeout_seconds)
        return self._parse(resp, ModelUnloadResult)

    async def gpu_info(self) -> GpuResponse:
        return self._parse(await self._request("GET", "/v1/gpu"), GpuResponse)

    async def gpus(self) -> list[GpuInfo]:
        return (await self.gpu_info()).gpus


async def probe_health(base_url: str, *, timeout_seconds: float = 5.0, transport: httpx.AsyncBaseTransport | None = None) -> DaemonHealth:
    """Unauthenticated ``GET /health`` (readiness probe used e.g. by the Wake-on-LAN controller)."""
    try:
        async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport) as http:
            resp = await http.get("/health")
    except httpx.TimeoutException as exc:
        raise WorkerTimeout(f"{base_url}/health timed out") from exc
    except httpx.TransportError as exc:
        raise WorkerUnreachable(f"{base_url}/health unreachable: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise WorkerRemoteError(f"{base_url}/health -> HTTP {resp.status_code}", status_code=resp.status_code)
    try:
        return DaemonHealth.model_validate_json(resp.content)
    except ValidationError as exc:
        raise WorkerProtocolError(f"{base_url}/health: unexpected response") from exc
