import pytest
from pydantic import ValidationError

from hermclaw.contracts import PlanContract, ReviewContract, ScopeContract, enforce_review_invariant
from hermclaw.contracts.acceptance import AbsenceEvidence, AcceptanceList, TestEvidence
from hermclaw.contracts.tools import CoderAction, ToolName


def _plan(steps):
    return {"goal": "build a feature", "steps": steps}


def _step(i, deps=(), kind="implement", **kw):
    return {
        "id": f"S{i:03d}",
        "title": f"step {i}",
        "kind": kind,
        "capability": "coding",
        "goal": "do something useful",
        "depends_on": list(deps),
        **kw,
    }


def test_plan_valid_dag_and_topological_order():
    p = PlanContract.model_validate(_plan([_step(3, ["S001", "S002"]), _step(1), _step(2, ["S001"])]))
    assert p.topological_order() == ["S001", "S002", "S003"]


@pytest.mark.parametrize(
    "steps,msg",
    [
        ([_step(1, ["S002"]), _step(2, ["S001"])], "cycle"),
        ([_step(1, ["S009"])], "unknown step"),
        ([_step(1), _step(1)], "duplicate"),
        ([_step(1, ["S001"])], "itself"),
    ],
)
def test_plan_rejects_invalid_graphs(steps, msg):
    with pytest.raises(ValidationError) as exc:
        PlanContract.model_validate(_plan(steps))
    assert msg in str(exc.value)


def test_plan_rejects_unknown_fields_and_bad_ids():
    with pytest.raises(ValidationError):
        PlanContract.model_validate(_plan([{**_step(1), "secret_field": 1}]))
    with pytest.raises(ValidationError):
        PlanContract.model_validate(_plan([{**_step(1), "id": "step-1"}]))


def test_acceptance_discriminated_union():
    a = AcceptanceList.model_validate(
        {"items": [{"type": "absence", "path_glob": "**/*.py", "pattern": "OLD"}, {"type": "test", "command": "pytest -q"}]}
    )
    assert isinstance(a.items[0], AbsenceEvidence) and a.items[0].expected_matches == 0
    assert isinstance(a.items[1], TestEvidence)
    with pytest.raises(ValidationError):
        AcceptanceList.model_validate({"items": [{"type": "magic"}]})


def test_scope_paths_normalised_and_traversal_rejected():
    s = ScopeContract(target_paths=["./src/a.py", "src/a.py", "./.github/w.yml"])
    assert s.target_paths == ["src/a.py", ".github/w.yml"]
    for bad in ["/etc/passwd", "../x", "a/../../b", ""]:
        with pytest.raises(ValidationError):
            ScopeContract(target_paths=[bad])


def test_review_invariant_major_blocks_pass():
    r = ReviewContract.model_validate({"verdict": "pass", "findings": [{"severity": "major", "summary": "SQL injection risk"}]})
    eff, overridden = enforce_review_invariant(r)
    assert eff.verdict == "fix_required" and overridden
    ok = ReviewContract.model_validate({"verdict": "pass", "findings": [{"severity": "minor", "summary": "naming nit"}]})
    eff2, over2 = enforce_review_invariant(ok)
    assert eff2.verdict == "pass" and not over2


def test_coder_action_requires_known_tool():
    assert CoderAction.model_validate({"tool": "read_file", "args": {"path": "a"}}).tool == ToolName.read_file
    with pytest.raises(ValidationError):
        CoderAction.model_validate({"tool": "rm_rf", "args": {}})
