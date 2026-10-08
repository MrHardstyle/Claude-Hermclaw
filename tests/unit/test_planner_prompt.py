"""Planner prompt contract: budgets, deterministic line-safe truncation, redaction, system prompts (P14 14.1)."""

from __future__ import annotations

import json

from hermclaw.contracts.acceptance import EVIDENCE_TYPES
from hermclaw.core.config import get_config
from hermclaw.planner.inputs import ContextSnippet, PlannerInput, PlannerSettings
from hermclaw.planner.prompt import (
    PLANNER_INPUT_KEYS,
    TRUNCATION_MARKER,
    PromptBudget,
    build_messages,
    compact_json,
    context_section,
    fit_to_budget,
    planner_system_prompt,
    planner_user_payload,
    repair_message,
    replan_user_payload,
    replanner_system_prompt,
    shrink_json,
    truncate_lines,
    truncate_lines_tail,
)
from hermclaw.planner.validation import ValidationContext
from tests.unit.test_planner_support import FASTAPI

SECRET = "glpat-" + "abcdefghijklmnopqrstuvwxyz"


def _kc() -> dict[str, str]:
    return ValidationContext.build(get_config(), PlannerInput(), PlannerSettings()).kind_capability


# --------------------------------------------------------------------------------------------- truncation
def test_truncate_lines_never_cuts_inside_a_line() -> None:
    text = "".join(f"line {i:04d} " + "x" * (i % 37) + "\n" for i in range(500))
    for limit in (50, 333, 1000, 5000):
        out, dropped = truncate_lines(text, limit)
        assert len(out) <= limit
        body, marker = out.rsplit("\n", 1) if dropped else (out, "")
        kept = body.splitlines(keepends=False) if body else []
        assert kept == text.splitlines()[: len(kept)]
        assert marker == TRUNCATION_MARKER.format(n=dropped)
        assert len(kept) + dropped == 500


def test_truncate_lines_untouched_when_it_fits_and_single_long_line() -> None:
    assert truncate_lines("a\nb\n", 10) == ("a\nb\n", 0)
    out, dropped = truncate_lines("y" * 500, 60)
    assert dropped == 1 and out == TRUNCATION_MARKER.format(n=1)
    assert "y" not in out


def test_truncate_lines_tail_keeps_the_end() -> None:
    text = "\n".join(f"row {i}" for i in range(100)) + "\nFAILED summary"
    out, dropped = truncate_lines_tail(text, 120)
    assert len(out) <= 120 and out.endswith("FAILED summary") and dropped > 0
    assert out.startswith(TRUNCATION_MARKER.format(n=dropped))


def test_shrink_json_is_deterministic_and_fits() -> None:
    value = {"files": [f"src/module_{i}/file_{i}.py" for i in range(3000)], "notes": "n\n" * 5000, "nested": {"k": list(range(500))}}
    a, ta = shrink_json(value, 4000)
    b, tb = shrink_json(value, 4000)
    assert ta and tb and a == b and len(compact_json(a)) <= 4000
    assert shrink_json({"a": 1}, 100) == ({"a": 1}, False)
    stub, truncated = shrink_json({f"key{i}": "v" * 100 for i in range(100)}, 60)
    assert truncated and len(compact_json(stub)) <= 60 and stub["truncated"] is True


def test_context_section_orders_by_score_and_reports_omissions() -> None:
    snippets = [ContextSnippet(path=f"f{i}.py", snippet="code\n" * 50, score=i / 10) for i in range(10)]
    budget = PromptBudget.from_total(6000)
    items, stats = context_section(snippets, budget)
    paths = [i["path"] for i in items if "path" in i]
    assert paths == sorted(paths, key=lambda p: -int(p[1:-3]))  # highest score first
    assert stats["included"] + stats["omitted"] == 10
    if stats["omitted"]:
        assert items[-1]["omitted_items"] == stats["omitted"]


# --------------------------------------------------------------------------------------------- payload
def test_planner_payload_keys_redaction_and_test_command() -> None:
    inputs = FASTAPI.inputs.model_copy(
        update={
            "retrieved_context": [
                *FASTAPI.inputs.retrieved_context,
                ContextSnippet(path="app/settings.py", snippet=f"TOKEN = '{SECRET}'\n"),
            ]
        }
    )
    payload, stats = planner_user_payload(
        job={"id": "j", "title": f"rotate {SECRET}", "goal": f"use password=hunter2xyz and {SECRET}", "repository": SECRET},
        inputs=inputs,
        constraints=["keep API stable", f"token: {SECRET}"],
        capabilities=[{"name": "coding"}],
        risk_policy={"min_risk_by_kind": {"deploy": "medium"}},
        test_command="pytest -q",
        budget=PromptBudget.from_total(60_000),
    )
    assert tuple(payload) == PLANNER_INPUT_KEYS
    text = compact_json(payload)
    assert SECRET not in text and "hunter2xyz" not in text and "***REDACTED***" in text
    assert payload["job"]["title"] == "rotate ***REDACTED***"  # regression: the whole job section is redacted
    assert payload["repository_inventory"]["test_command"] == "pytest -q"
    assert payload["existing_tests"] == ["tests/test_users.py"]
    assert stats["context"]["included"] == 3


def test_fit_to_budget_bounds_huge_inputs_deterministically() -> None:
    profile = get_config().models.by_role("planner")
    settings = PlannerSettings()
    budget = PromptBudget.for_profile(profile, settings, system_chars=4000)
    assert budget.total_chars == int((profile.context_tokens - profile.max_output_tokens) * 3.2 * 0.85) - 4000
    huge = PlannerInput(
        repository_inventory={"files": [f"pkg/m{i}/mod_{i}.py" for i in range(20_000)], "test_command": "pytest"},
        retrieved_context=[
            ContextSnippet(path=f"pkg/m{i}/mod_{i}.py", snippet="def f():\n    pass\n" * 400, score=1.0) for i in range(300)
        ],
        research_summary={"text": "finding\n" * 20_000},
        existing_tests=[f"tests/test_{i}.py" for i in range(5000)],
    )

    def build(b: PromptBudget) -> tuple[dict[str, object], dict[str, object]]:
        return planner_user_payload(
            job={"goal": "g\n" * 50_000},
            inputs=huge,
            constraints=[f"c{i}" for i in range(3000)],
            capabilities=[],
            risk_policy={},
            test_command="pytest",
            budget=b,
        )

    p1, s1 = fit_to_budget(build, budget)
    p2, s2 = fit_to_budget(build, budget)
    assert p1 == p2 and s1 == s2
    assert len(compact_json(p1)) <= budget.total_chars and "over_budget" not in s1
    assert s1["context"]["omitted"] > 0 and s1["inventory_truncated"] is True


def test_scaled_budget_keeps_total_and_shrinks_sections() -> None:
    b = PromptBudget.from_total(100_000)
    s = b.scaled(0.5)
    assert s.total_chars == 100_000 and s.section_total == 50_000 and s.context_chars < b.context_chars


def test_replan_payload_bounds_failure_sections() -> None:
    package = {
        "trigger": {"reason_code": "stagnation", "detail": "", "failed_step": "S002"},
        "current_plan": {"version": 1, "plan": {"steps": [{"id": f"S{i:03d}", "goal": "g" * 3000} for i in range(1, 60)]}},
        "completed_steps": [{"id": f"S{i:03d}", "summary": "s" * 500} for i in range(1, 40)],
        "failed_step": {"id": "S002", "goal": "x" * 100},
        "deterministic_evidence": {"tests": [{"output_tail": "t\n" * 50_000}]},
        "open_steps": [],
        "research_evidence": [],
    }
    budget = PromptBudget.from_total(40_000)
    payload, stats = replan_user_payload(
        job={"id": "j", "title": "t", "goal": "Original goal"},
        inputs=FASTAPI.inputs,
        constraints=[],
        capabilities=[],
        risk_policy={},
        test_command=None,
        package=package,
        budget=budget,
    )
    assert payload["original_goal"] == "Original goal" and "goal" not in payload["job"]
    assert len(compact_json(payload)) <= budget.total_chars
    assert {"current_plan", "deterministic_evidence"} <= set(stats["replan_sections_truncated"])


# --------------------------------------------------------------------------------------------- system prompts / repair
def test_planner_system_prompt_rules() -> None:
    kc = _kc()
    system = planner_system_prompt(kc, sorted(set(kc.values())), 30)
    assert "You are Gemma, the PLANNER" in system
    assert "You never write code" in system
    assert "implement->coding" in system and "research->research" in system
    assert "plan->" not in system and "replan->" not in system  # runtime-only kinds are not offered
    for ev in EVIDENCE_TYPES:
        assert ev in system
    assert "Never invent paths" in system and "At most 30 steps" in system
    assert "Never plan git commits" in system
    assert "Answer with exactly ONE JSON object" in system


def test_replanner_system_prompt_adds_rerun_rules() -> None:
    kc = _kc()
    system = replanner_system_prompt(kc, sorted(set(kc.values())), 30)
    assert "REPLANNER" in system and "rerun_reason" in system and "completed_steps are already done" in system


def test_repair_message_lists_every_error() -> None:
    msg = repair_message(["steps[0].id: bad", "step S002: unknown capability 'x'"], 0)
    assert "- steps[0].id: bad\n- step S002: unknown capability 'x'" in msg
    assert msg.endswith("Repair attempts remaining after this one: 0.")


def test_build_messages_is_deterministic() -> None:
    a = build_messages("sys", {"k": [1, 2]}, {"x": 1})
    b = build_messages("sys", {"k": [1, 2]}, {"x": 1})
    assert a.stats == b.stats and a.stats["input_sha256"] == b.stats["input_sha256"]
    assert [m.role for m in a.messages] == ["system", "user"] and json.loads(a.messages[1].content) == {"k": [1, 2]}
    assert a.user_payload_chars == len(a.messages[1].content)
