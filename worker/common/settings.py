"""Worker daemon configuration from the environment (P07).

Environment variables (all optional unless marked):

=================================  ==========================================================================
``WORKER_ID`` (required)           registry id, must match ``config/hosts.yaml`` (e.g. ``exec-222``)
``WORKER_KIND``                    ``execution`` | ``model`` | ``media`` (default: the daemon's own kind)
``WORKER_TOKEN_FILE``              token file (one token per line, first = current). Default
                                   ``$CREDENTIALS_DIRECTORY/worker-token`` (systemd ``LoadCredential``),
                                   fallback ``/etc/hermclaw-worker/worker-token``
``ORCHESTRATOR_URL``               base URL of the orchestrator API (heartbeats); unset = no heartbeats
``WORKER_BIND``                    ``host:port`` to listen on (default ``127.0.0.1:8787``)
``WORKER_HEARTBEAT_SECONDS``       heartbeat interval (default 15; the orchestrator's ack may override it)
``WORKER_DATA_DIR``                state directory (default ``/var/lib/hermclaw-worker``)
``WORKER_CAPABILITIES``            comma separated capability names reported in heartbeats
``WORKER_CAPABILITIES_FILE``       alternatively a ``capabilities.yaml``: every capability whose
                                   ``worker_kind`` equals this daemon's kind is reported
``WORKER_HOSTNAME``                reported hostname (default ``socket.gethostname()``)
``WORKER_LOG_LEVEL``/``_LOG_JSON`` logging (default ``INFO`` / ``true``)
``WORKER_MAX_SKEW_SECONDS``        allowed clock skew of signed requests (default 120)
``WORKER_MAX_BODY_MB``             maximum request body (workspace upload) in MiB (default 1024)
``WORKER_MAX_CONCURRENT_COMMANDS`` execution worker: parallel sandbox commands (default 2)
``WORKER_POLICIES_FILE``           execution worker: ``policies.yaml`` whose ``sandbox`` section is used
``WORKER_CONTAINER_LABEL``         execution worker: label marking Hermclaw sandbox containers
``OLLAMA_URL``                     model worker: Ollama base URL (default ``http://127.0.0.1:11434``)
``WORKER_NVIDIA_SMI``              path/name of ``nvidia-smi`` (default ``nvidia-smi``)
=================================  ==========================================================================

The base capabilities of a kind (:data:`BASE_CAPABILITIES`) are always reported in addition to the
configured ones; they describe what the daemon's protocol endpoints provide, not task semantics.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hermclaw.contracts.common import WorkerKind
from hermclaw.core.config import SandboxPolicy
from hermclaw.core.errors import ConfigError

DEFAULT_BIND = "127.0.0.1:8787"
DEFAULT_DATA_DIR = Path("/var/lib/hermclaw-worker")
DEFAULT_TOKEN_FALLBACK = Path("/etc/hermclaw-worker/worker-token")
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_CONTAINER_LABEL = "hermclaw.managed=true"
TOKEN_CREDENTIAL_NAME = "worker-token"  # noqa: S105 - credential file name, not a secret

BASE_CAPABILITIES: dict[WorkerKind, tuple[str, ...]] = {
    WorkerKind.execution: ("sandbox", "workspace_sync"),
    WorkerKind.model: ("ollama", "gpu_telemetry"),
    WorkerKind.media: ("gpu_telemetry",),
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class WorkerDaemonSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    kind: WorkerKind
    token_file: Path
    orchestrator_url: str | None = None
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8787, ge=1, le=65535)
    heartbeat_seconds: float = Field(default=15.0, ge=1.0, le=600.0)
    data_dir: Path = DEFAULT_DATA_DIR
    capabilities: tuple[str, ...] = ()
    hostname: str = Field(default_factory=socket.gethostname)
    log_level: str = "INFO"
    log_json: bool = True
    max_skew_seconds: float = Field(default=120.0, ge=1.0, le=600.0)
    max_body_mb: int = Field(default=1024, ge=1, le=64 * 1024)
    max_concurrent_commands: int = Field(default=2, ge=1, le=64)
    sandbox: SandboxPolicy = Field(default_factory=SandboxPolicy)
    container_label: str = DEFAULT_CONTAINER_LABEL
    ollama_url: str = DEFAULT_OLLAMA_URL
    nvidia_smi: str = "nvidia-smi"
    comfyui_url: str | None = None  # media worker: ComfyUI API (e.g. http://127.0.0.1:8188)
    comfyui_workflows_dir: Path | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    @property
    def media_enabled(self) -> bool:
        return bool({"image", "video"} & set(self.capabilities))

    @property
    def max_body_bytes(self) -> int:
        return self.max_body_mb * 1024 * 1024

    @property
    def bind(self) -> str:
        return f"{self.bind_host}:{self.bind_port}"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    def all_capabilities(self) -> list[str]:
        """Base capabilities of the kind plus the configured ones (sorted, de-duplicated)."""
        return sorted({*BASE_CAPABILITIES.get(self.kind, ()), *self.capabilities})

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, default_kind: WorkerKind | None = None) -> WorkerDaemonSettings:
        env = os.environ if environ is None else environ
        worker_id = env.get("WORKER_ID", "").strip()
        if not worker_id:
            raise ConfigError("WORKER_ID is required")
        kind_raw = env.get("WORKER_KIND", "").strip() or (default_kind.value if default_kind else "")
        if not kind_raw:
            raise ConfigError("WORKER_KIND is required")
        try:
            kind = WorkerKind(kind_raw)
        except ValueError as exc:
            raise ConfigError(f"WORKER_KIND must be one of {[k.value for k in WorkerKind]}, got '{kind_raw}'") from exc
        if default_kind is not None and kind != default_kind:
            raise ConfigError(f"this daemon serves WORKER_KIND={default_kind.value}, environment says '{kind.value}'")

        values: dict[str, Any] = {"worker_id": worker_id, "kind": kind, "token_file": _token_file(env)}
        if url := env.get("ORCHESTRATOR_URL", "").strip():
            values["orchestrator_url"] = url.rstrip("/")
        host, port = parse_bind(env.get("WORKER_BIND", "").strip() or DEFAULT_BIND)
        values["bind_host"], values["bind_port"] = host, port
        for key, env_name in (
            ("heartbeat_seconds", "WORKER_HEARTBEAT_SECONDS"),
            ("max_skew_seconds", "WORKER_MAX_SKEW_SECONDS"),
        ):
            if raw := env.get(env_name, "").strip():
                values[key] = _float(env_name, raw)
        for key, env_name in (("max_body_mb", "WORKER_MAX_BODY_MB"), ("max_concurrent_commands", "WORKER_MAX_CONCURRENT_COMMANDS")):
            if raw := env.get(env_name, "").strip():
                values[key] = _int(env_name, raw)
        for key, env_name in (
            ("hostname", "WORKER_HOSTNAME"),
            ("log_level", "WORKER_LOG_LEVEL"),
            ("container_label", "WORKER_CONTAINER_LABEL"),
            ("nvidia_smi", "WORKER_NVIDIA_SMI"),
            ("ffmpeg", "WORKER_FFMPEG"),
            ("ffprobe", "WORKER_FFPROBE"),
        ):
            if raw := env.get(env_name, "").strip():
                values[key] = raw
        if raw := env.get("OLLAMA_URL", "").strip():
            values["ollama_url"] = raw.rstrip("/")
        if raw := env.get("WORKER_COMFYUI_URL", "").strip():
            values["comfyui_url"] = raw.rstrip("/")
        if raw := env.get("WORKER_COMFYUI_WORKFLOWS", "").strip():
            values["comfyui_workflows_dir"] = Path(raw)
        if raw := env.get("WORKER_DATA_DIR", "").strip():
            values["data_dir"] = Path(raw)
        if raw := env.get("WORKER_LOG_JSON", "").strip():
            values["log_json"] = _bool("WORKER_LOG_JSON", raw)
        values["capabilities"] = tuple(_capabilities(env, kind))
        if raw := env.get("WORKER_POLICIES_FILE", "").strip():
            values["sandbox"] = load_sandbox_policy(Path(raw))
        try:
            return cls(**values)
        except ValidationError as exc:
            raise ConfigError(f"invalid worker settings: {exc}") from exc


def parse_bind(value: str) -> tuple[str, int]:
    host, sep, port_s = value.rpartition(":")
    if not sep or not host or not port_s.isdigit():
        raise ConfigError(f"WORKER_BIND must be 'host:port', got '{value}'")
    port = int(port_s)
    if not 1 <= port <= 65535:
        raise ConfigError(f"WORKER_BIND port out of range: {port}")
    return host.strip("[]"), port


def _token_file(env: Mapping[str, str]) -> Path:
    if raw := env.get("WORKER_TOKEN_FILE", "").strip():
        return Path(raw)
    if cred := env.get("CREDENTIALS_DIRECTORY", "").strip():
        return Path(cred) / TOKEN_CREDENTIAL_NAME
    return DEFAULT_TOKEN_FALLBACK


def _capabilities(env: Mapping[str, str], kind: WorkerKind) -> list[str]:
    if raw := env.get("WORKER_CAPABILITIES", "").strip():
        return [c.strip() for c in raw.split(",") if c.strip()]
    if raw := env.get("WORKER_CAPABILITIES_FILE", "").strip():
        return capabilities_from_file(Path(raw), kind)
    return []


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping")
    return data


def capabilities_from_file(path: Path, kind: WorkerKind) -> list[str]:
    """Capability names of ``kind`` from a ``capabilities.yaml`` (same schema as the orchestrator config)."""
    from hermclaw.core.config import CapabilitiesConfig

    try:
        cfg = CapabilitiesConfig.model_validate(_read_yaml_mapping(path))
    except ValidationError as exc:
        raise ConfigError(f"invalid capabilities file {path}: {exc}") from exc
    return [c.name for c in cfg.capabilities if c.worker_kind == kind.value]


def load_sandbox_policy(path: Path) -> SandboxPolicy:
    """The ``sandbox`` section of a ``policies.yaml`` (defaults when the section is absent)."""
    data = _read_yaml_mapping(path)
    try:
        return SandboxPolicy.model_validate(data.get("sandbox") or {})
    except ValidationError as exc:
        raise ConfigError(f"invalid sandbox policy in {path}: {exc}") from exc


def _float(name: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got '{raw}'") from exc


def _int(name: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got '{raw}'") from exc


def _bool(name: str, raw: str) -> bool:
    low = raw.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean, got '{raw}'")
