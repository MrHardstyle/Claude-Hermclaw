"""Schema parsing + semantic plan validation (P14 14.3, 14.5, 14.6)."""

from __future__ import annotations

from typing import Any

import pytest

from hermclaw.contracts.plan import PlanContract
from hermclaw.core.config import get_config
from hermclaw.planner.errors import PlanInvalid
from hermclaw.planner.inputs import (
    ContextSnippet,
    PlannerInput,
    PlannerSettings,
    collect_known_paths,
    collect_known_symbols,
    detect_test_command,
    infer_framework,
)
from hermclaw.planner.parsing import extract_json_object, validate_schema
from hermclaw.planner.validation import ValidationContext, ancestors, open_research_requests, semantic_errors
from tests.unit.test_planner_support import FASTAPI, LINUX_ADMIN, PHP, REACT, plan, step


def _ctx(inputs: PlannerInput = FASTAPI.inputs, *, extra: str = "", settings: PlannerSettings | None = None) -> ValidationContext:
    return ValidationContext.build(get_config(), inputs, settings or PlannerSettings(), extra_text=extra)


def _errors(data: dict[str, Any], ctx: ValidationContext | None = None, **kw: Any) -> list[str]:
    return semantic_errors(PlanContract.model_validate(data), ctx or _ctx(), **kw)


def _impl(sid: str = "S001", **extra: Any) -> dict[str, Any]:
    data = step(sid, "implement", "coding", f"Implement change number {sid} in the router.", repo_hints=["app/routers/users.py"])
    data.update(extra)
    return data


# --------------------------------------------------------------------------------------------- parsing (14.3)
def test_extract_json_object_variants() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('Plan follows: {"a": {"b": [1]}} done.') == {"a": {"b": [1]}}
    with pytest.raises(PlanInvalid) as e1:
        extract_json_object("[1, 2]")
    assert e1.value.phase == "parse" and "must be an object" in e1.value.errors[0]
    with pytest.raises(PlanInvalid) as e2:
        extract_json_object('{"a": ')
    assert "invalid JSON" in e2.value.errors[0]
    with pytest.raises(PlanInvalid) as e3:
        extract_json_object("   ", reasoning_chars=500)
    assert "reasoning is never accepted as a plan" in e3.value.errors[0]
    with pytest.raises(PlanInvalid) as e4:
        extract_json_object("")
    assert "reasoning" not in e4.value.errors[0]


def test_validate_schema_reports_located_errors() -> None:
    data = plan("goal ok", [step("S1", "implement", "coding", "tiny")])
    with pytest.raises(PlanInvalid) as info:
        validate_schema(PlanContract, data)
    errors = info.value.errors
    assert info.value.phase == "schema"
    assert any(e.startswith("steps[0](S1).id:") for e in errors)
    assert any(e.startswith("steps[0](S1).goal:") for e in errors)


def test_validate_schema_dag_errors() -> None:
    cyc = plan("goal", [_impl("S001", depends_on=["S002"]), _impl("S002", depends_on=["S001"])])
    with pytest.raises(PlanInvalid) as info:
        validate_schema(PlanContract, cyc)
    assert "cycle" in info.value.errors[0]
    unknown = plan("goal", [_impl("S001", depends_on=["S009"])])
    with pytest.raises(PlanInvalid) as info2:
        validate_schema(PlanContract, unknown)
    assert "unknown step S009" in info2.value.errors[0]


# --------------------------------------------------------------------------------------------- inputs
def test_test_command_detection_shapes() -> None:
    assert detect_test_command(PlannerInput(test_command=" make test ")) == ("make test", "generic")
    assert detect_test_command(PlannerInput(repository_inventory={"test_command": "pytest -q"})) == ("pytest -q", "pytest")
    assert detect_test_command(PlannerInput(repository_inventory={"test_commands": ["", "go test ./..."]})) == ("go test ./...", "go")
    assert detect_test_command(PlannerInput(repository_inventory={"tests": {"command": "npx vitest run"}})) == ("npx vitest run", "npm")
    assert detect_test_command(PlannerInput(repository_inventory={"test_commands": [{"cmd": "cargo test"}]})) == ("cargo test", "cargo")
    assert detect_test_command(PlannerInput(test_command="run.sh", test_framework="phpunit")) == ("run.sh", "phpunit")
    assert detect_test_command(PlannerInput()) == (None, "generic")
    assert infer_framework("python -m unittest discover") == "unittest"
    assert infer_framework("vendor/bin/phpunit") == "phpunit"


def test_known_paths_and_symbols_are_normalised() -> None:
    inputs = PlannerInput(
        known_paths=["./src/a.py", "/etc/passwd", "../up.py", "docs/my file.md"],
        repository_inventory={"files": [{"path": "src\\b.py"}, "src/a.py"], "symbols": [{"name": "Foo"}, "bar", {"x": 1}]},
        retrieved_context=[ContextSnippet(path="lib/c.ts")],
        existing_tests=["tests/test_a.py", "pytest"],
        known_symbols=[" Baz "],
    )
    assert collect_known_paths(inputs) == ["src/a.py", "src/b.py", "lib/c.ts", "tests/test_a.py"]
    assert collect_known_symbols(inputs) == ["Baz", "Foo", "bar"]


def test_context_capability_restriction_and_runtime_kinds() -> None:
    ctx = _ctx(LINUX_ADMIN.inputs)
    assert set(ctx.kind_capability) == {"ssh", "research", "review"}
    full = _ctx()
    assert "plan" not in full.kind_capability and "replan" not in full.kind_capability
    assert full.kind_capability["implement"] == "coding" and full.research_capability == "research"
    assert full.test_command == "pytest -q" and full.test_framework == "pytest"


def test_hint_classification() -> None:
    ctx = _ctx()
    assert ctx.classify_hint("app/routers/users.py") == "path"
    assert ctx.classify_hint("README.md") == "path"
    assert ctx.classify_hint("src/**/*.ts") == "path"
    assert ctx.classify_hint(".github") == "path"
    assert ctx.classify_hint("list_users") == "symbol"
    assert ctx.classify_hint("AuthService::login") == "symbol"
    assert ctx.classify_hint("users module please") == "invalid"
    assert ctx.classify_hint("") == "invalid"


# --------------------------------------------------------------------------------------------- semantic rules
def test_valid_fixture_plans_have_no_semantic_errors() -> None:
    for fixture in (FASTAPI, PHP, REACT, LINUX_ADMIN):
        assert _errors(fixture.answer(), _ctx(fixture.inputs, extra=fixture.goal)) == []


def test_capability_rules() -> None:
    data = plan(
        "goal",
        [
            _impl("S001", capability="python"),
            step("S002", "implement", "testing", "Implement with the testing capability.", repo_hints=["app/models.py"]),
            step("S003", "plan", "planning", "Plan again inside a plan."),
        ],
    )
    errors = _errors(data)
    assert any("S001: unknown capability 'python'" in e for e in errors)
    assert any("S002: kind 'implement' requires capability 'coding', got 'testing'" in e for e in errors)
    assert any("S003: kind 'plan' is reserved for the runtime" in e for e in errors)


def test_capability_not_available_for_job() -> None:
    ctx = _ctx(PlannerInput(capabilities=["research", "review"]))
    errors = _errors(plan("goal", [_impl("S001")]), ctx)
    assert any("capability 'coding' is not available for this job" in e for e in errors)
    assert any("step kind 'implement' is not available" in e for e in errors)


def test_repo_hint_grounding() -> None:
    data = plan(
        "goal",
        [
            _impl("S001", repo_hints=["app/routers/orders.py", "/srv/app/main.py", "../x.py", ".env", "list_users", "OrderService"]),
            _impl("S002", repo_hints=["app/**/*.py", "app/", "users module please"], allowed_new_paths=["../escape.py", "secrets/k.txt"]),
        ],
    )
    errors = _errors(data, _ctx(extra="mentions OrderService? no"))
    joined = "\n".join(errors)
    assert "repo_hint 'app/routers/orders.py' does not exist" in joined
    assert "repo_hint '/srv/app/main.py' must be repository-relative" in joined
    assert "repo_hint '../x.py' must be repository-relative" in joined
    assert "repo_hint '.env' is a forbidden path" in joined
    assert "list_users" not in joined  # symbol seen in retrieved context
    assert "OrderService" not in joined  # symbol seen in extra text (goal/constraints)
    assert "app/**/*.py" not in joined and "'app/'" not in joined  # glob + directory resolve against inventory
    assert "'users module please' is neither" in joined
    assert "allowed_new_paths entry '../escape.py'" in joined
    assert "allowed_new_paths entry 'secrets/k.txt' is a forbidden path" in joined


def test_unknown_symbol_and_paths_without_inventory() -> None:
    errors = _errors(plan("goal", [_impl("S001", repo_hints=["app/routers/users.py", "ImaginaryHelper"])]))
    assert any("symbol 'ImaginaryHelper' does not appear" in e for e in errors)
    # no inventory and no context at all: nothing to ground against -> path/symbol existence is not checked
    ctx = _ctx(PlannerInput())
    assert _errors(plan("goal", [_impl("S001", repo_hints=["any/new.py", "AnySymbol"])]), ctx) == []


def test_duplicate_work_and_unreachable_and_checking_steps() -> None:
    data = plan(
        "goal",
        [
            step("S001", "discover", "discover", "Find the router module and its tests."),
            _impl("S002"),
            {**_impl("S003"), "goal": "Implement change number S002 in the router."},
            step("S004", "review", "review", "Review everything carefully."),
            step("S005", "research", "research", "Research pagination best practices."),
        ],
    )
    errors = _errors(data)
    assert any("steps S002 and S003 describe the same work (kind implement, same goal)" in e for e in errors)
    assert any("S001: discover step is unreachable" in e for e in errors)
    assert any("S005: research step is unreachable" in e for e in errors)
    assert any("S004: review step must depend on the step(s) it checks" in e for e in errors)


def test_research_must_run_before_mutating_work() -> None:
    data = plan(
        "goal",
        [
            _impl("S001"),
            step("S002", "research", "research", "Research follow-up question.", depends_on=["S001"]),
            _impl("S003", depends_on=["S002"], repo_hints=["app/models.py"]),
        ],
    )
    errors = _errors(data)
    assert any("S002: research must run before the work that needs it, but it depends on mutating step(s) S001" in e for e in errors)


def test_max_steps_counts_open_research_requests() -> None:
    settings = PlannerSettings(max_steps=3)
    steps = [_impl(f"S00{i}", repo_hints=["app/main.py"], title=f"impl {i}") for i in range(1, 4)]
    for i, s in enumerate(steps):
        s["goal"] = f"Implement distinct change {i} in main."
    data = plan("goal", steps, research_needed=[{"question": "Which pagination style is standard?"}])
    errors = _errors(data, _ctx(settings=settings))
    assert any("plan has 3 steps plus 1 research request(s); at most 3" in e for e in errors)
    data["research_needed"] = []
    assert _errors(data, _ctx(settings=settings)) == []


def test_research_needed_without_research_capability() -> None:
    ctx = _ctx(PlannerInput(capabilities=["coding"]))
    data = plan("goal", [_impl("S001", repo_hints=[])], research_needed=[{"question": "Is there an official API for this?"}])
    assert any("requires the 'research' capability" in e for e in _errors(data, ctx))


def test_open_research_requests_skip_covered_and_duplicates() -> None:
    data = plan(
        "goal",
        [step("S001", "research", "research", "Research: which lockout window does OWASP recommend?"), _impl("S002", depends_on=["S001"])],
        research_needed=[
            {"question": "Which lockout window does OWASP recommend?"},
            {"question": "Is bcrypt cost 12 still adequate?"},
            {"question": "is BCRYPT cost 12 still adequate"},
        ],
    )
    reqs = open_research_requests(PlanContract.model_validate(data))
    assert [r.question for r in reqs] == ["Is bcrypt cost 12 still adequate?"]


def test_remote_steps_need_substantive_acceptance_and_valid_checks() -> None:
    data = plan(
        "goal",
        [
            step("S001", "ssh", "ssh", "Configure logrotate on the host.", acceptance=[{"type": "scope"}]),
            _impl(
                "S002",
                acceptance=[
                    {"type": "presence", "path_glob": "/abs/path", "pattern": "("},
                    {"type": "command", "command": "sudo systemctl restart nginx", "stdout_pattern": "["},
                    {"type": "test", "command": "git push origin main"},
                    {"type": "schema", "path": "../cfg.yaml"},
                ],
            ),
        ],
    )
    errors = _errors(data, _ctx())
    joined = "\n".join(errors)
    assert "S001: mutating step of kind 'ssh' needs machine-checkable acceptance" in joined
    assert "acceptance[0] (presence): path_glob must be repository-relative" in joined
    assert "acceptance[0] (presence): pattern is not a valid regular expression" in joined
    assert "acceptance[1] (command): command 'sudo systemctl restart nginx' is forbidden" in joined
    assert "acceptance[1] (command): stdout_pattern is not a valid regular expression" in joined
    assert "acceptance[2] (test): command 'git push origin main' is forbidden" in joined
    assert "acceptance[3] (schema): path must be repository-relative" in joined


def test_preserved_steps_are_only_structural() -> None:
    # S001 (preserved, e.g. completed in an earlier version) has an unknown capability -> not re-validated
    data = plan("goal", [_impl("S001", capability="legacy-cap"), _impl("S002", depends_on=["S001"], repo_hints=["app/models.py"])])
    assert _errors(data, preserved=frozenset({"S001"})) == []
    dup = plan("goal", [_impl("S001"), {**_impl("S002"), "goal": "Implement change number S001 in the router."}])
    errs = _errors(dup, preserved=frozenset({"S001"}))
    assert errs == [
        "steps S001 and S002 describe the same work (kind implement, same goal) (it is already completed; depend on it instead of repeating it)"
    ]


def test_ancestors_are_transitive() -> None:
    data = plan(
        "goal",
        [
            _impl("S001"),
            _impl("S002", depends_on=["S001"], repo_hints=["app/models.py"]),
            step("S003", "review", "review", "Review all of it.", depends_on=["S002"]),
        ],
    )
    anc = ancestors(PlanContract.model_validate(data))
    assert anc == {"S001": set(), "S002": {"S001"}, "S003": {"S001", "S002"}}


def test_error_limit_is_applied() -> None:
    steps = [_impl(f"S{i:03d}", capability="nope", title=f"impl {i}", goal=f"Implement distinct thing number {i}.") for i in range(1, 25)]
    errors = _errors(plan("goal", steps), limit=5)
    assert len(errors) == 5
