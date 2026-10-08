"""Worker protocol contracts (Bauplan §24, P07): heartbeat, capability, task input/result."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from hermclaw.contracts.common import Contract, WorkerKind, WorkerState

WORKER_PROTOCOL_VERSION = 1


class GpuInfo(Contract):
    index: int = 0
    name: str = ""
    memory_total_mb: int = 0
    memory_used_mb: int = 0
    utilization_percent: int = 0
    temperature_c: int | None = None
    driver: str = ""


class LoadedModel(Contract):
    name: str
    size_bytes: int = 0
    size_vram_bytes: int = 0
    context_length: int | None = None
    expires_at: str | None = None


class WorkerHeartbeat(Contract):
    worker_id: str
    hostname: str
    kind: WorkerKind
    state: WorkerState
    protocol_version: int = WORKER_PROTOCOL_VERSION
    worker_version: str
    capabilities: list[str] = Field(default_factory=list)
    cpu_percent: float = 0.0
    load_avg: list[float] = Field(default_factory=list)
    ram_total_mb: int = 0
    ram_used_mb: int = 0
    disk_free_mb: int = 0
    gpus: list[GpuInfo] = Field(default_factory=list)
    loaded_models: list[LoadedModel] = Field(default_factory=list)
    active_job: str | None = None
    active_step: str | None = None
    uptime_seconds: int = 0
    service_versions: dict[str, str] = Field(default_factory=dict)
    sent_at: datetime


class CommandRequest(Contract):
    """Execution worker: run a command inside the sandbox for a workspace."""

    request_id: str
    job_id: str
    step_id: str
    workspace: str = Field(description="workspace id on the execution worker")
    command: str = Field(min_length=1, max_length=8000)
    image: str | None = None
    timeout_seconds: int = Field(default=600, ge=1, le=7200)
    network: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    cpus: float | None = None
    memory: str | None = None


class CommandResult(Contract):
    request_id: str
    exit_code: int | None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_ms: int = 0
    sandbox: Literal["podman", "docker", "local"] = "podman"
    container_name: str | None = None
    error: str | None = None


class WorkspaceSyncManifest(Contract):
    workspace: str
    files: dict[str, str] = Field(default_factory=dict, description="path -> sha256")
    deleted: list[str] = Field(default_factory=list)


class ModelLoadRequest(Contract):
    model: str
    context_tokens: int = Field(ge=512)
    keep_alive: str = "10m"


class ModelUnloadRequest(Contract):
    model: str


class MediaJobRequest(Contract):
    request_id: str
    job_id: str
    step_id: str
    kind: Literal["image", "video"]
    backend: Literal["ffmpeg", "comfyui"]
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=3600, ge=1, le=6 * 3600)


class MediaJobResult(Contract):
    request_id: str
    ok: bool
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0


class WorkerInput(Contract):
    """Generic envelope for dispatching a step to a worker."""

    job_id: str
    step_id: str
    attempt_id: str
    capability: str
    payload: dict[str, Any] = Field(default_factory=dict)
    deadline_at: datetime | None = None


class WorkerResult(Contract):
    job_id: str
    step_id: str
    attempt_id: str
    ok: bool
    outcome: Literal["completed", "blocked", "failed", "checkpointed", "cancelled"]
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
