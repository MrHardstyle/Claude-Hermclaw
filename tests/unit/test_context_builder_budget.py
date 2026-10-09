"""16.2 token budgeting: estimate (D-008), total budget, fixed section shares, redistribution, config."""

from __future__ import annotations

import pytest

from hermclaw.context_builder import (
    ContextBuilderConfig,
    SectionBudgets,
    SectionName,
    char_cost,
    estimate_tokens,
    plan_budget,
    split_elastic,
)
from hermclaw.context_builder.budget import framing_text
from hermclaw.context_builder.tokens import clip_tail_to_cost, clip_to_cost, cost_for_tokens, tokens_for_cost
from hermclaw.core.config import load_config
from hermclaw.core.errors import ConfigError


def test_estimate_is_3_2_chars_per_token_and_exact() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 32) == 10
    assert estimate_tokens("a" * 33) == 11  # ceil
    assert estimate_tokens("a" * 3200) == 1000
    assert tokens_for_cost(16) == 5
    assert cost_for_tokens(5) == 16
    for tokens in range(0, 500):
        assert tokens_for_cost(cost_for_tokens(tokens)) <= tokens


def test_non_ascii_is_costed_conservatively() -> None:
    text = "äöü漢字🙂"
    assert char_cost(text) == 4 * len(text)
    assert estimate_tokens(text) >= len(text)  # at least one token per non-ASCII code point
    assert clip_to_cost(text, 8) == "äö"
    assert clip_tail_to_cost(text, 8) == "字🙂"
    assert clip_to_cost("abcäd", 5) == "abc"
    assert clip_tail_to_cost("abcd", 2) == "cd"
    assert clip_to_cost("abc", 0) == "" and clip_tail_to_cost("abc", 0) == ""


def test_total_budget_is_context_minus_output_minus_margin() -> None:
    plan = plan_budget(
        context_tokens=32768, max_output_tokens=6144, safety_margin_tokens=256, safety_margin_fraction=0.05, budgets=SectionBudgets()
    )
    assert plan.safety_margin_tokens == 1639  # ceil(5 % of 32768) > 256
    assert plan.total_tokens == 32768 - 6144 - 1639
    assert plan.framing_tokens >= estimate_tokens(framing_text())
    assert plan.available_cost == cost_for_tokens(plan.total_tokens - plan.framing_tokens)
    assert sum(plan.section_cost.values()) <= plan.available_cost
    assert set(plan.section_cost) == set(SectionName)
    assert plan.tokens(SectionName.RELEVANT_CODE) == tokens_for_cost(plan.section_cost[SectionName.RELEVANT_CODE])
    # minimum margin wins for small windows
    small = plan_budget(
        context_tokens=4096, max_output_tokens=1024, safety_margin_tokens=512, safety_margin_fraction=0.05, budgets=SectionBudgets()
    )
    assert small.safety_margin_tokens == 512
    assert small.total_tokens == 4096 - 1024 - 512


def test_default_shares_sum_to_at_most_one_and_are_fixed() -> None:
    b = SectionBudgets()
    shares = b.as_mapping()
    assert list(shares) == list(SectionName)
    assert 0.99 <= sum(shares.values()) <= 1.0
    assert all(v > 0 for v in shares.values())
    assert b.fraction(SectionName.RELEVANT_CODE) > b.fraction(SectionName.RELEVANT_TESTS) > 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"relevant_code": 0.9},  # sum > 1
        {"latest_failure": -0.1},
        {"scope": 1.5},
        {"relevant_code": 0.0, "relevant_tests": 0.0},
        {"step_goal": float("nan")},
    ],
)
def test_invalid_section_budgets_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ConfigError):
        SectionBudgets(**kwargs)


def test_window_too_small_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="too small"):
        plan_budget(
            context_tokens=2048, max_output_tokens=2048, safety_margin_tokens=0, safety_margin_fraction=0.0, budgets=SectionBudgets()
        )
    with pytest.raises(ConfigError):
        plan_budget(context_tokens=0, max_output_tokens=0, safety_margin_tokens=0, safety_margin_fraction=0.0, budgets=SectionBudgets())
    with pytest.raises(ConfigError):
        ContextBuilderConfig(context_tokens=1024, max_output_tokens=600)


def test_split_elastic_redistributes_unused_budget() -> None:
    budgets = SectionBudgets()
    plan = plan_budget(context_tokens=16384, max_output_tokens=2048, safety_margin_tokens=256, safety_margin_fraction=0.05, budgets=budgets)
    base = plan.section_cost[SectionName.RELEVANT_CODE] + plan.section_cost[SectionName.RELEVANT_TESTS]
    # nothing used: everything flows to code/tests, split by their shares
    s0 = split_elastic(plan, budgets, {})
    others = sum(c for n, c in plan.section_cost.items() if n not in (SectionName.RELEVANT_CODE, SectionName.RELEVANT_TESTS))
    assert s0.redistributed_cost == others
    assert s0.code_cost + s0.tests_cost == base + others
    assert s0.code_cost > s0.tests_cost
    # every section fully used: no redistribution
    full = {n: c for n, c in plan.section_cost.items()}
    s1 = split_elastic(plan, budgets, full)
    assert s1.redistributed_cost == 0
    assert s1.code_cost + s1.tests_cost == base
    # an overrun (never-truncated system contract) reduces the pool
    over = dict(full)
    over[SectionName.SYSTEM_CONTRACT] += 500
    s2 = split_elastic(plan, budgets, over)
    assert s2.code_cost + s2.tests_cost == base - 500


def test_config_from_example_config_uses_coder_profile_and_policies() -> None:
    cfg = load_config()
    cb = ContextBuilderConfig.from_config(cfg)
    coder = cfg.models.by_role("coder")
    assert cb.context_tokens == coder.context_tokens
    assert cb.max_output_tokens == max(coder.max_output_tokens, cfg.policies.coder.max_output_tokens)
    assert cb.exclude_globs == tuple(cfg.policies.scope.always_forbidden)
    assert cb.read_max_chars == cfg.policies.coder.tool_output_chars
    plan = cb.plan()
    assert plan.total_tokens == cb.context_tokens - cb.max_output_tokens - plan.safety_margin_tokens
    # explicit overrides win
    cb2 = ContextBuilderConfig.from_config(cfg, read_max_chars=500, exclude_globs=("secret/**",))
    assert cb2.read_max_chars == 500 and cb2.exclude_globs == ("secret/**",)
    # profile route, embedding profiles rejected
    planner = cfg.models.by_role("planner")
    assert ContextBuilderConfig.from_profile(planner).context_tokens == planner.context_tokens
    with pytest.raises(ConfigError):
        ContextBuilderConfig.from_profile(cfg.models.by_role("embedding"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_output_tokens": -1},
        {"search_k": 0},
        {"provider_timeout_seconds": 0},
        {"safety_margin_fraction": 0.6},
        {"system_contract": "   "},
        {"system_contract": "x" * 400_000},
    ],
)
def test_invalid_config_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        ContextBuilderConfig(**kwargs)  # type: ignore[arg-type]


def test_with_overrides_revalidates() -> None:
    cfg = ContextBuilderConfig()
    assert cfg.with_overrides(search_k=3).search_k == 3
    with pytest.raises(ConfigError):
        cfg.with_overrides(max_concurrency=0)
