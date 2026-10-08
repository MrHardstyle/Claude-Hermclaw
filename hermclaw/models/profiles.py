"""Model profiles (P08 8.2–8.7): registry over ``models.yaml``, architecture checks, DB sync, secret resolution.

The role → model mapping is fixed by the architecture (Bauplan §3, CLAUDE.md):

=================  ======================  ====================================================
role               Ollama model family     profile requirements
=================  ======================  ====================================================
fast               Qwen3 8B                chat, context ≥ 16K                       (8.3)
planner            Gemma 4 26B A4B         chat, context ≥ 32K                       (8.4)
planner_fallback   Gemma 4 12B             chat, ``fallback_for`` = planner alias    (8.4)
coder              Qwen3-Coder 30B         chat, context ≥ 32K, output 4K–8K         (8.5)
heavy              Qwen3.8 27B             chat, context 24K–32K                     (8.6)
embedding          EmbeddingGemma 2        embedding, ``embedding_dimensions`` set   (8.7)
=================  ======================  ====================================================

``check_architecture`` reports deviations; the runtime refuses to start the gateway on errors (no silent model
replacement).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.core.config import HostsConfig, ModelProfileConfig, ModelsConfig
from hermclaw.core.errors import ConfigError, NotFoundError
from hermclaw.persistence.models import ModelProfile

DEFAULT_OLLAMA_PORT = 11434


# ----------------------------------------------------------------------------------------------- secrets
def resolve_api_key(ref: str | None, *, required: bool = True, env: str | None = None) -> str | None:
    """Resolve a secret reference (``cred:``/``file:``/``env:``; ``literal:`` only in tests).

    Delegates to the shared ``SecretStore`` (production policy: no ``env:``/``literal:``, file mode 0400) which also
    registers the value with the default redactor so the key can never leak into logs, events or prompts."""
    from hermclaw.security.secrets import SecretStore

    if not ref:
        if required:
            raise ConfigError("no secret reference configured", code="SECRET_MISSING")
        return None
    if ":" not in ref:
        raise ConfigError("secret reference must look like 'cred:<name>', 'file:<path>' or 'env:<NAME>'", code="SECRET_REF_INVALID")
    return SecretStore(env=env).resolve(ref, required=required)


# ----------------------------------------------------------------------------------------------- model tags
def normalize_model_tag(name: str) -> str:
    """``qwen3`` and ``qwen3:latest`` name the same Ollama model."""
    n = name.strip()
    return n if ":" in n.rsplit("/", 1)[-1] else f"{n}:latest"


def same_model(a: str, b: str) -> bool:
    return normalize_model_tag(a) == normalize_model_tag(b)


# ----------------------------------------------------------------------------------------------- architecture
@dataclass(frozen=True)
class RoleRequirement:
    role: str
    model_prefixes: tuple[str, ...]
    kind: Literal["chat", "embedding"] = "chat"
    min_context: int | None = None
    max_context: int | None = None
    min_output: int | None = None
    max_output: int | None = None
    required: bool = True
    step: str = ""


ARCHITECTURE: tuple[RoleRequirement, ...] = (
    RoleRequirement("fast", ("qwen3:8b",), min_context=16384, step="8.3"),
    RoleRequirement("planner", ("gemma4:26b",), min_context=32768, step="8.4"),
    RoleRequirement("planner_fallback", ("gemma4:12b",), min_context=32768, step="8.4"),
    RoleRequirement("coder", ("qwen3-coder:30b",), min_context=32768, min_output=4096, max_output=8192, step="8.5"),
    RoleRequirement("heavy", ("qwen3.8:27b",), min_context=24576, max_context=32768, step="8.6"),
    RoleRequirement("embedding", ("embeddinggemma",), kind="embedding", step="8.7"),
)


@dataclass(frozen=True)
class ProfileIssue:
    severity: Literal["error", "warning"]
    role: str
    alias: str | None
    message: str

    def as_dict(self) -> dict[str, str | None]:
        return {"severity": self.severity, "role": self.role, "alias": self.alias, "message": self.message}


def _model_matches(model: str, prefixes: Sequence[str]) -> bool:
    tag = normalize_model_tag(model).lower().rsplit("/", 1)[-1]
    return any(tag.startswith(p.lower()) for p in prefixes)


def check_architecture(models: ModelsConfig) -> list[ProfileIssue]:
    """Validate the configured profiles against the fixed model architecture (8.2–8.7)."""
    issues: list[ProfileIssue] = []
    aliases = [p.alias for p in models.profiles]
    for dup in sorted({a for a in aliases if aliases.count(a) > 1}):
        issues.append(ProfileIssue("error", "-", dup, "duplicate model alias"))
    for alias in aliases:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", alias):
            issues.append(ProfileIssue("error", "-", alias, "alias must match [A-Za-z0-9][A-Za-z0-9._-]{0,99}"))
    for req in ARCHITECTURE:
        enabled = [p for p in models.profiles if p.role == req.role and p.enabled]
        if not enabled:
            if req.required:
                issues.append(ProfileIssue("error", req.role, None, f"no enabled profile for role '{req.role}' ({req.step})"))
            continue
        for p in enabled:
            if not _model_matches(p.model, req.model_prefixes):
                issues.append(
                    ProfileIssue(
                        "error",
                        req.role,
                        p.alias,
                        f"model '{p.model}' violates the fixed architecture (expected {' / '.join(req.model_prefixes)}*)",
                    )
                )
            if p.kind != req.kind:
                issues.append(ProfileIssue("error", req.role, p.alias, f"kind must be '{req.kind}', got '{p.kind}'"))
            if req.min_context is not None and p.context_tokens < req.min_context:
                issues.append(ProfileIssue("warning", req.role, p.alias, f"context {p.context_tokens} below target {req.min_context}"))
            if req.max_context is not None and p.context_tokens > req.max_context:
                issues.append(ProfileIssue("warning", req.role, p.alias, f"context {p.context_tokens} above target {req.max_context}"))
            if req.min_output is not None and p.max_output_tokens < req.min_output:
                issues.append(ProfileIssue("warning", req.role, p.alias, f"max_output_tokens {p.max_output_tokens} below {req.min_output}"))
            if req.max_output is not None and p.max_output_tokens > req.max_output:
                issues.append(ProfileIssue("warning", req.role, p.alias, f"max_output_tokens {p.max_output_tokens} above {req.max_output}"))
            if p.kind == "chat" and p.max_output_tokens >= p.context_tokens:
                issues.append(ProfileIssue("error", req.role, p.alias, "max_output_tokens must be smaller than context_tokens"))
            if p.kind == "embedding" and not p.embedding_dimensions:
                issues.append(ProfileIssue("error", req.role, p.alias, "embedding profile needs embedding_dimensions"))
    planners = [p.alias for p in models.profiles if p.role == "planner" and p.enabled]
    for p in models.profiles:
        if p.fallback_for is None:
            continue
        try:
            target = models.by_alias(p.fallback_for)
        except ConfigError:
            issues.append(ProfileIssue("error", p.role, p.alias, f"fallback_for references unknown alias '{p.fallback_for}'"))
            continue
        if target.alias == p.alias:
            issues.append(ProfileIssue("error", p.role, p.alias, "a profile cannot be its own fallback"))
        if target.kind != p.kind:
            issues.append(ProfileIssue("error", p.role, p.alias, "fallback must have the same kind as its primary"))
        if p.role == "planner_fallback" and target.alias not in planners:
            issues.append(ProfileIssue("error", p.role, p.alias, "planner_fallback must be the fallback of the planner profile"))
    fb = [p for p in models.profiles if p.role == "planner_fallback" and p.enabled]
    if fb and not any(p.fallback_for in planners for p in fb):
        issues.append(
            ProfileIssue("error", "planner_fallback", fb[0].alias, "planner_fallback has no fallback_for pointing at the planner")
        )
    return issues


def assert_architecture(models: ModelsConfig) -> list[ProfileIssue]:
    """Raise ``ConfigError(MODEL_ARCHITECTURE_VIOLATION)`` on error-level issues; return the warnings."""
    issues = check_architecture(models)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise ConfigError(
            "model profiles violate the fixed architecture: " + "; ".join(f"{i.alias or i.role}: {i.message}" for i in errors),
            code="MODEL_ARCHITECTURE_VIOLATION",
            details={"issues": [i.as_dict() for i in issues]},
        )
    return issues


# ----------------------------------------------------------------------------------------------- registry
class ProfileRegistry:
    """Read-only lookup over the configured profiles (by alias, role, model tag, resource group)."""

    def __init__(self, models: ModelsConfig) -> None:
        self.models = models
        self._by_alias = {p.alias: p for p in models.profiles}

    def __contains__(self, alias: object) -> bool:
        return alias in self._by_alias

    def __iter__(self) -> Iterator[ModelProfileConfig]:
        return iter(self.models.profiles)

    def get(self, alias: str) -> ModelProfileConfig:
        profile = self._by_alias.get(alias)
        if profile is None:
            raise ConfigError(f"unknown model alias '{alias}'", code="MODEL_ALIAS_UNKNOWN")
        if not profile.enabled:
            raise ConfigError(f"model alias '{alias}' is disabled", code="MODEL_ALIAS_DISABLED")
        return profile

    def by_role(self, role: str) -> ModelProfileConfig:
        for p in self.models.profiles:
            if p.role == role and p.enabled:
                return p
        raise ConfigError(f"no enabled model profile for role '{role}'", code="MODEL_ROLE_UNKNOWN")

    def fallback_for(self, alias: str) -> ModelProfileConfig | None:
        return self.models.fallback_for(alias)

    def by_model(self, model: str, *, host: str | None = None) -> list[ModelProfileConfig]:
        return [p for p in self.models.profiles if same_model(p.model, model) and (host is None or p.host == host)]

    def group_members(self, resource_group: str, *, host: str | None = None) -> list[ModelProfileConfig]:
        return [p for p in self.models.profiles if p.resource_group == resource_group and (host is None or p.host == host)]

    def enabled(self, *, kind: str | None = None) -> list[ModelProfileConfig]:
        return [p for p in self.models.profiles if p.enabled and (kind is None or p.kind == kind)]

    def embedding_profile(self) -> ModelProfileConfig | None:
        for p in self.models.profiles:
            if p.kind == "embedding" and p.enabled:
                return p
        return None

    def hosts(self) -> list[str]:
        return sorted({p.host for p in self.models.profiles if p.enabled})


# ----------------------------------------------------------------------------------------------- hosts
def validate_http_url(url: str, *, what: str = "url") -> str:
    """Accept only absolute http(s) URLs without credentials/query (config-provided endpoints, no SSRF via input)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{what} must be an absolute http(s) URL", code="MODEL_ENDPOINT_INVALID")
    if parts.username or parts.password:
        raise ConfigError(f"{what} must not embed credentials", code="MODEL_ENDPOINT_INVALID")
    if parts.query or parts.fragment:
        raise ConfigError(f"{what} must not contain a query or fragment", code="MODEL_ENDPOINT_INVALID")
    return url.rstrip("/")


def ollama_base_url(hosts: HostsConfig, host_id: str) -> str:
    """Ollama endpoint of a model host: the ``ollama`` service probe of ``hosts.yaml`` (default port 11434)."""
    host = hosts.by_id(host_id)
    port = next((s.port for s in host.services if s.name == "ollama"), DEFAULT_OLLAMA_PORT)
    return validate_http_url(f"http://{host.address}:{port}", what=f"ollama endpoint of {host_id}")


def ollama_urls(hosts: HostsConfig, models: ModelsConfig) -> dict[str, str]:
    return {h: ollama_base_url(hosts, h) for h in ProfileRegistry(models).hosts()}


# ----------------------------------------------------------------------------------------------- DB sync
@dataclass
class SyncReport:
    created: list[str]
    updated: list[str]
    disabled: list[str]

    def as_dict(self) -> dict[str, list[str]]:
        return {"created": self.created, "updated": self.updated, "disabled": self.disabled}


def _row_values(p: ModelProfileConfig) -> dict[str, Any]:
    return {
        "alias": p.alias,
        "role": p.role,
        "model": p.model,
        "kind": p.kind,
        "host_worker_id": p.host,
        "context_tokens": p.context_tokens,
        "max_output_tokens": p.max_output_tokens,
        "resource_group": p.resource_group,
        "exclusive": p.exclusive,
        "priority": p.priority,
        "memory_gb": p.memory_gb,
        "fallback_for": p.fallback_for,
        "enabled": p.enabled,
        "metadata_": {
            "temperature": p.temperature,
            "think": p.think,
            "timeout_seconds": p.timeout_seconds,
            "embedding_dimensions": p.embedding_dimensions,
        },
    }


_COMPARED = (
    "role",
    "model",
    "kind",
    "host_worker_id",
    "context_tokens",
    "max_output_tokens",
    "resource_group",
    "exclusive",
    "priority",
    "memory_gb",
    "fallback_for",
    "enabled",
    "metadata_",
)


async def sync_profiles(session: AsyncSession, models: ModelsConfig) -> SyncReport:
    """Upsert every configured profile into ``model_profiles``; rows no longer configured are disabled (never
    deleted – invocations reference aliases historically). Runs in the caller's transaction; concurrent syncs are
    safe thanks to ``INSERT … ON CONFLICT``."""
    existing = {row.alias: row for row in (await session.execute(select(ModelProfile))).scalars()}
    report = SyncReport(created=[], updated=[], disabled=[])
    configured = set()
    for p in models.profiles:
        configured.add(p.alias)
        values = _row_values(p)
        row = existing.get(p.alias)
        if row is None:
            report.created.append(p.alias)
        elif any(getattr(row, k) != values[k] for k in _COMPARED):
            report.updated.append(p.alias)
        else:
            continue
        db_values = {("metadata" if k == "metadata_" else k): v for k, v in values.items()}
        stmt = pg_insert(ModelProfile).values(**db_values)
        update_cols: dict[str, Any] = {k: stmt.excluded[k] for k in db_values if k != "alias"}
        update_cols["updated_at"] = func.now()
        await session.execute(stmt.on_conflict_do_update(index_elements=["alias"], set_=update_cols))
    for alias, row in existing.items():
        if alias not in configured and row.enabled:
            row.enabled = False
            report.disabled.append(alias)
    await session.flush()
    session.expire_all()
    return report


async def list_profile_rows(session: AsyncSession, *, enabled_only: bool = False) -> list[ModelProfile]:
    stmt = select(ModelProfile).order_by(ModelProfile.alias)
    if enabled_only:
        stmt = stmt.where(ModelProfile.enabled.is_(True))
    return list((await session.execute(stmt)).scalars())


async def get_profile_row(session: AsyncSession, alias: str) -> ModelProfile:
    row = await session.get(ModelProfile, alias)
    if row is None:
        raise NotFoundError(f"model profile '{alias}' not found", code="MODEL_PROFILE_NOT_FOUND")
    return row


async def get_profile_row_by_role(session: AsyncSession, role: str) -> ModelProfile:
    stmt = select(ModelProfile).where(ModelProfile.role == role, ModelProfile.enabled.is_(True)).order_by(ModelProfile.alias).limit(1)
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        raise NotFoundError(f"no enabled model profile for role '{role}'", code="MODEL_PROFILE_NOT_FOUND")
    return row


def profile_from_row(row: ModelProfile) -> ModelProfileConfig:
    """Rebuild a typed profile from a DB row (e.g. for API/UI consumers without config access)."""
    meta = dict(row.metadata_ or {})
    data: dict[str, Any] = {
        "alias": row.alias,
        "role": row.role,
        "model": row.model,
        "kind": row.kind,
        "context_tokens": row.context_tokens,
        "max_output_tokens": row.max_output_tokens,
        "resource_group": row.resource_group,
        "exclusive": row.exclusive,
        "priority": row.priority,
        "memory_gb": row.memory_gb,
        "fallback_for": row.fallback_for,
        "enabled": row.enabled,
    }
    if row.host_worker_id:
        data["host"] = row.host_worker_id
    for key in ("temperature", "think", "timeout_seconds", "embedding_dimensions"):
        if meta.get(key) is not None:
            data[key] = meta[key]
    return ModelProfileConfig.model_validate(data)


def aliases(profiles: Iterable[ModelProfileConfig]) -> list[str]:
    return [p.alias for p in profiles]
