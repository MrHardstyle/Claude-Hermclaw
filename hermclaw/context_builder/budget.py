"""Token budgeting (step 16.2).

``total budget = profile.context_tokens - max_output_tokens - safety margin``. From the total the fixed framing
(headings, separators, response protocol, chat-template overhead) is reserved; the rest is split into *fixed*
per-section budgets (fractions, summing to at most 100 %). Budget a section does not use is redistributed to
RELEVANT CODE and RELEVANT TESTS (the elastic sections). The SYSTEM CONTRACT is never truncated; if it is larger
than its share, the deficit is taken from the elastic sections.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields

from hermclaw.context_builder.sections import (
    CLOSING_RESERVATION,
    ELASTIC_SECTIONS,
    MESSAGE_OVERHEAD_TOKENS,
    RESPONSE_PROTOCOL,
    SECTION_ORDER,
    SECTION_SEPARATOR,
    SectionName,
    heading,
)
from hermclaw.context_builder.tokens import cost_for_tokens, estimate_tokens, tokens_for_cost
from hermclaw.core.errors import ConfigError

MIN_TOTAL_BUDGET_TOKENS = 512


@dataclass(frozen=True)
class SectionBudgets:
    """Fixed share of the available budget per section (fractions of 1.0; the sum must be <= 1.0)."""

    system_contract: float = 0.03
    step_goal: float = 0.05
    scope: float = 0.04
    constraints: float = 0.03
    acceptance: float = 0.05
    current_repo_facts: float = 0.03
    relevant_code: float = 0.25
    relevant_tests: float = 0.10
    current_diff: float = 0.10
    latest_failure: float = 0.12
    short_step_history: float = 0.06
    available_tools: float = 0.11
    completion_conditions: float = 0.03

    def __post_init__(self) -> None:
        total = 0.0
        for f in fields(self):
            v = getattr(self, f.name)
            if not isinstance(v, int | float) or math.isnan(v) or v < 0 or v > 1:
                raise ConfigError(f"section budget {f.name} must be a fraction between 0 and 1, got {v!r}")
            total += float(v)
        if total > 1.0 + 1e-9:
            raise ConfigError(f"section budgets sum to {total:.3f} (> 1.0)")
        if self.relevant_code + self.relevant_tests <= 0:
            raise ConfigError("relevant_code + relevant_tests must receive a positive budget share")

    def fraction(self, name: SectionName) -> float:
        return float(getattr(self, _field_name(name)))

    def as_mapping(self) -> dict[SectionName, float]:
        return {name: self.fraction(name) for name in SECTION_ORDER}


def _field_name(name: SectionName) -> str:
    return name.value.lower().replace(" ", "_")


def framing_text() -> str:
    """Worst-case fixed text around the sections (all headings present + protocol + closing line)."""
    parts = [heading(n) for n in SECTION_ORDER]
    return SECTION_SEPARATOR.join([*parts, RESPONSE_PROTOCOL, CLOSING_RESERVATION]) + SECTION_SEPARATOR * 2


@dataclass(frozen=True)
class BudgetPlan:
    context_tokens: int
    max_output_tokens: int
    safety_margin_tokens: int
    total_tokens: int  # context - output - safety margin: the hard ceiling for the prompt
    framing_tokens: int  # reserved for headings / protocol / chat template
    available_cost: int  # character-equivalents distributable over the sections
    section_cost: dict[SectionName, int]  # fixed per-section budgets (character equivalents)

    def tokens(self, name: SectionName) -> int:
        """Section budget in (estimated) tokens."""
        return tokens_for_cost(self.section_cost[name])


def plan_budget(
    *,
    context_tokens: int,
    max_output_tokens: int,
    safety_margin_tokens: int,
    safety_margin_fraction: float,
    budgets: SectionBudgets,
) -> BudgetPlan:
    if context_tokens <= 0 or max_output_tokens < 0:
        raise ConfigError("context_tokens must be positive and max_output_tokens non-negative")
    margin = max(int(safety_margin_tokens), math.ceil(context_tokens * float(safety_margin_fraction)))
    total = context_tokens - max_output_tokens - margin
    if total < MIN_TOTAL_BUDGET_TOKENS:
        raise ConfigError(
            f"context window too small: {context_tokens} context - {max_output_tokens} output - {margin} safety margin "
            f"= {total} tokens (< {MIN_TOTAL_BUDGET_TOKENS})"
        )
    # +2: each of the two messages is estimated with ceil()
    framing = estimate_tokens(framing_text()) + 2 * MESSAGE_OVERHEAD_TOKENS + 2
    available = cost_for_tokens(total - framing)
    if available <= 0:  # pragma: no cover - guarded by MIN_TOTAL_BUDGET_TOKENS
        raise ConfigError("no budget left after framing")
    section_cost = {name: int(available * budgets.fraction(name)) for name in SECTION_ORDER}
    return BudgetPlan(
        context_tokens=context_tokens,
        max_output_tokens=max_output_tokens,
        safety_margin_tokens=margin,
        total_tokens=total,
        framing_tokens=framing,
        available_cost=available,
        section_cost=section_cost,
    )


@dataclass(frozen=True)
class ElasticSplit:
    code_cost: int
    tests_cost: int
    redistributed_cost: int


def split_elastic(plan: BudgetPlan, budgets: SectionBudgets, used: dict[SectionName, int]) -> ElasticSplit:
    """Budget for RELEVANT CODE / RELEVANT TESTS: base share + unused budget of every other section.

    ``used`` maps every non-elastic section to the cost it actually consumed (absent = 0). A section that
    overran its share (only the never-truncated SYSTEM CONTRACT can) reduces the pool.
    """
    unused = 0
    for name in SECTION_ORDER:
        if name in ELASTIC_SECTIONS:
            continue
        unused += plan.section_cost[name] - used.get(name, 0)
    code_frac, tests_frac = budgets.relevant_code, budgets.relevant_tests
    base_code = plan.section_cost[SectionName.RELEVANT_CODE]
    base_tests = plan.section_cost[SectionName.RELEVANT_TESTS]
    pool = base_code + base_tests + unused
    if pool <= 0:
        return ElasticSplit(0, 0, unused)
    tests = int(pool * tests_frac / (code_frac + tests_frac))
    code = pool - tests
    return ElasticSplit(code_cost=max(0, code), tests_cost=max(0, tests), redistributed_cost=unused)
