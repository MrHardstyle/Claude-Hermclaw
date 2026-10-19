"""Schema parsing + semantic plan validation (P14 14.3, 14.5, 14.6)."""

from __future__ import annotations

from typing import Any

import pytest

from hermclaw.contracts.plan import PlanContract, PlanStep
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
from hermclaw.planner.validation import (
    AdviceOnce,
    ValidationContext,
    ancestors,
    new_manifest_notes,
    open_research_requests,
    prune_unreachable_must_change,
    quoted_names,
    semantic_errors,
)
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


def test_research_request_rephrasing_an_existing_research_step_is_covered() -> None:
    """BUG-221: the planner's own research step and its research_needed entry name the same topic in other words."""
    data = plan(
        "goal",
        [
            step(
                "S001",
                "research",
                "research",
                "Recherchiere die aktuelle Proxmox VE REST-API-Dokumentation: Authentifizierung, "
                "Endpunkte für Nodes, VMs, LXC-Container, Storages, CPU/RAM/GPU/Temperaturen.",
            ),
            step(
                "S002",
                "research",
                "research",
                "Recherchiere die aktuelle Unraid GraphQL-API-Dokumentation: API-Key, Schema für "
                "VMs, Docker-Container, Array/Cache/Pools, CPU/RAM/GPU/Temperaturen.",
            ),
            _impl("S003", depends_on=["S001", "S002"]),
        ],
        research_needed=[
            {
                "question": "Was sind die exakten Endpunkte und Antwortformate der Proxmox VE REST-API für VMs, LXC, Storages, "
                "CPU/RAM/GPU/Temperaturen?"
            },
            {
                "question": "Was sind die exakten GraphQL-Queries und Antwortformate der Unraid-API für VMs, Docker-Container, "
                "Array/Cache/Pools, CPU/RAM/GPU/Temperaturen?"
            },
            {"question": "Which TOML parser ships with the Python standard library since which version?"},
        ],
    )
    reqs = open_research_requests(PlanContract.model_validate(data))
    assert [r.question for r in reqs] == ["Which TOML parser ships with the Python standard library since which version?"]


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


# --------------------------------------------------------------------------------------------- grounding sources
def test_paths_anywhere_in_the_inventory_ground_hints() -> None:
    """Regression: the prompt allows paths that appear in repository_inventory, so every inventory section counts
    (not only ``files``/``paths``); prose, URLs, commands, absolute paths and versions do not."""
    inputs = PlannerInput(
        repository_inventory={
            "entrypoints": ["app/main.py"],
            "manifests": {"pyproject.toml": {"tool": "poetry"}},
            "config": [{"path": "config/settings.toml"}],
            "test_command": "pytest -q",
            "homepage": "https://example.org/docs/index.html",
            "notes": "uses the src layout",
            "python": "3.12",
            "system": "/etc/hosts",
            "parent": "../other/repo.py",
        },
        existing_tests=["tests/test_users.py::test_page", "test_users_suite"],
    )
    known = set(collect_known_paths(inputs))
    assert {"app/main.py", "pyproject.toml", "config/settings.toml", "tests/test_users.py"} <= known
    assert not known & {"pytest -q", "3.12", "/etc/hosts", "../other/repo.py", "uses the src layout", "test_users_suite"}
    assert not any("://" in k or "example.org" in k for k in known)

    ctx = _ctx(inputs)
    data = plan(
        "Settings",
        [_impl(repo_hints=["config/settings.toml", "pyproject.toml", "app/main.py", "tests/test_users.py"])],
    )
    assert _errors(data, ctx) == []
    invented = plan("Settings", [_impl(repo_hints=["config/other.toml"])])
    assert any("repo_hint 'config/other.toml' does not exist" in e for e in _errors(invented, ctx))


def test_conventional_extensionless_files_are_paths() -> None:
    ctx = _ctx(PlannerInput(repository_inventory={"files": ["Makefile", "docker/Dockerfile"]}))
    assert ctx.classify_hint("Makefile") == "path" and ctx.classify_hint("Dockerfile") == "path"
    assert ctx.classify_hint("list_users") == "symbol"
    errors = _errors(plan("Build", [_impl(repo_hints=["Makefile", "Jenkinsfile"])]), ctx)
    assert errors == [
        "step S001: repo_hint 'Jenkinsfile' does not exist in repository_inventory/retrieved_context "
        "(never invent paths; files to be created belong in allowed_new_paths)"
    ]


def test_a_hint_to_a_file_an_earlier_step_creates_is_valid_once_ordered() -> None:
    """Live G1 (BUG-262): three planner answers named collector.py / docs files in the hints of the step
    that starts or documents the collector – files the collector step creates – and the plan failed with "never invent
    paths". A hint to a file another step lists in allowed_new_paths is valid when that step runs first."""
    ctx = _ctx(PlannerInput(repository_inventory={"files": ["README.md"]}))
    collector = step("S001", "implement", "coding", "Create the collector with tests.", allowed_new_paths=["collector.py", "tests/"])
    scripts = step("S002", "implement", "coding", "Write the start script.", allowed_new_paths=["start.ps1"], repo_hints=["collector.py"])
    ordered = plan("Widget", [collector, {**scripts, "depends_on": ["S001"]}])
    assert not any("collector.py" in e for e in _errors(ordered, ctx)), _errors(ordered, ctx)
    web = step("S003", "implement", "coding", "Create the web page.", allowed_new_paths=["web/"], depends_on=["S001"])
    nested = plan("Widget", [collector, web, {**scripts, "repo_hints": ["web/index.html"], "depends_on": ["S003"]}])
    assert not any("web/index.html" in e for e in _errors(nested, ctx)), _errors(nested, ctx)  # transitive, directory pattern
    unordered = _errors(plan("Widget", [collector, scripts]), ctx)
    assert any("repo_hint 'collector.py' is created by step S001: add S001 to depends_on" in e for e in unordered), unordered
    invented = _errors(plan("Widget", [collector, {**scripts, "repo_hints": ["docs/api.md"], "depends_on": ["S001"]}]), ctx)
    assert any("repo_hint 'docs/api.md' does not exist" in e for e in invented)


def test_a_file_the_goal_names_must_be_among_the_steps_files() -> None:
    """Live G1 (BUG-268): the replanned config step was to "implement the data models in collector.py" with only
    README/config/docs in scope; it worked on collector.py as an adopted leftover and was measured on the failing
    collector tests of later steps until it stagnated. A file the goal names belongs to the step's files."""
    ctx = _ctx(PlannerInput(repository_inventory={"files": ["README.md", "src/collector.py", "config.example.toml"]}))
    config = step(
        "S001",
        "implement",
        "coding",
        "Implement the config class in collector.py and describe it in docs/api.md.",
        repo_hints=["config.example.toml"],
        allowed_new_paths=["docs/api.md"],
    )
    errors = _errors(plan("Widget", [config]), ctx)
    assert any("S001: its goal names src/collector.py, which is not among the step's files" in e for e in errors), errors
    listed = _errors(plan("Widget", [{**config, "repo_hints": ["config.example.toml", "src/collector.py"]}]), ctx)
    assert not any("its goal names" in e for e in listed), listed
    web = step("S002", "implement", "coding", "Create web/app.js that reads the JSON of the collector.", allowed_new_paths=["web/"])
    assert not any("its goal names" in e for e in _errors(plan("Widget", [web]), ctx))  # inside its own directory
    research = step("S003", "research", "research", "Find out how src/collector.py should query Unraid.", network=True)
    assert not any("its goal names" in e for e in _errors(plan("Widget", [research]), ctx))  # only workspace-mutating steps


@pytest.mark.parametrize("pattern", ["**", "*", "**/*", "*/**", "./**"])
def test_catch_all_patterns_are_rejected(pattern: str) -> None:
    """Scope stays explicit: hints and new-path patterns must name a concrete location."""
    errors = _errors(plan("Wide", [_impl(repo_hints=[pattern])]))
    assert any("matches everything" in e and "repo_hint" in e for e in errors)
    errors = _errors(plan("Wide", [_impl(allowed_new_paths=[pattern])]))
    assert any("allowed_new_paths entry" in e and "matches everything" in e for e in errors)
    assert _errors(plan("Narrow", [_impl(repo_hints=["app/**/*.py"], allowed_new_paths=["app/routers/*.py"])])) == []


def test_network_only_for_capabilities_that_allow_it() -> None:
    """Bauplan §30: network is off by default and only granted through the step capability."""
    errors = _errors(plan("Net", [_impl(network=True)]))
    assert any("S001: network access is not allowed for capability 'coding'" in e and "research" in e for e in errors)
    cmd = {"type": "command", "command": "curl -fsS https://example.org/health", "network": True}
    errors = _errors(plan("Net", [_impl(acceptance=[cmd])]))
    assert any("acceptance[0] (command): requests network access" in e for e in errors)
    research = step("S001", "research", "research", "Look up the pagination conventions of the framework.", network=True)
    work = _impl("S002", depends_on=["S001"])
    assert _errors(plan("Net", [research, work])) == []


def test_diff_globs_must_be_repository_relative() -> None:
    diff = {"type": "diff", "must_change": ["../outside.py"], "must_not_change": ["/etc/passwd"]}
    errors = _errors(plan("Diff", [_impl(acceptance=[diff])]))
    assert any("must_change entry '../outside.py' must be repository-relative" in e for e in errors)
    assert any("must_not_change entry '/etc/passwd' must be repository-relative" in e for e in errors)


# --------------------------------------------------------------------------------------------- BUG-143
def test_workspace_steps_without_any_scope_source_are_rejected() -> None:
    """BUG-143 (live test job, README-only repository): Gemma planned implement steps with neither repo_hints nor
    allowed_new_paths. The plan was accepted and the step blocked at run time with SCOPE_UNAVAILABLE; now the
    planner gets the error in its repair turn."""
    diff = {"type": "diff", "must_change": ["*"], "must_not_change": [], "allow_empty": False}
    data = plan(
        "goal",
        [
            step("S001", "discover", "discover", "Identify the programming language and project structure."),
            step("S003", "implement", "coding", "Add a new route /health that returns ok.", depends_on=["S001"], acceptance=[diff]),
            step("S004", "documentation", "documentation", "Document the new health route.", depends_on=["S003"]),
        ],
    )
    errors = _errors(data)
    assert any(e.startswith("step S003: implement step names no files to work on") and "allowed_new_paths" in e for e in errors)
    assert any(e.startswith("step S004: documentation step names no files to work on") for e in errors)
    # any one source is enough: hints, allowed new paths, or presence evidence for a file that does not exist yet
    for extra in (
        {"repo_hints": ["app/main.py"]},
        {"allowed_new_paths": ["app/health.py", "tests/"]},
        {"acceptance": [{"type": "presence", "path_glob": "app/health.py"}]},
    ):
        ok = plan("goal", [step("S001", "implement", "coding", "Add a new route /health that returns ok.", **extra)])
        assert not any("names no files" in e for e in _errors(ok)), extra
    # presence evidence for an existing file creates nothing, and a glob is not a concrete new file
    for glob in ("app/main.py", "app/*.py"):
        bad = plan("goal", [step("S001", "implement", "coding", "Add a new route.", acceptance=[{"type": "presence", "path_glob": glob}])])
        assert any("names no files" in e for e in _errors(bad)), glob
    # read-only and remote kinds build no workspace scope
    remote = plan("goal", [step("S001", "verify", "testing", "Run the test suite."), step("S002", "discover", "discover", "Look around.")])
    assert not any("names no files" in e for e in _errors(remote))


def test_checking_step_error_names_candidate_dependencies() -> None:
    """BUG-143: in the replan of the live job Gemma returned the same plan three times for 'verify step must
    depend on the step(s) it checks'; the error now says which ids to add."""
    data = plan(
        "goal",
        [
            _impl("S007", allowed_new_paths=["app/"]),
            _impl("S008", repo_hints=["app/main.py"]),
            step("S010", "verify", "testing", "Run the newly created tests."),
        ],
    )
    errors = _errors(data)
    assert any(
        e.startswith("step S010: verify step must depend on the step(s) it checks; add the ids of those steps to its depends_on")
        and '"depends_on": ["S007", "S008"]' in e
        for e in errors
    ), errors
    lone = plan("goal", [step("S001", "review", "review", "Review everything carefully.")])
    assert any(e.endswith("add the ids of those steps to its depends_on") for e in _errors(lone))


# --------------------------------------------------------------------------------------------- live-job root causes (2026-10-16)
LIVE_REPO_QUESTIONS = [  # research questions of the live jobs rc1-rc8 (research_runs.question)
    "What is the programming language and web framework used in this repository?",
    "Determine a suitable web framework and language to implement the health endpoint since the repository is empty.",
    "What is the most efficient way to scaffold a minimal web server for a single endpoint in this environment?",
    "What is the preferred web framework and language for this project?",
    "What is the intended tech stack for this repository?",
    "Research how to add a simple JSON health endpoint and a corresponding test in the identified framework.",
    "What are the most popular web frameworks in Python and Node.js for building REST APIs?",
    "Add JSON health endpoint in [framework] with test",
]


@pytest.mark.parametrize("question", LIVE_REPO_QUESTIONS)
def test_research_about_the_repository_or_a_technology_choice_is_rejected(question: str) -> None:
    """U1: research searches the web; questions about the repository itself or about which technology to use
    returned other people's projects and popularity rankings and steered every live job into frameworks."""
    as_request = plan("goal", [_impl("S001")], research_needed=[{"question": question}])
    assert any("cannot be researched on the web" in e for e in _errors(as_request))
    as_step = plan("goal", [step("S001", "research", "research", question), _impl("S002", depends_on=["S001"])])
    assert any("S001: research step" in e and "cannot be answered by web research" in e for e in _errors(as_step))


@pytest.mark.parametrize(
    "question",
    [
        "Which lockout window does OWASP recommend?",
        "How do I configure CORS in Flask 3.1?",
        "What does the PostgreSQL 17 error 'could not serialize access' mean?",
        "Which options does the nginx proxy_buffering directive accept?",
    ],
)
def test_external_research_questions_stay_allowed(question: str) -> None:
    data = plan("goal", [step("S001", "research", "research", question), _impl("S002", depends_on=["S001"])])
    assert not any("web research" in e for e in _errors(data))


def test_tests_for_new_code_belong_in_the_same_step() -> None:
    """U3 (every live first plan): "implement code" -> "implement test" as separate steps. The implementing step
    could not run its tests, the test step could not fix the code, and handoffs stagnated."""
    code = step("S001", "implement", "coding", "Create the service module.", allowed_new_paths=["app/service.py"])
    tests = step("S002", "implement", "coding", "Write tests for the service.", allowed_new_paths=["tests/"], depends_on=["S001"])
    errors = _errors(plan("goal", [code, tests]))
    assert any("S002 only writes tests for the code of step S001" in e and "ONE step" in e for e in errors)
    merged = step("S001", "implement", "coding", "Create the service module with tests.", allowed_new_paths=["app/service.py", "tests/"])
    assert not any("only writes tests" in e for e in _errors(plan("goal", [merged])))
    # tests for code a completed step already delivered (replan) are fine as a step of their own
    assert not any("only writes tests" in e for e in _errors(plan("goal", [code, tests]), preserved=frozenset({"S001"})))
    js = step("S003", "implement", "coding", "Write src/app.test.js.", allowed_new_paths=["src/app.test.js"], depends_on=["S001"])
    assert any("S003 only writes tests" in e for e in _errors(plan("goal", [code, js])))
    # rc22 battery X1: a README among the hints of the test step hid the split (BUG-209)
    readme = step(
        "S004",
        "implement",
        "coding",
        "Write and run tests.",
        allowed_new_paths=["test_service.py"],
        repo_hints=["README.md"],
        depends_on=["S001"],
    )
    assert any("S004 only writes tests" in e for e in _errors(plan("goal", [code, readme])))
    # live G1 (BUG-237): the planner dropped the dependency – tests/test_service.py beside app/service.py is still a split
    apart = step(
        "S005", "implement", "coding", "Write the unit tests.", allowed_new_paths=["tests/test_service.py", "tests/fixtures/a.json"]
    )
    assert any("S005 only writes tests for the code of step S001" in e for e in _errors(plan("goal", [code, apart])))
    unrelated = step("S006", "implement", "coding", "Test the parser.", allowed_new_paths=["tests/test_parser.py"])
    assert not any("S006 only writes tests" in e for e in _errors(plan("goal", [code, unrelated])))
    # live G1 on rc39 (BUG-250): the frontend step also wrote tests/test_service.py for the other step's code
    frontend = step(
        "S007",
        "implement",
        "coding",
        "Create the frontend and the service tests.",
        allowed_new_paths=["web/frontend.js", "tests/test_service.py", "tests/test_frontend.py", "tests/data/x.json"],
    )
    errors = _errors(plan("goal", [code, frontend]))
    assert any("S007 writes tests for code of step S001 (tests/test_service.py)" in e and "move these test paths" in e for e in errors)
    assert not any("test_frontend" in e for e in errors), "tests of the step's own code stay"
    own = step("S007", "implement", "coding", "Create the frontend.", allowed_new_paths=["web/frontend.js", "tests/test_frontend.py"])
    assert not any("writes tests for code" in e for e in _errors(plan("goal", [code, own])))


def test_tests_for_another_steps_code_are_moved_into_that_step() -> None:
    """R-033 planner bench: three of 18 answers kept the tests of S002's code in S001 through every repair turn and the
    planner gave up. The move is mechanical, so the runtime makes it: paths keep their field, ``must_change`` on them
    moves along, tests of the step's own code stay."""
    code = step("S001", "implement", "coding", "Create the service module.", allowed_new_paths=["app/service.py"])
    frontend = step(
        "S002",
        "implement",
        "coding",
        "Create the frontend and the service tests.",
        allowed_new_paths=["web/frontend.js", "tests/test_service.py", "tests/test_frontend.py"],
        acceptance=[{"type": "diff", "must_change": ["web/frontend.js", "tests/test_service.py"]}],
    )
    moved, notes = prune_unreachable_must_change(PlanContract.model_validate(plan("goal", [code, frontend])), _ctx())
    s1, s2 = moved.steps
    assert "tests/test_service.py" in s1.allowed_new_paths and "tests/test_service.py" not in s2.allowed_new_paths
    assert "tests/test_frontend.py" in s2.allowed_new_paths, "tests of the step's own code stay"
    assert [g for c in s2.acceptance if c.type == "diff" for g in c.must_change] == ["web/frontend.js"]
    assert ["tests/test_service.py"] in [c.must_change for c in s1.acceptance if c.type == "diff"]
    assert notes == ["step S002: moved tests tests/test_service.py to step S001, which writes the code they test"]
    assert not any("writes tests for code" in e for e in semantic_errors(moved, _ctx()))
    # a stem named by the code of two steps stays for the planner to decide
    twin = step("S003", "implement", "coding", "Create the service CLI.", allowed_new_paths=["cli/service.py"])
    kept, none = prune_unreachable_must_change(PlanContract.model_validate(plan("goal", [code, twin, frontend])), _ctx())
    assert "tests/test_service.py" in kept.steps[2].allowed_new_paths and not any("moved tests" in n for n in none)
    # preserved steps of a replan are not touched
    same, nothing = prune_unreachable_must_change(
        PlanContract.model_validate(plan("goal", [code, frontend])), _ctx(), preserved=frozenset({"S001"})
    )
    assert "tests/test_service.py" in same.steps[1].allowed_new_paths and nothing == []


def test_a_step_naming_another_steps_component_must_be_ordered_with_it() -> None:
    """Live G1 on rc39 (BUG-251): frontend ("liest Daten von der lokalen JSON-API des Collectors") and start scripts
    ("startet Collector") sat beside the collector step without a dependency; the scripts ran first and guessed config
    format and port. Naming another step's component requires a dependency in either direction."""
    collector = step(
        "S002", "implement", "coding", "Implementiere den Collector mit JSON-API.", allowed_new_paths=["collector.py", "config.toml"]
    )
    frontend = step(
        "S003",
        "implement",
        "coding",
        "Erstelle das Frontend; es liest Daten von der lokalen JSON-API des Collectors.",
        allowed_new_paths=["frontend.html", "frontend.js"],
    )
    scripts = step("S004", "implement", "coding", "Skript, das den Collector startet.", allowed_new_paths=["start_widget.ps1"])
    errors = _errors(plan("goal", [collector, frontend, scripts]))
    assert any("step S003 refers to collector, which step S002 writes" in e and "depends_on" in e for e in errors)
    assert any("step S004 refers to collector, which step S002 writes" in e for e in errors)
    assert not any("refers to config" in e for e in errors), "generic stems name no component"
    ordered = [
        collector,
        step("S003", "implement", "coding", frontend["goal"], allowed_new_paths=["frontend.html", "frontend.js"], depends_on=["S002"]),
        step("S004", "implement", "coding", scripts["goal"], allowed_new_paths=["start_widget.ps1"], depends_on=["S003"]),
    ]
    assert not any("refers to" in e for e in _errors(plan("goal", ordered))), "a transitive dependency orders them"
    # a completed provider (replan history) needs no dependency: its files are in the workspace
    assert not any("refers to" in e for e in _errors(plan("goal", [collector, scripts]), preserved=frozenset({"S002"})))


def test_an_acronym_names_a_short_component() -> None:
    """Live G1 on rc56 (BUG-273): the frontend "fetches data from the local API" that the core step writes in
    collector/api.py; the three-letter stem was ignored, the frontend ran without a dependency and three reviews in a
    row found its config and secret handling at odds with an API it never saw. An upper-case acronym names a short
    component; the ordinary word does not."""
    core = step(
        "S004", "implement", "coding", "Collector-Kern mit lokaler JSON-API.", allowed_new_paths=["collector/api.py", "collector/core.py"]
    )
    web = step("S005", "implement", "coding", "Frontend mit Datenabruf von der lokalen API.", allowed_new_paths=["frontend/app.js"])
    errors = _errors(plan("goal", [core, web]))
    assert any("step S005 refers to api, which step S004 writes" in e for e in errors), errors
    plain = step("S005", "implement", "coding", "Frontend that calls the api of the page.", allowed_new_paths=["frontend/app.js"])
    assert not any("refers to api" in e for e in _errors(plan("goal", [core, plain])))


def test_a_check_only_step_cannot_create_files() -> None:
    """rc22 battery X1: the unit tests were planned into a kind=test step, which only runs checks; the files were never
    written and the implementing step's review demanded them until the step failed (BUG-209)."""
    code = step("S001", "implement", "coding", "Create the analyzer.", allowed_new_paths=["analyzer.py"])
    tests = step("S002", "test", "testing", "Write and run tests.", allowed_new_paths=["test_analyzer.py"], depends_on=["S001"])
    errors = _errors(plan("goal", [code, tests]))
    assert any("S002: kind 'test' only runs checks" in e and "test_analyzer.py" in e for e in errors)
    run_only = step("S002", "test", "testing", "Run the tests.", depends_on=["S001"])
    assert not any("only runs checks" in e for e in _errors(plan("goal", [code, run_only])))


def test_must_change_has_to_match_the_files_of_the_step() -> None:
    """U4: a must_change glob that matches none of the step's files can never pass verification."""
    ok = _impl("S001", allowed_new_paths=["src/**"], acceptance=[{"type": "diff", "must_change": ["*"]}])
    assert not any("must_change" in e for e in _errors(plan("goal", [ok])))
    ok2 = _impl("S001", acceptance=[{"type": "diff", "must_change": ["app/routers/*.py"]}])
    assert not any("must_change" in e for e in _errors(plan("goal", [ok2])))
    bad = _impl("S001", allowed_new_paths=["src/"], acceptance=[{"type": "diff", "must_change": ["lib/util.py"]}])
    assert any("must_change 'lib/util.py' matches none of the files" in e for e in _errors(plan("goal", [bad])))


def test_invented_must_change_files_are_dropped_when_the_step_keeps_a_reachable_one() -> None:
    """BUG-188 (rc16 battery J5): the planner listed src/index.js, src/utils/encoders.js, ... next to the step's own
    files; the plan was rejected three times and the job failed. Unreachable globs are dropped with a note; a step
    whose globs ALL miss keeps the error (a wrong path the planner must fix)."""
    runaway = _impl(
        "S001",
        allowed_new_paths=["src/slugify.js"],
        acceptance=[
            {"type": "diff", "must_change": ["src/slugify.js"]},
            {"type": "diff", "must_change": ["src/utils/encoders.js"]},
            {"type": "diff", "must_change": ["src/index.js", "src/slugify.js"], "must_not_change": ["README.md"]},
            {"type": "presence", "path_glob": "src/slugify.js", "pattern": "slugify"},
        ],
    )
    pruned, notes = prune_unreachable_must_change(PlanContract.model_validate(plan("goal", [runaway])), _ctx())
    diffs = [c for c in pruned.steps[0].acceptance if c.type == "diff"]
    assert [d.must_change for d in diffs] == [["src/slugify.js"], ["src/slugify.js"]]
    assert diffs[1].must_not_change == ["README.md"] and len(pruned.steps[0].acceptance) == 3
    assert notes == ["step S001: dropped must_change outside the step's files: src/utils/encoders.js, src/index.js"]
    assert not any("must_change" in e for e in semantic_errors(pruned, _ctx()))
    wrong = _impl("S001", allowed_new_paths=["src/"], acceptance=[{"type": "diff", "must_change": ["lib/util.py"]}])
    kept, none = prune_unreachable_must_change(PlanContract.model_validate(plan("goal", [wrong])), _ctx())
    assert none == [] and any("matches none of the files" in e for e in semantic_errors(kept, _ctx()))


def test_existing_manifest_may_change_but_is_not_required_to() -> None:
    """rc19 battery J5: Gemma demanded a change to the repository's correct package.json; the coder added a
    description field only to satisfy the criterion and the job counted as adding a dependency (BUG-203)."""
    data = _impl(
        "S001",
        goal="Add the email filter to the users router.",
        repo_hints=["app/routers/users.py", "pyproject.toml"],
        acceptance=[
            {"type": "diff", "must_change": ["pyproject.toml"]},
            {"type": "diff", "must_change": ["app/routers/users.py", "pyproject.toml"], "must_not_change": ["README.md"]},
        ],
    )
    pruned, notes = prune_unreachable_must_change(PlanContract.model_validate(plan("goal", [data])), _ctx())
    diffs = [c for c in pruned.steps[0].acceptance if c.type == "diff"]
    assert [d.must_change for d in diffs] == [["app/routers/users.py"]] and diffs[0].must_not_change == ["README.md"]
    assert notes == ["step S001: pyproject.toml, pyproject.toml may change but is not required to (existing manifest)"]
    assert "pyproject.toml" in pruned.steps[0].repo_hints  # still allowed to change
    # the job names the manifest: the change is what it asks for
    job = ValidationContext.build(get_config(), FASTAPI.inputs, PlannerSettings(), job_text="Add a ruff section to pyproject.toml.")
    named, none = prune_unreachable_must_change(PlanContract.model_validate(plan("goal", [data])), job)
    assert none == [] and named.steps[0].acceptance == PlanContract.model_validate(plan("goal", [data])).steps[0].acceptance


def test_must_change_on_an_existing_file_the_goal_does_not_name_is_dropped() -> None:
    """R-033 blind judging and cycle 1 on rc59 (M2, BUG-278): ``must_change`` on files the step goal never names
    (README, the entry point) failed correct solutions; the worker had no reason to touch them."""
    data = _impl(
        "S001",
        goal="Fix the double counting in the users router and add a test.",
        repo_hints=["app/routers/users.py", "app/main.py", "README.md"],
        allowed_new_paths=["tests/test_double_counting.py"],
        acceptance=[
            {"type": "diff", "must_change": ["app/routers/users.py", "app/main.py", "README.md"]},
            {"type": "diff", "must_change": ["tests/test_double_counting.py", "tests/"]},
        ],
    )
    pruned, notes = prune_unreachable_must_change(PlanContract.model_validate(plan("goal", [data])), _ctx())
    diffs = [c.must_change for c in pruned.steps[0].acceptance if c.type == "diff"]
    assert diffs == [["app/routers/users.py"], ["tests/test_double_counting.py", "tests/"]], "new files and globs stay"
    assert notes == ["step S001: app/main.py, README.md may change but is not required to (named neither in the step goal nor in the job)"]
    assert {"app/main.py", "README.md"} <= set(pruned.steps[0].repo_hints), "still allowed to change"
    # named by its file name in the goal, or by the job: the change is asked for
    named = _impl("S001", goal="Update README.md with the new filter.", acceptance=[{"type": "diff", "must_change": ["README.md"]}])
    kept, none = prune_unreachable_must_change(PlanContract.model_validate(plan("goal", [named])), _ctx())
    assert none == [] and [c.must_change for c in kept.steps[0].acceptance if c.type == "diff"] == [["README.md"]]
    job = ValidationContext.build(get_config(), FASTAPI.inputs, PlannerSettings(), job_text="Document the filter in README.md.")
    unnamed = _impl("S001", acceptance=[{"type": "diff", "must_change": ["README.md"]}])
    assert prune_unreachable_must_change(PlanContract.model_validate(plan("goal", [unnamed])), job)[1] == []
    # a generic stem is no name: "the config" does not name app/config.py
    generic = _impl("S001", goal="Read the port from the config.", acceptance=[{"type": "diff", "must_change": ["app/main.py"]}])
    assert prune_unreachable_must_change(PlanContract.model_validate(plan("goal", [generic])), _ctx())[1]


def test_must_not_change_on_the_steps_own_files_is_dropped() -> None:
    """rc19 battery J6: a degenerate answer forbade the step's own files; every attempt and two replans failed on
    'files that must not change were changed' until REPLAN_LIMIT (BUG-204)."""
    data = _impl(
        "S001",
        goal="Add the email filter to the users router and test it.",
        repo_hints=["app/routers/users.py", "tests/test_users.py"],
        acceptance=[
            {"type": "diff", "must_change": ["app/routers/users.py", "tests/test_users.py"]},
            {"type": "diff", "must_not_change": ["app/main.py", "README.md"]},
            {"type": "diff", "must_not_change": ["app/routers/users.py", "tests/test_users.py"]},
            {"type": "diff", "must_not_change": ["tests/*.py", "app/models.py"]},
        ],
    )
    pruned, notes = prune_unreachable_must_change(PlanContract.model_validate(plan("goal", [data])), _ctx())
    diffs = [c for c in pruned.steps[0].acceptance if c.type == "diff"]
    assert [(d.must_change, d.must_not_change) for d in diffs] == [
        (["app/routers/users.py", "tests/test_users.py"], []),
        ([], ["app/main.py", "README.md"]),  # other files stay protected
        ([], ["app/models.py"]),
    ]
    assert notes == ["step S001: dropped must_not_change on the step's own files: app/routers/users.py, tests/test_users.py, tests/*.py"]


# ------------------------------------------------------------------------------ new dependency manifest (BUG-178)
def _health_plan(*new_paths: str) -> PlanContract:
    data = step(
        "S001",
        "implement",
        "coding",
        "Implement a Flask application with a /health endpoint and a test.",
        allowed_new_paths=list(new_paths),
        acceptance=[{"type": "diff", "must_change": list(new_paths)}],
    )
    return PlanContract.model_validate(plan("Health endpoint with a test.", [data]))


def test_a_new_manifest_in_a_repository_without_one_gets_advice_once() -> None:
    """Live battery J1: Gemma planned Flask plus requirements.txt for a job that names no package; every install was
    refused and the job failed. The planner hears once that such packages are refused; the same plan again is fine."""
    empty = ValidationContext.build(get_config(), PlannerInput(), PlannerSettings(), job_text="Erstelle einen Health-Endpunkt mit Test.")
    notes = new_manifest_notes(_health_plan("app.py", "test_app.py", "requirements.txt"), empty)
    assert len(notes) == 1 and "S001" in notes[0] and "requirements.txt" in notes[0] and "standard-library" in notes[0]
    assert new_manifest_notes(_health_plan("app.py", "test_app.py"), empty) == []
    assert new_manifest_notes(_health_plan("/abs/requirements.txt"), empty) == [], "invalid paths are the validator's job"
    advice = AdviceOnce()
    assert advice(notes) == notes and advice(notes) == [], "the same plan after the advice is accepted"
    # the repository already has the manifest, or the job itself names it: nothing to advise
    has_manifest = ValidationContext.build(get_config(), PlannerInput(known_paths=["requirements.txt", "app.py"]), PlannerSettings())
    assert new_manifest_notes(_health_plan("requirements.txt"), has_manifest) == []
    named = ValidationContext.build(get_config(), PlannerInput(), PlannerSettings(), job_text="Add Flask; list it in requirements.txt.")
    assert new_manifest_notes(_health_plan("requirements.txt"), named) == []
    assert new_manifest_notes(_health_plan("app.py", "requirements.txt"), empty, frozenset({"S001"})) == [], "kept steps"
    # BUG-183 (J4): the job quotes the package the step adds (spelt with _ instead of -): the manifest is wanted
    quoting = ValidationContext.build(
        get_config(), PlannerInput(), PlannerSettings(), job_text='Binde das Python-Paket "acme-sdk-zz9" ein und rufe connect() auf.'
    )
    sdk = step(
        "S001", "implement", "coding", "Add acme_sdk_zz9 to requirements.txt and call connect().", allowed_new_paths=["requirements.txt"]
    )
    assert new_manifest_notes(PlanContract.model_validate(plan("SDK", [sdk])), quoting) == []
    assert len(new_manifest_notes(_health_plan("requirements.txt"), quoting)) == 1, "a package the job does not quote still gets advice"
    assert quoted_names("Nutze „Left-Pad“ und `lodash.merge`, nicht 'x'.") == ["left-pad", "lodash-merge"]


def test_open_steps_without_a_replacement_are_carried_over_with_remapped_dependencies() -> None:
    """BUG-224 (grand job G1): a fallback for the failed collector step S003 must not drop frontend, tests and README."""
    from hermclaw.planner.replan_contract import ReplanStep
    from hermclaw.planner.replanner import carry_over_open_steps

    def ps(sid: str, kind: str, goal: str, deps: list[str], new: list[str] | None = None) -> PlanStep:
        cap = {"implement": "coding", "documentation": "documentation", "test": "testing"}[kind]
        return PlanStep.model_validate(step(sid, kind, cap, goal, depends_on=deps, allowed_new_paths=new or []))

    open_steps = [
        ps("S004", "implement", "Implementiere das Widget-Frontend (index.html, styles.css, app.js).", ["S003"], ["index.html"]),
        ps("S006", "implement", "Implementiere unittest-Tests für Collector und Parser.", ["S003"], ["test_collector.py"]),
        ps("S007", "documentation", "Aktualisiere README.md auf Deutsch: Installation, Bedienung, Autostart.", ["S003", "S004"]),
        ps("S008", "test", "Führe die unittest-Tests aus.", ["S006"]),
        ps("S009", "implement", "Write the collector core with config handling.", ["S001"], ["collector_core.py"]),
    ]
    fallback = ReplanStep.model_validate(
        step(
            "S011",
            "implement",
            "coding",
            "Implementiere den Python-Collector (collector.py) mit TOML-Konfiguration.",
            depends_on=["S001"],
            allowed_new_paths=["collector.py", "collector_core.py"],
        )
    )
    carried, notes = carry_over_open_steps([fallback], open_steps, frozenset({"S001"}))
    by_id = {c.id: c.depends_on for c in carried}
    assert by_id == {"S004": ["S011"], "S006": ["S011"], "S007": ["S011", "S004"], "S008": ["S006"]}  # S009: same created file
    assert len(notes) == 4 and all("carried over" in n for n in notes)
    review = ReplanStep.model_validate(step("S012", "test", "testing", "Run the whole suite once more.", depends_on=["S011"]))
    carried, _ = carry_over_open_steps([fallback, review], open_steps, frozenset({"S001"}))
    assert "S008" not in {c.id for c in carried}  # a new test step covers the open one


def test_every_literal_of_the_job_is_named_by_a_step() -> None:
    """Traceability: first plans of J2 and X3 left out status 400 and inventory.db – all such plans needed replans."""
    from hermclaw.planner.validation import job_literals

    text = "Entwickle eine REST-API (Python-Standardbibliothek, wie Node.js). Ohne Token 401. Ungültige Eingaben 400, z.B. "
    text += "Wert 2390.0000000000005; unbekannte Notizen 404. Endpunkte in docs/api.md, Daten aus `notes.db`."
    assert job_literals(text) == ["docs/api.md", "notes.db", "401", "400", "404"]
    assert job_literals("Werte bis 404 Zeilen in data/x.csv") == ["data/x.csv"], "status codes only in an HTTP job"
    ctx = ValidationContext.build(get_config(), FASTAPI.inputs, PlannerSettings(), job_text=text)
    api = _impl(
        "S001",
        goal="Implement the notes API backed by notes.db; answer 401 without a token and 400 on invalid input.",
        allowed_new_paths=["app/notes.py"],
    )
    errors = [e for e in _errors(plan("goal", [api]), ctx) if "the job names" in e]
    assert errors == [
        "the job names 'docs/api.md', '404', but no step mentions it: write each into the goal and an acceptance "
        "criterion of the step that delivers it (a file to read or create, a status code to answer)"
    ]
    docs = step("S002", "documentation", "documentation", "Document every endpoint.", depends_on=["S001"], allowed_new_paths=["api.md"])
    api["acceptance"] = [{"type": "presence", "path_glob": "app/notes.py", "pattern": "404"}]
    assert not [e for e in _errors(plan("goal", [api, docs]), ctx) if "the job names" in e], "a basename and acceptance count"
    assert not [e for e in _errors(plan("goal", [api]), ctx, preserved=frozenset({"S000"})) if "the job names" in e], "replan"


def test_a_replacement_step_keeps_the_requirements_of_the_failed_step() -> None:
    """BUG-285 (K1 on rc60): the replanned goal listed only lint fixes; columns, warnings and README were gone."""
    from hermclaw.planner.replan_contract import ReplanStep
    from hermclaw.planner.replanner import REPLACED_GOAL_PREFIX, carry_replaced_goal

    original = "Implement csv_eval.py: columns name/value, warnings with line numbers, empty and missing file handling."
    failed = PlanStep.model_validate(step("S003", "implement", "coding", original, allowed_new_paths=["csv_eval.py"]))

    def rs(sid: str, kind: str, cap: str, goal: str, **kw: Any) -> ReplanStep:
        return ReplanStep.model_validate(step(sid, kind, cap, goal, **kw))

    fix = rs("S005", "implement", "coding", "Remove the unused header variable (line 39).", repo_hints=["csv_eval.py"])
    docs = rs("S006", "documentation", "documentation", "Update README.md.", repo_hints=["README.md", "csv_eval.py"])
    other = rs("S007", "implement", "coding", "Add a helper module.", allowed_new_paths=["helper.py"])
    done = rs("S001", "implement", "coding", "Scaffold.", allowed_new_paths=["csv_eval.py"])
    out, notes = carry_replaced_goal([fix, docs, other, done], failed, frozenset({"S001"}))
    by_id = {s.id: s.constraints for s in out}
    assert by_id["S005"][0] == REPLACED_GOAL_PREFIX + original
    assert by_id["S006"] == docs.constraints and by_id["S007"] == other.constraints and by_id["S001"] == done.constraints
    assert notes == ["step S005: keeps the requirements of the replaced step S003"]

    # a second replan passes the ORIGINAL goal on, not the fix list of the replacement
    failed2 = PlanStep.model_validate(
        step("S005", "implement", "coding", fix.goal, repo_hints=["csv_eval.py"], constraints=list(by_id["S005"]))
    )
    again = rs("S008", "implement", "coding", "Define header before use.", repo_hints=["./csv_eval.py"])
    out2, _ = carry_replaced_goal([again], failed2, frozenset())
    assert out2[0].constraints == [REPLACED_GOAL_PREFIX + original]
    assert carry_replaced_goal([again], None, frozenset()) == ([again], [])

    # a documentation-only overlap carries nothing
    readme = PlanStep.model_validate(step("S004", "documentation", "documentation", "Write README.md.", allowed_new_paths=["README.md"]))
    assert carry_replaced_goal([docs], readme, frozenset())[1] == []


def test_a_step_creating_more_files_than_one_attempt_can_write_is_split() -> None:
    """BUG-226 (grand job G1): collector, frontend, scripts, tests, fixtures and README – 13 new files – in one step."""
    ctx = _ctx()
    assert ctx.max_new_files_per_step == 8  # derived from policies.coder.max_turns (20)
    files = [f"src/part_{i}.py" for i in range(9)]
    errors = _errors(plan("goal", [_impl(allowed_new_paths=files)]), ctx)
    assert any("creates 9 new files" in e and "split it by component" in e for e in errors)
    assert not any("new files" in e for e in _errors(plan("goal", [_impl(allowed_new_paths=files[:8])]), ctx))
