"""Context builder configuration (budget profile, section shares and retrieval limits)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any

from hermclaw.context_builder.budget import BudgetPlan, SectionBudgets, plan_budget
from hermclaw.context_builder.history import HistoryLimits
from hermclaw.context_builder.sections import DEFAULT_SYSTEM_CONTRACT, ELASTIC_SECTIONS, SectionName
from hermclaw.context_builder.tokens import char_cost
from hermclaw.core.config import HermclawConfig, ModelProfileConfig, ScopePolicy
from hermclaw.core.errors import ConfigError

_DEFAULT_EXCLUDES: tuple[str, ...] = tuple(ScopePolicy().always_forbidden)


@dataclass(frozen=True)
class ContextBuilderConfig:
    # budget profile (tokens): total = context_tokens - max_output_tokens - max(safety_margin_tokens, fraction)
    context_tokens: int = 32768
    max_output_tokens: int = 6144
    safety_margin_tokens: int = 256
    safety_margin_fraction: float = 0.05
    budgets: SectionBudgets = field(default_factory=SectionBudgets)
    history: HistoryLimits = field(default_factory=HistoryLimits)
    # never put these into a prompt (secrets/keys/.git) – defaults to policies.scope.always_forbidden
    exclude_globs: tuple[str, ...] = _DEFAULT_EXCLUDES
    # retrieval limits (16.3 / 16.8)
    target_head_lines: int = 160
    test_head_lines: int = 120
    max_target_files: int = 8
    max_acceptance_test_files: int = 6
    max_failure_refs: int = 8
    failure_region_lines: int = 20
    search_k: int = 8
    max_hits_per_query: int = 40  # context_for / search hits considered (highest score first)
    max_test_queries: int = 6
    max_code_snippets: int = 24
    max_test_snippets: int = 12
    min_snippet_cost: int = 300
    max_snippet_lines: int = 400
    merge_gap_lines: int = 3
    read_max_chars: int = 12_000
    # diff (16.7)
    diff_max_bytes: int = 200_000
    min_diff_file_cost: int = 400
    # failure / correction (16.5)
    correction_item_chars: int = 800
    max_list_items: int = 40
    # provider access
    provider_timeout_seconds: float = 20.0
    max_concurrency: int = 4
    system_contract: str = DEFAULT_SYSTEM_CONTRACT

    def __post_init__(self) -> None:
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, bool) or not isinstance(v, int | float):
                continue
            if f.name in {"max_output_tokens", "safety_margin_tokens", "safety_margin_fraction"}:
                if v < 0:
                    raise ConfigError(f"context builder: {f.name} must be >= 0")
            elif v <= 0:
                raise ConfigError(f"context builder: {f.name} must be > 0")
        if self.safety_margin_fraction >= 0.5:
            raise ConfigError("context builder: safety_margin_fraction must be < 0.5")
        if not self.system_contract.strip():
            raise ConfigError("context builder: system_contract must not be empty")
        plan = self.plan()  # validates that the window can hold a prompt at all
        system_cost = char_cost(self.system_contract.strip())
        absorbable = sum(plan.section_cost[n] for n in (SectionName.SYSTEM_CONTRACT, *ELASTIC_SECTIONS))
        if system_cost > absorbable:
            raise ConfigError(
                f"context builder: the system contract ({system_cost} chars) does not fit the context window "
                f"(at most {absorbable} chars incl. the relevance budget)"
            )

    def plan(self) -> BudgetPlan:
        return plan_budget(
            context_tokens=self.context_tokens,
            max_output_tokens=self.max_output_tokens,
            safety_margin_tokens=self.safety_margin_tokens,
            safety_margin_fraction=self.safety_margin_fraction,
            budgets=self.budgets,
        )

    def with_overrides(self, **overrides: Any) -> ContextBuilderConfig:
        return replace(self, **overrides)

    @classmethod
    def from_profile(cls, profile: ModelProfileConfig, *, max_output_tokens: int | None = None, **overrides: Any) -> ContextBuilderConfig:
        """Budget from a model profile; ``max_output_tokens`` overrides the profile's (e.g. the coder policy)."""
        if profile.kind != "chat":
            raise ConfigError(f"model profile '{profile.alias}' is not a chat model")
        return cls(
            context_tokens=profile.context_tokens,
            max_output_tokens=profile.max_output_tokens if max_output_tokens is None else max_output_tokens,
            **overrides,
        )

    @classmethod
    def from_config(cls, config: HermclawConfig, *, role: str = "coder", **overrides: Any) -> ContextBuilderConfig:
        """Coder budget: ``models.by_role(role)`` window, ``policies.coder.max_output_tokens`` (the requested output,
        at least the profile's), ``policies.scope.always_forbidden`` as prompt exclusions and
        ``policies.coder.tool_output_chars`` as the per-read cap."""
        profile = config.models.by_role(role)
        out = profile.max_output_tokens
        if role == "coder":
            out = max(out, config.policies.coder.max_output_tokens)
        overrides.setdefault("exclude_globs", tuple(config.policies.scope.always_forbidden))
        overrides.setdefault("read_max_chars", config.policies.coder.tool_output_chars)
        return cls.from_profile(profile, max_output_tokens=out, **overrides)
