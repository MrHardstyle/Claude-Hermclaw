"""Deterministic plan enrichment (P14 14.7 risk assignment, 14.8 acceptance generation, 14.9 research requests)."""

from __future__ import annotations

from typing import Any

import pytest

from hermclaw.contracts.common import Risk
from hermclaw.contracts.plan import PlanContract, PlanStep
from hermclaw.core.config import get_config
from hermclaw.core.errors import ValidationFailed
from hermclaw.planner.enrich import (
    RiskPolicy,
    add_research_steps,
    assign_risk,
    enrich_plan,
    generate_acceptance,
    max_risk,
    next_step_id,
    touched_paths,
)
from hermclaw.planner.inputs import PlannerInput, PlannerSettings
from hermclaw.planner.validation import ValidationContext
from tests.unit.test_planner_support import FASTAPI, LINUX_ADMIN, YAML_CONFIG, plan, step

SETTINGS = PlannerSettings()
WIDE = PlannerInput(
    repository_inventory={"files": [f"src/pkg/mod{i}.py" for i in range(8)] + ["src/main.py", "README.md"], "test_command": "pytest"}
)


def _ctx(inputs: PlannerInput = FASTAPI.inputs) -> ValidationContext:
    return ValidationContext.build(get_config(), inputs, SETTINGS)


def _step(**data: Any) -> PlanStep:
    base = step("S001", "implement", "coding", "Implement the change in the router module.")
    base.update(data)
    return PlanStep.model_validate(base)


def _types(step_: PlanStep, ctx: ValidationContext) -> list[str]:
    return [a.type for a in generate_acceptance(step_, ctx)[0]]


# --------------------------------------------------------------------------------------------- acceptance (14.8)
def test_mutating_step_without_acceptance_gets_diff_test_scope_security() -> None:
    ctx = _ctx()
    s = _step(repo_hints=["app/routers/users.py", "list_users", "app/routers"], forbidden_paths=["app/models.py"])
    acceptance, added = generate_acceptance(s, ctx)
    assert added == ["diff", "test", "scope", "security"]
    diff, test = acceptance[0], acceptance[1]
    assert diff.type == "diff" and diff.allow_empty is False
    assert diff.must_change == ["app/routers/users.py", "app/routers/"]  # symbol hint excluded, directory as glob
    assert diff.must_not_change == ["app/models.py"]
    assert test.type == "test" and test.command == "pytest -q" and test.framework == "pytest"


def test_substantive_acceptance_is_kept_and_only_scope_security_added() -> None:
    s = _step(repo_hints=["app/routers/users.py"], acceptance=[{"type": "presence", "path_glob": "app/routers/users.py", "pattern": "x"}])
    acceptance, added = generate_acceptance(s, _ctx())
    assert added == ["scope", "security"] and [a.type for a in acceptance] == ["presence", "scope", "security"]


def test_existing_scope_and_security_are_not_duplicated() -> None:
    s = _step(acceptance=[{"type": "scope"}, {"type": "security"}], repo_hints=["app/main.py"])
    assert _types(s, _ctx()) == ["scope", "security", "diff", "test"]


def test_no_test_evidence_without_detected_test_command_and_new_paths_as_fallback() -> None:
    ctx = _ctx(YAML_CONFIG.inputs)
    assert ctx.test_command is None
    s = _step(allowed_new_paths=["config/new.yaml"])
    acceptance, added = generate_acceptance(s, ctx)
    assert added == ["diff", "scope", "security"]
    assert acceptance[0].type == "diff" and acceptance[0].must_change == ["config/new.yaml"]


def test_documentation_step_is_workspace_mutating() -> None:
    s = PlanStep.model_validate(step("S002", "documentation", "documentation", "Document the API change.", repo_hints=["README.md"]))
    assert _types(s, _ctx()) == ["diff", "test", "scope", "security"]


@pytest.mark.parametrize("kind", ["review", "test", "research", "ssh", "deploy"])
def test_non_workspace_kinds_get_no_generated_acceptance(kind: str) -> None:
    caps = {"review": "review", "test": "testing", "research": "research", "ssh": "ssh", "deploy": "deploy"}
    s = PlanStep.model_validate(step("S001", kind, caps[kind], "A step that does not change the workspace."))
    assert generate_acceptance(s, _ctx()) == ([], [])


# --------------------------------------------------------------------------------------------- risk (14.7)
@pytest.mark.parametrize("kind", ["deploy", "ssh", "database"])
def test_operational_kinds_have_a_medium_floor(kind: str) -> None:
    s = PlanStep.model_validate(step("S001", kind, kind, "Change the production system state."))
    risk, why = assign_risk(s, _ctx(), RiskPolicy.effective({}, SETTINGS))
    assert risk == Risk.medium and why == f"kind {kind} is at least medium"


def test_model_risk_is_never_lowered() -> None:
    s = PlanStep.model_validate(step("S001", "deploy", "deploy", "Deploy the release.", risk="high"))
    assert assign_risk(s, _ctx(), RiskPolicy.effective({}, SETTINGS)) == (Risk.high, None)
    low = _step(risk="medium")
    assert assign_risk(low, _ctx(), RiskPolicy.effective({}, SETTINGS)) == (Risk.medium, None)


def test_untested_implement_step_over_many_explicit_paths_is_high() -> None:
    ctx = _ctx(WIDE)
    hints = [f"src/pkg/mod{i}.py" for i in range(6)]
    presence = [{"type": "presence", "path_glob": "src/pkg/mod0.py", "pattern": "x"}]
    s = _step(repo_hints=hints, acceptance=presence)
    risk, why = assign_risk(s, ctx, RiskPolicy.effective({}, SETTINGS))
    assert risk == Risk.high and why is not None and "without test evidence" in why
    tested = _step(repo_hints=hints, acceptance=[*presence, {"type": "test", "command": "pytest"}])
    assert assign_risk(tested, ctx, RiskPolicy.effective({}, SETTINGS)) == (Risk.low, None)


def test_directory_and_glob_hints_count_the_paths_they_match() -> None:
    """Regression: a single directory/glob hint covering many files used to count as one path."""
    ctx = _ctx(WIDE)
    presence = [{"type": "presence", "path_glob": "src/pkg/mod0.py", "pattern": "x"}]
    for hint in ("src/pkg", "src/pkg/", "src/**/*.py"):
        s = _step(repo_hints=[hint], acceptance=presence)
        assert touched_paths(s, ctx, limit=100) >= 8, hint
        assert assign_risk(s, ctx, RiskPolicy.effective({}, SETTINGS))[0] == Risk.high, hint
    narrow = _step(repo_hints=["src/main.py", "README.md"], allowed_new_paths=["src/new.py"], acceptance=presence)
    assert touched_paths(narrow, ctx, limit=100) == 3
    assert assign_risk(narrow, ctx, RiskPolicy.effective({}, SETTINGS))[0] == Risk.low


def test_touched_paths_is_capped_by_the_limit() -> None:
    s = _step(repo_hints=["src/**/*.py"])
    assert touched_paths(s, _ctx(WIDE), limit=3) == 3


def test_risk_policy_overrides_only_make_it_stricter() -> None:
    policy = RiskPolicy.effective({"min_risk_by_kind": {"deploy": "low", "implement": "medium"}, "many_paths_threshold": 2}, SETTINGS)
    assert policy.min_risk_by_kind["deploy"] == Risk.medium  # cannot be lowered
    assert policy.min_risk_by_kind["implement"] == Risk.medium
    assert policy.many_paths_threshold == 2
    looser = RiskPolicy.effective({"many_paths_threshold": 50, "high_if_untested_many_paths": False}, SETTINGS)
    assert looser.many_paths_threshold == SETTINGS.many_paths_threshold and looser.high_if_untested_many_paths is True
    view = policy.prompt_view()
    assert view["risk_is_never_lowered_by_runtime"] is True and view["min_risk_by_kind"]["implement"] == "medium"


@pytest.mark.parametrize("override", [{"min_risk_by_kind": {"deploy": "extreme"}}, {"min_risk_by_kind": {"rollout": "high"}}, {"x": 1}])
def test_invalid_risk_policy_overrides_are_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationFailed):
        RiskPolicy.effective(override, SETTINGS)


def test_max_risk() -> None:
    assert max_risk(Risk.low, Risk.high) == Risk.high and max_risk(Risk.medium, Risk.low) == Risk.medium


# --------------------------------------------------------------------------------------------- research (14.9)
def test_next_step_id_continues_and_fills_gaps_at_the_end() -> None:
    assert next_step_id(set()) == "S001"
    assert next_step_id({"S001", "S007"}) == "S008"
    assert next_step_id({"S999", "S001"}) == "S002"


def test_research_requests_become_research_steps_before_all_work() -> None:
    data = plan(
        "Rate limit",
        [
            step("S001", "implement", "coding", "Implement the limiter.", repo_hints=["app/main.py"]),
            step("S002", "implement", "coding", "Wire the limiter into routes.", depends_on=["S001"], repo_hints=["app/main.py"]),
            step("S003", "review", "review", "Review the limiter.", depends_on=["S002"]),
        ],
        research_needed=[
            {"question": "Which rate limiting algorithm suits login endpoints?", "reason": "pick an approach"},
            {"question": "Which rate limiting algorithm suits login endpoints?"},  # duplicate
            {"question": "What are common lockout durations?"},
        ],
    )
    ctx = _ctx()
    result = enrich_plan(PlanContract.model_validate(data), ctx, RiskPolicy.effective({}, SETTINGS))
    research = [s for s in result.plan.steps if s.kind.value == "research"]
    assert [s.id for s in research] == ["S004", "S005"] == result.generated_research_steps
    assert all(s.capability == "research" and s.network is True and s.depends_on == [] for s in research)
    assert "Reason: pick an approach" in research[0].goal
    by_id = {s.id: s for s in result.plan.steps}
    assert set(by_id["S001"].depends_on) == {"S004", "S005"}  # the first work step waits for research
    assert by_id["S002"].depends_on == ["S001"]  # later work is ordered transitively
    order = result.plan.topological_order()
    assert order.index("S004") < order.index("S001") and order.index("S005") < order.index("S001")
    assert any("research step S004 generated" in n for n in result.notes)


def test_research_steps_respect_preserved_and_reserved_ids() -> None:
    contract = PlanContract.model_validate(
        plan(
            "Replan",
            [
                step("S001", "implement", "coding", "Completed earlier work.", repo_hints=["app/main.py"]),
                step("S002", "implement", "coding", "New work after the completed step.", depends_on=["S001"], repo_hints=["app/main.py"]),
            ],
            research_needed=[{"question": "Is the dependency still maintained?"}],
        )
    )
    data = contract.model_dump(mode="json")
    created = add_research_steps(data, contract, _ctx(), preserved=frozenset({"S001"}), reserved_ids={"S003", "S004"})
    assert created == ["S005"]
    by_id = {s["id"]: s for s in data["steps"]}
    assert by_id["S001"]["depends_on"] == []  # completed history untouched
    assert by_id["S002"]["depends_on"] == ["S001", "S005"]


def test_no_research_steps_without_research_capability() -> None:
    no_research = PlannerInput(capabilities=["ssh", "review"])
    ctx = _ctx(no_research)
    contract = PlanContract.model_validate(LINUX_ADMIN.answer())
    data = contract.model_dump(mode="json")
    assert add_research_steps(data, contract, ctx, preserved=frozenset(), reserved_ids=set()) == []


def test_preserved_steps_are_not_enriched() -> None:
    contract = PlanContract.model_validate(
        plan(
            "Replan",
            [
                step("S001", "implement", "coding", "Completed earlier work.", repo_hints=["app/main.py"]),
                step("S002", "deploy", "deploy", "Deploy it.", depends_on=["S001"], acceptance=[{"type": "command", "command": "true"}]),
            ],
        )
    )
    result = enrich_plan(contract, _ctx(), RiskPolicy.effective({}, SETTINGS), preserved=frozenset({"S001"}))
    s001, s002 = result.plan.steps
    assert s001.acceptance == [] and s001.risk == Risk.low
    assert s002.risk == Risk.medium and result.risk_raised == {"S002": ("low", "medium")}
