"""Typed YAML configuration: hosts, models, policies, capabilities, logging (P02 2.3).

Files are read from ``<config_dir>/<name>.yaml``; if absent, ``<name>.example.yaml`` is used so that a
fresh checkout and the test-suite run without manual setup. Production installs copy the examples to
``/etc/hermclaw`` and adjust them (see docs/operations/INSTALLATION.md).
"""

from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator, model_validator

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
_TEMPLATE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ModelProfileConfig(_Strict):
    alias: str
    role: ModelRole
    model: str  # Ollama tag (backend ollama) or the model name llama-server serves (backend llamacpp, ``--alias``)
    kind: Literal["chat", "embedding"] = "chat"
    # ollama: Ollama loads the model per request. llamacpp: a resident llama-server on the profile's host (service
    # ``llamacpp`` in hosts.yaml) serves it over its OpenAI-compatible API (D-015: MoE experts in RAM, attention on the GPU)
    backend: Literal["ollama", "llamacpp"] = "ollama"
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
    # Ollama ``num_gpu``: layers offloaded to the GPU. Unset = Ollama's own memory estimate. Set it only where that
    # estimate overcommits the GPU (BUG-150: gemma4:12b at 32K on the 8 GB GTX 1080 fails with a CUDA allocation
    # error instead of offloading to the CPU).
    gpu_layers: int | None = Field(default=None, ge=0, le=1000)
    enabled: bool = True
    # A documented decision (DECISIONS.md) to run this role on another model than the architecture names: the check
    # reports the deviation as a warning instead of an error. Empty = the architecture's model is required.
    deviation: str = Field(default="", max_length=500)
    # llamacpp only: further variables for the model's chat template, sent with every request next to
    # ``enable_thinking`` (Research R-035: gpt-oss-20b reasons at ``reasoning_effort: low``). JSON scalars keep their
    # type; ``enable_thinking`` itself stays the job of ``think``.
    chat_template_kwargs: dict[str, StrictBool | StrictInt | StrictFloat | StrictStr] = Field(default_factory=dict, max_length=16)
    # llamacpp only: the most tokens one answer may think (``thinking_budget_tokens`` of the request, read by llama-server
    # and Strata); unset = the server's own default. A coder turn on Strata thought ~2,200 of its median 2,333 tokens
    # (G1 on rc87), so the budget is the lever on the time of a turn.
    thinking_budget_tokens: int | None = Field(default=None, ge=0, le=65536)

    @model_validator(mode="after")
    def _template_kwargs(self) -> ModelProfileConfig:
        for key, value in self.chat_template_kwargs.items():
            if not _TEMPLATE_KEY.match(key) or key == "enable_thinking":
                raise ValueError(f"{self.alias}: chat_template_kwargs key {key!r} not allowed (enable_thinking is set by think)")
            if isinstance(value, str) and len(value) > 200:
                raise ValueError(f"{self.alias}: chat_template_kwargs value of {key!r} longer than 200 characters")
        return self


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


#: environment variables the *runtime* sets on sandbox commands (never the model – tool calls carry no environment):
#: the database tools point a project's migration tool at the throw-away sandbox database (P27), the verifier marks
#: its commands as CI runs. Always allowed in addition to ``policies.sandbox.env_allowlist``, so an operator override
#: of the allow-list cannot break them (BUG-084).
RUNTIME_SANDBOX_ENV: tuple[str, ...] = (
    "CI",
    "DATABASE_URL",
    "SQLALCHEMY_DATABASE_URI",
    "HERMCLAW_SANDBOX_URL",
    "HERMCLAW_ALEMBIC_*",
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "PGPASSWORD",
    "PGDATABASE",
    # libpq parameters a sandbox-server DSN of databases.yaml may carry (hermclaw.dbtools.targets._LIBPQ_ENV)
    "PGSSLMODE",
    "PGSSLROOTCERT",
    "PGSSLCERT",
    "PGSSLKEY",
    "PGSSLCRL",
    "PGCONNECT_TIMEOUT",
    "PGAPPNAME",
    "PGTARGETSESSIONATTRS",
    "PGOPTIONS",
    "PGGSSENCMODE",
    "PGCHANNELBINDING",
    "PYTHONDONTWRITEBYTECODE",
)


DEFAULT_EGRESS_ALLOWED_HOSTS: tuple[str, ...] = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")


class EgressPolicy(_Strict):
    """Registry-only network for dependency installs (D-014).

    An install command runs on ``internal_network`` (podman ``--internal``: no route outside); the only peer there
    is the ``hermclaw-egress-proxy`` container at ``proxy_ip``, which is also attached to ``outbound_network`` and
    forwards CONNECT tunnels to ``allowed_hosts`` only (``worker/execution/egress_proxy.py``)."""

    enabled: bool = False
    allowed_hosts: list[str] = Field(default_factory=lambda: list(DEFAULT_EGRESS_ALLOWED_HOSTS), min_length=1)
    internal_network: str = Field(default="hermclaw-egress", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
    outbound_network: str = Field(default="hermclaw-egress-out", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
    internal_subnet: str = "10.89.240.0/24"
    outbound_subnet: str = "10.89.241.0/24"
    proxy_ip: str = "10.89.240.2"
    proxy_port: int = Field(default=3128, ge=1, le=65535)
    proxy_container: str = Field(default="hermclaw-egress-proxy", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
    #: image with python3 that runs the proxy script (the generic toolchain image has it)
    proxy_image: str = "localhost/hermclaw-sandbox-generic:1"
    #: resolvers of the proxy container; empty = the worker host's non-loopback nameservers (/etc/resolv.conf)
    dns: list[str] = Field(default_factory=list)
    install_timeout_seconds: int = Field(default=900, ge=30, le=7200)

    @model_validator(mode="after")
    def _addresses(self) -> EgressPolicy:
        internal = ipaddress.ip_network(self.internal_subnet)
        ipaddress.ip_network(self.outbound_subnet)
        if ipaddress.ip_address(self.proxy_ip) not in internal:
            raise ValueError(f"egress.proxy_ip {self.proxy_ip} is not inside egress.internal_subnet {self.internal_subnet}")
        if self.internal_network == self.outbound_network:
            raise ValueError("egress.internal_network and egress.outbound_network must differ")
        for host in self.allowed_hosts:
            h = host.strip().lstrip(".")
            if not h or "/" in h or ":" in h or " " in h or "*" in h:
                raise ValueError(f"egress.allowed_hosts: {host!r} is not a host name (exact, or .domain for subdomains)")
        return self

    @property
    def proxy_url(self) -> str:
        return f"http://{self.proxy_ip}:{self.proxy_port}"


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
    egress: EgressPolicy = Field(default_factory=EgressPolicy)

    @property
    def request_env_allowlist(self) -> list[str]:
        """Names a sandbox request may set: the configured allow-list plus the runtime's own (``RUNTIME_SANDBOX_ENV``)."""
        return [*self.env_allowlist, *(n for n in RUNTIME_SANDBOX_ENV if n not in self.env_allowlist)]


class ScopePolicy(_Strict):
    always_forbidden: list[str] = Field(
        default_factory=lambda: [".git/**", "**/.env", "**/*.pem", "**/*.key", "**/id_rsa*", "**/id_ed25519*"]
    )
    max_target_paths: int = 25
    max_new_paths: int = 25


class CoderPolicy(_Strict):
    max_turns: int = 20
    # a step with many files gets more turns, two of every five for writing a file, up to this limit: 20 turns for 11
    # files left 9 turns for tests and fixes and the step ran out (ladder A10 #5, BUG-339)
    max_turns_per_step: int = Field(default=40, ge=1, le=200)
    # complete_step in the last turn while a check still fails: these turns once more, with the check's output, instead of
    # a verifier run and a new attempt that re-reads every file first (live G1: twice ~45 min, BUG-349); 0 switches it off
    grace_turns: int = Field(default=3, ge=0, le=10)
    max_output_tokens: int = 6144
    tool_output_chars: int = 12000
    # a command the coder starts without timeout_seconds: a hanging test (a server it never stops) blocked a turn for
    # the full sandbox limit of 600 s (rc19 battery J1, BUG-205); longer suites ask for more, up to that limit
    default_command_timeout_seconds: int = Field(default=180, ge=10, le=7200)
    # after the verifier passed, one model call matches the step's own tests to the requirements of the task; a
    # requirement no test checks becomes one correction (Research R-035: A7–A9 failed on stated requirements the
    # delivered tests never exercised); false switches it off
    coverage_check: bool = True


class StagnationPolicy(_Strict):
    warn_after: int = 2
    diagnose_after: int = 3
    stop_after: int = 4
    sequence_length: int = 3  # n of the "same tool sequence" n-gram
    max_diagnoses: int = 2  # a further forced diagnosis escalates to stop
    strip_line_numbers: bool = True  # line/column numbers do not distinguish error signatures


class CorrectionPolicy(_Strict):
    max_corrections_per_step: int = 2
    max_attempts_per_step: int = 3
    #: replans in a row without progress: a replan after which another step completed starts a fresh budget, so a long
    #: job is not failed for its third unrelated problem hours after the first two (BUG-213)
    max_replans_per_job: int = 2
    #: hard ceiling over the whole job, progress or not
    max_replans_total: int = 12


#: defect-only lint of changed files (pyflakes F + E9 syntax, no style rules); skipped where the image has no ruff. The
#: delivered code of rc23 battery jobs carried unused variables and dead code that no check reported (BUG-215).
DEFAULT_LINT_COMMANDS: dict[str, str] = {
    "python": (
        "command -v ruff >/dev/null 2>&1 || exit 0; ruff check --no-cache --quiet --output-format concise "
        '--select F,E9 --ignore F403,F405 --per-file-ignores "__init__.py:F401" {files}'
    ),
    # the compiler's own warnings (unused variables and imports, dead code) and clippy's correctness and suspicious
    # groups, no style lints; the crate, not the files – the Rust ladder on rc98 had no lint at all (BUG-371)
    "rust": (
        "command -v cargo >/dev/null 2>&1 && [ -f Cargo.toml ] || exit 0; cargo clippy --offline --quiet --all-targets -- "
        "-D warnings -A clippy::all -D clippy::correctness -D clippy::suspicious"
    ),
    # go vet: format strings, unreachable code, copied locks … for the module (BUG-371)
    "go": "command -v go >/dev/null 2>&1 && [ -f go.mod ] || exit 0; go vet ./...",
}


class VerifierPolicy(_Strict):
    max_changed_files: int = 40
    allow_deletions: bool = True
    generated_file_globs: list[str] = Field(default_factory=lambda: ["**/node_modules/**", "**/__pycache__/**", "**/*.pyc", "**/dist/**"])
    lint_commands: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_LINT_COMMANDS))
    # a changed package with a __main__.py is started once in the sandbox (BUG-309): passed when it still runs after
    # this many seconds or exits without a traceback; 0 disables the check
    start_seconds: float = Field(default=5, ge=0, le=60)
    # a Python program that serves HTTP is started once and loaded like its user loads it (BUG-346): seconds it gets to
    # listen; 0 disables the check
    serve_seconds: float = Field(default=15, ge=0, le=120)


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
    browser_render: bool = False  # render JS pages with a headless Chromium (HERMCLAW_CHROMIUM or auto-detected)
    browser_executable: str | None = None


class GitPolicy(_Strict):
    branch_prefix: str = "hermclaw/"
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master", "release/*"])
    author_name: str = "Hermclaw Runtime"
    author_email: str = "hermclaw@localhost"
    create_merge_request: bool = False
    # job workspaces (and their workspace-scoped code index) of finished jobs are removed this many hours after
    # the job ended (hermclaw.runtime.maintenance.WorkspaceJanitor); 0 keeps them forever
    workspace_retention_hours: int = Field(default=72, ge=0)


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
    # full-match regexes of chained mutate commands that are explicitly allowed (';', '&&', '|' … are refused otherwise)
    compound_allowlist: list[str] = Field(default_factory=list)


class WakeOnLanPolicy(_Strict):
    """Global Wake-on-LAN / idle sleep tuning (``hermclaw.wol``). MAC, broadcast, per-host ping/service timeouts
    and attempts stay in ``hosts.yaml`` (``wake_on_lan``), the sleep command in ``hosts.yaml`` ``idle_sleep_command``.
    Mapped by ``hermclaw.runtime.main.wake_settings_from_policy`` / ``idle_settings_from_policy``."""

    # a ready worker without any work for this long is put to sleep by the idle hook
    idle_after_minutes: float = Field(default=30.0, gt=0)
    # timeout of one idle sleep command (the SSH session often drops when the host suspends)
    idle_command_timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    # pause between two failed readiness probes while a woken host boots
    probe_interval_seconds: float = Field(default=2.0, gt=0, le=60)


class MemoryPolicy(_Strict):
    """Hermclaw's knowledge (D-019): what planner and coder are given and what the runtime learns by itself."""

    enabled: bool = True  # the recall tool, lessons in planner/coder prompts, learning after jobs
    recall_k: int = Field(default=5, ge=1, le=20)  # entries a recall returns at most
    planner_entries: int = Field(default=4, ge=0, le=20)  # active entries given to planner/replanner with the job
    coder_entries: int = Field(default=2, ge=0, le=10)  # active entries matching a failure, given with the correction
    # active lessons/conventions closest to the step goal and job text, in the coder's STEP GOAL from the first turn:
    # the planner had the PowerShell JSON convention and the coder never saw it (ladder A9 #4 on rc73, BUG-325)
    coder_start_entries: int = Field(default=8, ge=0, le=10)
    # ... all of them at least this similar (BUG-328): five PowerShell entries fit A9 (0.52–0.63, the rest <= 0.43), a
    # fixed two left out the two that would have saved ladder A9 #6 on rc75; the STEP GOAL keeps whole entries in budget
    # 0.45 since BUG-331: for the Python task A8 the fitting entries scored 0.45–0.57, unrelated ones at most 0.40
    coder_start_min_score: float = Field(default=0.45, ge=0.0, le=1.0)
    min_score: float = Field(default=0.55, ge=0.0, le=1.0)  # cosine similarity below which an entry is not relevant
    max_entry_chars: int = Field(default=1200, ge=200, le=8000)  # one entry as a model sees it
    learn: bool = True  # record incidents of failed/stalled work after a job
    promote_after: int = Field(default=2, ge=1, le=20)  # jobs with the same incident before its lesson becomes active
    learn_interval_seconds: float = Field(default=120.0, ge=10.0, le=86400.0)  # pause between two learning passes
    learn_batch: int = Field(default=20, ge=1, le=500)  # finished jobs one learning pass reads at most
    learn_since_days: int = Field(default=30, ge=0, le=3650)  # finished jobs older than this are not read (0 = all)
    min_repeats: int = Field(default=2, ge=1, le=20)  # refusals of one tool with one error code in a job that count
    research_days: int = Field(default=180, ge=1, le=3650)  # validity of a research finding in the knowledge
    # D-025: lessons from fixed mistakes – one model call per fix after the job, a candidate until seen in promote_after jobs
    lessons: bool = True
    lessons_per_job: int = Field(default=2, ge=0, le=10)  # fixes of one job turned into lessons at most
    lesson_alias: str | None = None  # model profile that writes them (default: the coder role)
    # the same lesson in other words scored 0.76–0.83 with embeddinggemma, different lessons at most 0.58 (rc73)
    lesson_similarity: float = Field(default=0.72, ge=0.5, le=1.0)  # a lesson this close to a known one is the same
    lessons_since_days: int = Field(default=2, ge=0, le=3650)  # jobs finished earlier get no lessons (0 = all)
    lesson_wait_seconds: float = Field(default=30.0, ge=0.0, le=3600.0)  # model lease wait; busy GPU = next pass


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
    wake_on_lan: WakeOnLanPolicy = Field(default_factory=WakeOnLanPolicy)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)


# ----------------------------------------------------------------------------------------------- capabilities
class CapabilityConfig(_Strict):
    name: str
    worker_kind: Literal["orchestrator", "execution", "model", "media"]
    model_role: str | None = None
    resources: list[str] = Field(default_factory=list)
    network: bool = False
    allow_destructive_commands: bool = False  # tool engine: destructive shell commands need an explicit opt-in
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
