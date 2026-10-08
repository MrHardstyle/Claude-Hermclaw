"""Typed YAML configuration: hosts, models, policies, capabilities, logging (P02 2.3).

Files are read from ``<config_dir>/<name>.yaml``; if absent, ``<name>.example.yaml`` is used so that a
fresh checkout and the test-suite run without manual setup. Production installs copy the examples to
``/etc/hermclaw`` and adjust them (see docs/operations/INSTALLATION.md).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hermclaw.core.errors import ConfigError
from hermclaw.core.settings import get_settings


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------------------------- hosts
class WakeOnLanConfig(_Strict):
    enabled: bool = False
    mac: str = ""
    broadcast: str = "192.168.178.255"
    port: int = 9
    ping_timeout_seconds: int = 90
    service_timeout_seconds: int = 180
    max_attempts: int = 3

    @field_validator("mac")
    @classmethod
    def _mac(cls, v: str) -> str:
        if v:
            cleaned = v.replace("-", ":").lower()
            parts = cleaned.split(":")
            if len(parts) != 6 or any(len(p) != 2 for p in parts):
                raise ValueError(f"invalid MAC address: {v}")
            int("".join(parts), 16)
            return cleaned
        return v


class SshConfig(_Strict):
    user: str = "hermclaw-admin"
    port: int = 22
    key_ref: str = "file:/etc/hermclaw/ssh/id_ed25519"
    known_hosts: str = "/etc/hermclaw/ssh/known_hosts"
    allow_sudo_commands: list[str] = Field(default_factory=list)


class ServiceProbe(_Strict):
    name: str
    port: int
    http_path: str | None = None


HostRole = Literal["orchestrator", "webui", "execution_worker", "model_worker", "gitlab", "backup"]


class HostConfig(_Strict):
    id: str
    address: str
    role: HostRole
    worker_api: str | None = None
    worker_kind: Literal["execution", "model", "media"] | None = None
    wake_on_lan: WakeOnLanConfig = Field(default_factory=WakeOnLanConfig)
    ssh: SshConfig | None = None
    services: list[ServiceProbe] = Field(default_factory=list)
    idle_sleep_command: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class HostsConfig(_Strict):
    hosts: list[HostConfig]

    def by_id(self, host_id: str) -> HostConfig:
        for h in self.hosts:
            if h.id == host_id:
                return h
        raise ConfigError(f"unknown host '{host_id}'")

    def by_role(self, role: str) -> list[HostConfig]:
        return [h for h in self.hosts if h.role == role]

    @model_validator(mode="after")
    def _unique(self) -> HostsConfig:
        ids = [h.id for h in self.hosts]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate host ids")
        return self


# ----------------------------------------------------------------------------------------------- models
ModelRole = Literal["fast", "planner", "planner_fallback", "coder", "heavy", "embedding", "research"]


class ModelProfileConfig(_Strict):
    alias: str
    role: ModelRole
    model: str  # Ollama tag
    kind: Literal["chat", "embedding"] = "chat"
    host: str = "model-224"
    context_tokens: int = 8192
    max_output_tokens: int = 2048
    temperature: float = 0.2
    think: bool = False
    timeout_seconds: int = 300
    priority: int = 50
    resource_group: str = "large-model-224"
    exclusive: bool = True
    memory_gb: float = 4.0
    fallback_for: str | None = None
    embedding_dimensions: int | None = None
    enabled: bool = True


class LiteLLMConfig(_Strict):
    base_url: str = "http://127.0.0.1:4000"
    api_key_ref: str = "cred:litellm-master-key"
    request_timeout_seconds: int = 900


class ModelsConfig(_Strict):
    litellm: LiteLLMConfig = Field(default_factory=LiteLLMConfig)
    model_host_capacity_gb: float = 36.0
    profiles: list[ModelProfileConfig]

    def by_alias(self, alias: str) -> ModelProfileConfig:
        for p in self.profiles:
            if p.alias == alias:
                return p
        raise ConfigError(f"unknown model alias '{alias}'")

    def by_role(self, role: str) -> ModelProfileConfig:
        for p in self.profiles:
            if p.role == role and p.enabled:
                return p
        raise ConfigError(f"no enabled model profile for role '{role}'")

    def fallback_for(self, alias: str) -> ModelProfileConfig | None:
        for p in self.profiles:
            if p.fallback_for == alias and p.enabled:
                return p
        return None


# ----------------------------------------------------------------------------------------------- policies
class CommandPolicy(_Strict):
    forbidden_patterns: list[str] = Field(default_factory=list)
    destructive_patterns: list[str] = Field(default_factory=list)
    mutate_patterns: list[str] = Field(default_factory=list)
    read_patterns: list[str] = Field(default_factory=list)
    max_output_bytes: int = 200_000


class SandboxPolicy(_Strict):
    engine: Literal["podman", "docker", "local"] = "podman"
    image: str = "docker.io/library/python:3.12-slim"
    images: dict[str, str] = Field(default_factory=dict)
    cpus: float = 2.0
    memory: str = "2g"
    pids_limit: int = 512
    tmpfs_size: str = "512m"
    default_timeout_seconds: int = 600
    network_default: Literal["none", "allowed"] = "none"
    env_allowlist: list[str] = Field(default_factory=lambda: ["PATH", "HOME", "LANG", "LC_ALL", "CI", "PYTHONDONTWRITEBYTECODE"])


class ScopePolicy(_Strict):
    always_forbidden: list[str] = Field(
        default_factory=lambda: [".git/**", "**/.env", "**/*.pem", "**/*.key", "**/id_rsa*", "**/id_ed25519*"]
    )
    max_target_paths: int = 25
    max_new_paths: int = 25


class CoderPolicy(_Strict):
    max_turns: int = 20
    max_output_tokens: int = 6144
    tool_output_chars: int = 12000


class StagnationPolicy(_Strict):
    warn_after: int = 2
    diagnose_after: int = 3
    stop_after: int = 4


class CorrectionPolicy(_Strict):
    max_corrections_per_step: int = 2
    max_attempts_per_step: int = 3
    max_replans_per_job: int = 2


class VerifierPolicy(_Strict):
    max_changed_files: int = 40
    allow_deletions: bool = True
    generated_file_globs: list[str] = Field(default_factory=lambda: ["**/node_modules/**", "**/__pycache__/**", "**/*.pyc", "**/dist/**"])
    lint_commands: dict[str, str] = Field(default_factory=dict)


class ReviewPolicy(_Strict):
    required_for_kinds: list[str] = Field(default_factory=lambda: ["implement", "database", "docker", "deploy", "ssh"])
    timeout_seconds: int = 1200


class ResearchPolicy(_Strict):
    search_provider: Literal["searxng", "none"] = "searxng"
    searxng_url: str = "http://127.0.0.1:8888"
    max_queries: int = 4
    max_sources: int = 8
    fetch_timeout_seconds: int = 20
    max_fetch_bytes: int = 2_000_000
    primary_domains: list[str] = Field(default_factory=list)
    user_agent: str = "HermclawResearch/0.1 (+internal)"


class GitPolicy(_Strict):
    branch_prefix: str = "hermclaw/"
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master", "release/*"])
    author_name: str = "Hermclaw Runtime"
    author_email: str = "hermclaw@localhost"
    create_merge_request: bool = False


class EventPolicy(_Strict):
    retention_days: int = 90
    heartbeat_seconds: int = 15


class LeasePolicy(_Strict):
    default_ttl_seconds: int = 300
    heartbeat_seconds: int = 30
    preemption_grace_seconds: int = 120


class SshPolicy(_Strict):
    allowed_hosts: list[str] = Field(default_factory=list)
    default_timeout_seconds: int = 120
    require_approval_for_mutations: bool = True


class PoliciesConfig(_Strict):
    commands: CommandPolicy = Field(default_factory=CommandPolicy)
    sandbox: SandboxPolicy = Field(default_factory=SandboxPolicy)
    scope: ScopePolicy = Field(default_factory=ScopePolicy)
    coder: CoderPolicy = Field(default_factory=CoderPolicy)
    stagnation: StagnationPolicy = Field(default_factory=StagnationPolicy)
    correction: CorrectionPolicy = Field(default_factory=CorrectionPolicy)
    verifier: VerifierPolicy = Field(default_factory=VerifierPolicy)
    review: ReviewPolicy = Field(default_factory=ReviewPolicy)
    research: ResearchPolicy = Field(default_factory=ResearchPolicy)
    git: GitPolicy = Field(default_factory=GitPolicy)
    events: EventPolicy = Field(default_factory=EventPolicy)
    leases: LeasePolicy = Field(default_factory=LeasePolicy)
    ssh: SshPolicy = Field(default_factory=SshPolicy)


# ----------------------------------------------------------------------------------------------- capabilities
class CapabilityConfig(_Strict):
    name: str
    worker_kind: Literal["orchestrator", "execution", "model", "media"]
    model_role: str | None = None
    resources: list[str] = Field(default_factory=list)
    network: bool = False
    description: str = ""


class CapabilitiesConfig(_Strict):
    capabilities: list[CapabilityConfig]
    step_kind_capability: dict[str, str] = Field(default_factory=dict)

    def get(self, name: str) -> CapabilityConfig:
        for c in self.capabilities:
            if c.name == name:
                return c
        raise ConfigError(f"unknown capability '{name}'")


class LoggingConfig(_Strict):
    level: str = "INFO"
    json_output: bool = Field(default=True, alias="json")
    service: str = "hermclaw"

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class HermclawConfig(BaseModel):
    hosts: HostsConfig
    models: ModelsConfig
    policies: PoliciesConfig
    capabilities: CapabilitiesConfig
    logging: LoggingConfig


def _read_yaml(config_dir: Path, name: str) -> dict[str, Any]:
    for candidate in (config_dir / f"{name}.yaml", config_dir / f"{name}.example.yaml"):
        if candidate.exists():
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                raise ConfigError(f"invalid YAML in {candidate}: {exc}") from exc
            if not isinstance(data, dict):
                raise ConfigError(f"{candidate} must contain a mapping")
            return data
    raise ConfigError(f"missing configuration file {name}.yaml in {config_dir}")


def load_config(config_dir: Path | None = None) -> HermclawConfig:
    cdir = config_dir or get_settings().config_dir
    try:
        return HermclawConfig(
            hosts=HostsConfig.model_validate(_read_yaml(cdir, "hosts")),
            models=ModelsConfig.model_validate(_read_yaml(cdir, "models")),
            policies=PoliciesConfig.model_validate(_read_yaml(cdir, "policies")),
            capabilities=CapabilitiesConfig.model_validate(_read_yaml(cdir, "capabilities")),
            logging=LoggingConfig.model_validate(_read_yaml(cdir, "logging")),
        )
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"configuration invalid: {exc}") from exc


@lru_cache(maxsize=1)
def get_config() -> HermclawConfig:
    return load_config()


def reset_config_cache() -> None:
    get_config.cache_clear()
