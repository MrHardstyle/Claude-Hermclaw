from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermclaw.core import job_metadata
from hermclaw.tournament.runner import TournamentRunner, _matches, changed_files, export_base, failed_names
from hermclaw.tournament.scoring import RunRecord, render_markdown, score_runs, summarise
from hermclaw.tournament.settings import tournament_of
from hermclaw.tournament.spec import WEIGHTS, load_spec

PLAN = {
    "goal": "fix",
    "summary": "fix",
    "steps": [{"id": "S001", "title": "Fix", "kind": "implement", "capability": "coding", "goal": "Fix the lint finding in a.py."}],
}


def run(model: str, task: str = "A1", **kw: Any) -> RunRecord:
    base: dict[str, Any] = {
        "rep": 1,
        "size": "small",
        "job_id": "j",
        "status": "succeeded",
        "error_code": None,
        "infrastructure": None,
        "duration_minutes": 10.0,
        "timed_out": False,
        "hidden_passed": 4,
        "hidden_failed": 0,
        "hidden_failed_names": [],
        "hidden_error": None,
        "changed_files": ["a.py"],
        "unexpected_changes": [],
        "protected_changes": [],
        "corrections": 0,
        "replans": 0,
        "model_calls": 20,
        "tool_calls": 20,
        "failed_tool_calls": 0,
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "model_seconds": 50.0,
    }
    return RunRecord(model=model, task=task, **{**base, **kw})


def test_settings_only_from_a_valid_reserved_entry() -> None:
    assert job_metadata.TOURNAMENT in job_metadata.RESERVED_KEYS
    meta = {job_metadata.TOURNAMENT: {"plan": PLAN, "coder_alias": "coder-x"}}
    settings = tournament_of(meta)
    assert settings is not None and settings.review is False and settings.replans == 0
    assert tournament_of({}) is None
    assert tournament_of({job_metadata.TOURNAMENT: {"plan": PLAN, "coder_alias": "bad alias!"}}) is None
    assert tournament_of({job_metadata.TOURNAMENT: "x"}) is None


def test_correctness_first_and_speed_only_for_complete_programs() -> None:
    fast_incomplete = run("fast", hidden_passed=3, hidden_failed=1, hidden_failed_names=["test_x"], duration_minutes=2.0, model_calls=5)
    slow_complete = run("slow", duration_minutes=20.0, model_calls=40)
    score_runs([fast_incomplete, slow_complete], WEIGHTS)
    assert fast_incomplete.score["effort"] == 0 and fast_incomplete.score["duration"] == 0 and fast_incomplete.score["first_try"] == 0
    assert fast_incomplete.score["correct"] == 37.5
    # the only complete run is the reference for effort and duration
    assert slow_complete.score["effort"] == 10 and slow_complete.score["duration"] == 10
    assert slow_complete.score["total"] == 100 > fast_incomplete.score["total"]


def test_disqualification_and_infrastructure() -> None:
    protected = run("a", protected_changes=["data/x.csv"])
    insecure = run("b", hidden_passed=5, hidden_failed=1, hidden_failed_names=["_hidden/t.py::test_security_token_required"])
    infra = run("c", infrastructure="job ended on MODEL_UNAVAILABLE")
    corrected = run("d", corrections=2, unexpected_changes=["README.md"])
    score_runs([protected, insecure, infra, corrected], WEIGHTS)
    assert protected.score["total"] == 0 and protected.disqualified and "data/x.csv" in protected.disqualified
    assert insecure.score["total"] == 0 and insecure.disqualified and "security" in insecure.disqualified
    assert infra.score == {} and not infra.scored
    assert corrected.score["first_try"] == 0 and corrected.score["changes"] == 7.5
    rows = summarise([protected, insecure, infra, corrected], ["a", "b", "c", "d"])
    assert rows[0]["model"] == "d" and {r["model"]: r["infrastructure_runs"] for r in rows}["c"] == 1
    assert "| d |" in render_markdown([protected, corrected], ["a", "d"], "T")


def test_a_program_the_hidden_checks_cannot_run_is_knocked_out(tmp_path: Path) -> None:
    """Agreed knock-out rule "unvollständige Programme": Ornith A7 #3 reported success, but its server did not start –
    3 passed, 28 errors in setup. It scored on the completed and changes weights like a working program (BUG-304)."""
    broken = run("ornith", status="succeeded", hidden_passed=3, hidden_failed=28, hidden_errors=28)
    wrong = run("qwen", status="failed", hidden_passed=11, hidden_failed=12, hidden_failed_names=["t"] * 12)
    score_runs([broken, wrong], WEIGHTS)
    assert broken.score["total"] == 0 and broken.disqualified == "incomplete program: 28 of 31 hidden checks could not run it"
    assert wrong.disqualified is None and wrong.score["correct"] > 0  # a running program with wrong results is graded

    out = tmp_path / "out"
    (out / "grading").mkdir(parents=True)
    legacy = {k: v for k, v in broken.as_dict().items() if k not in ("hidden_errors", "score", "disqualified")}
    (out / "runs.jsonl").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    (out / "grading" / "ornith-A1-1-j.txt").write_text("ERROR _hidden/t.py::test_a\n3 passed, 28 errors in 10.43s\n", encoding="utf-8")
    runner = TournamentRunner.__new__(TournamentRunner)
    runner.out, runner.runs_file = out, out / "runs.jsonl"
    [loaded] = runner.load_runs()
    assert loaded.hidden_errors == 28  # recorded before the count was kept: read from the stored grading output


def test_failed_check_names_and_globs() -> None:
    out = "FAILED _hidden/test_a.py::test_one - assert 1 == 2\nnot ok 3 - age 17.5 is rejected\nok 4 - fine\n"
    assert failed_names(out) == ["_hidden/test_a.py::test_one", "age 17.5 is rejected"]
    assert _matches("data/buchungen.csv", ["data/**"]) and _matches("tests/x/y.py", ["tests/*"])
    assert not _matches("ledger/parse.py", ["data/**"])


def test_changed_files_and_the_base_export(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    (repo / "a.py").write_text("A = 1\n")
    (repo / "b.py").write_text("B = 1\n")
    git("init", "-q")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "a.py").write_text("A = 2\n")
    (repo / "new.py").write_text("N = 1\n")
    assert changed_files(repo, base) == ["a.py", "new.py"]
    export_base(repo, base, tmp_path / "base")
    assert (tmp_path / "base" / "a.py").read_text() == "A = 1\n" and not (tmp_path / "base" / "new.py").exists()


def test_spec_loads_plans_hidden_dirs_and_weights(tmp_path: Path) -> None:
    (tmp_path / "hidden" / "A1").mkdir(parents=True)
    task = {
        "id": "A1",
        "title": "t",
        "prompt": "p",
        "base_branch": "tournament/t",
        "plan": PLAN,
        "hidden": {"dir": "A1", "command": "pytest"},
    }
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump({"repository": "r", "models": ["m"], "defaults": {"reps": 2}, "tasks": [task]}))
    spec = load_spec(path)
    assert spec.reps(spec.tasks[0]) == 2 and spec.limit_minutes(spec.tasks[0]) == 20
    path.write_text(yaml.safe_dump({"repository": "r", "models": ["m"], "tasks": [{**task, "hidden": {"dir": "B", "command": "x"}}]}))
    with pytest.raises(ValueError, match="hidden check directory"):
        load_spec(path)
