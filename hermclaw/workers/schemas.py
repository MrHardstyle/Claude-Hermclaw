"""Worker API schemas (P07 7.1).

Request bodies of the worker protocol live in :mod:`hermclaw.contracts.worker` (strict contracts:
``WorkerHeartbeat``, ``CommandRequest``, ``CommandResult``, ``ModelLoadRequest``, ...). This module adds
the *response* and registry views shared by the orchestrator API, the worker daemons and the client.

Responses parsed by a client use :class:`ResponseModel` (unknown fields ignored) so a newer daemon can
add fields without breaking an older orchestrator within the same protocol version. Everything is
plain Pydantic and import-light (no SQLAlchemy) because the daemons import it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hermclaw.contracts.common import Contract, WorkerKind, WorkerState
from hermclaw.contracts.worker import WORKER_PROTOCOL_VERSION, CommandResult, GpuInfo, LoadedModel

WORKSPACE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
WORKER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"


class ResponseModel(BaseModel):
    """Lenient base for responses: unknown fields are ignored (forward compatible)."""

    model_config = ConfigDict(extra="ignore")


class ErrorBody(ResponseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ResponseModel):
    """Shape of every error answer of the worker API and the daemons: ``{"error": {...}}``."""

    error: ErrorBody


# ============================================================================================ registry
class HeartbeatAck(ResponseModel):
    """Orchestrator answer to ``POST /api/workers/heartbeat``."""

    accepted: bool
    worker_id: str
    state: WorkerState
    compatible: bool
    expected_protocol_version: int = WORKER_PROTOCOL_VERSION
    heartbeat_interval_seconds: float
    server_time: datetime
    stale: bool = False
    message: str = ""


class WorkerResources(ResponseModel):
    cpu_percent: float = 0.0
    load_avg: list[float] = Field(default_factory=list)
    ram_total_mb: int = 0
    ram_used_mb: int = 0
    disk_free_mb: int = 0
    gpus: list[GpuInfo] = Field(default_factory=list)
    loaded_models: list[LoadedModel] = Field(default_factory=list)
    uptime_seconds: int = 0


class WorkerInfo(ResponseModel):
    """Registry view of one worker (``GET /api/workers``)."""

    id: str
    hostname: str
    address: str
    kind: WorkerKind
    state: WorkerState
    api_url: str | None = None
    worker_version: str | None = None
    protocol_version: int | None = None
    compatible: bool = True
    last_heartbeat_at: datetime | None = None
    heartbeat_age_seconds: float | None = None
    active_job: str | None = None
    active_step: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    declared_capabilities: list[str] = Field(default_factory=list)
    wol_enabled: bool = False
    admin_drain: bool = False
    state_reason: str | None = None
    state_changed_at: datetime | None = None
    service_versions: dict[str, str] = Field(default_factory=dict)
    resources: WorkerResources | None = None
    in_config: bool = True


class WorkerHealthSample(ResponseModel):
    created_at: datetime
    state: WorkerState
    cpu_percent: float
    ram_total_mb: int
    ram_used_mb: int
    disk_free_mb: int
    gpus: list[dict[str, Any]] = Field(default_factory=list)
    loaded_models: list[dict[str, Any]] = Field(default_factory=list)
    active_job: str | None = None
    active_step: str | None = None
    uptime_seconds: int = 0


class WorkerDetail(WorkerInfo):
    health: list[WorkerHealthSample] = Field(default_factory=list)


class DrainRequest(Contract):
    drain: bool = True
    reason: str = Field(default="operator", max_length=200)


# ============================================================================================ daemons
class DaemonHealth(ResponseModel):
    """``GET /health`` of a worker daemon (unauthenticated, no sensitive data)."""

    status: Literal["ok", "degraded"]
    worker_id: str
    kind: WorkerKind
    state: WorkerState
    protocol_version: int = WORKER_PROTOCOL_VERSION
    worker_version: str
    uptime_seconds: int = 0
    checks: dict[str, bool] = Field(default_factory=dict)
    orchestrator_reachable: bool | None = None


class WorkspaceInfo(ResponseModel):
    workspace: str
    exists: bool = True
    files: int = 0
    bytes: int = 0
    mode: Literal["replace", "merge", "none"] = "none"
    duration_ms: int = 0


class DeletePathsRequest(Contract):
    paths: list[str] = Field(min_length=1, max_length=10_000)


class DeletePathsResult(ResponseModel):
    workspace: str
    deleted: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class CommandStatus(ResponseModel):
    request_id: str
    status: Literal["running", "finished"]
    result: CommandResult | None = None


class RecoverResult(ResponseModel):
    removed: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    engine: str = ""


class ModelsResponse(ResponseModel):
    loaded: list[LoadedModel] = Field(default_factory=list)
    installed: list[str] = Field(default_factory=list)


class ModelLoadResult(ResponseModel):
    model: str
    loaded: bool
    context_length: int | None = None
    size_vram_bytes: int = 0
    unloaded_others: list[str] = Field(default_factory=list)
    duration_ms: int = 0


class ModelUnloadResult(ResponseModel):
    model: str
    unloaded: bool
    was_loaded: bool = True
    duration_ms: int = 0


class GpuResponse(ResponseModel):
    available: bool
    gpus: list[GpuInfo] = Field(default_factory=list)
    error: str | None = None


class SelftestRequest(Contract):
    model: str | None = Field(default=None, description="optional: run a tiny inference on this model")
    context_tokens: int = Field(default=2048, ge=512, le=131072)


class SelftestCheck(ResponseModel):
    name: str
    ok: bool
    detail: str = ""
    duration_ms: int = 0


class SelftestResult(ResponseModel):
    ok: bool
    worker_id: str
    checks: list[SelftestCheck] = Field(default_factory=list)


def worker_api_schemas() -> dict[str, dict[str, Any]]:
    """JSON schemas of every worker-protocol model (documentation / contract tests)."""
    from hermclaw.contracts import worker as contracts

    models: list[type[BaseModel]] = [
        contracts.WorkerHeartbeat,
        contracts.CommandRequest,
        contracts.CommandResult,
        contracts.WorkspaceSyncManifest,
        contracts.ModelLoadRequest,
        contracts.ModelUnloadRequest,
        HeartbeatAck,
        WorkerInfo,
        WorkerDetail,
        DrainRequest,
        DaemonHealth,
        WorkspaceInfo,
        DeletePathsRequest,
        DeletePathsResult,
        CommandStatus,
        RecoverResult,
        ModelsResponse,
        ModelLoadResult,
        ModelUnloadResult,
        GpuResponse,
        SelftestRequest,
        SelftestResult,
        ErrorResponse,
    ]
    return {m.__name__: m.model_json_schema() for m in models}
