"""Lessons from fixed mistakes (D-025): which fixes of a job go to the model, and what it sees."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from hermclaw.memory.distill import LessonDraft, build_messages, fix_episodes

T0 = datetime(2026, 10, 23, tzinfo=UTC)


def _attempt(kind: str = "initial", status: str = "failed", correction: dict[str, Any] | None = None, no: int = 1) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        step_id=STEP,
        kind=kind,
        status=status,
        correction_input=correction,
        attempt_no=no,
        started_at=T0 + timedelta(minutes=no),
    )


def _call(attempt: Any, turn: int, tool: str, args: dict[str, Any], *, code: str | None = None, out: str = "ok") -> Any:
    return SimpleNamespace(
        attempt_id=attempt.id,
        turn=turn,
        tool=tool,
        arguments=args,
        status="failed" if code else "succeeded",
        error_code=code,
        result_summary=out,
        started_at=T0 + timedelta(minutes=attempt.attempt_no, seconds=turn),
    )


STEP = uuid.uuid4()
RUN = {"command": "python -m pytest -q"}


def test_a_passing_correction_and_a_red_run_turned_green_are_fixes() -> None:
    first = _attempt()
    fix = _attempt("correction", "completed", {"items": [{"message": "2 of 5 tests failed: test_ids_are_distinct"}]}, no=2)
    failed_fix = _attempt("correction", "failed", {"items": [{"message": "lint"}]}, no=3)
    calls = [
        _call(first, 1, "run_test", RUN, code="TESTS_FAILED", out="FAILED test_env – 401 != 200"),
        _call(first, 2, "read_file", {"path": "app.py"}),
        _call(first, 3, "replace_text", {"path": "app.py", "old": "TOKEN = os.environ", "new": "def token():"}),
        _call(first, 4, "run_test", RUN, out="5 passed"),
        _call(first, 5, "run_test", RUN, code="TESTS_FAILED", out="red again"),
        _call(first, 6, "run_test", RUN, out="green without an edit"),  # no edit in between: no fix
        _call(fix, 1, "write_file", {"path": "store.py", "content": "class Store:\n    next_id = 0\n"}),
        _call(fix, 2, "run_test", RUN, out="5 passed"),
        _call(failed_fix, 1, "write_file", {"path": "x.py", "content": "x"}),
    ]
    episodes = fix_episodes([first, fix, failed_fix], calls, {STEP: "Build the notes API"}, limit=5)
    assert [(e.source, e.attempt_id) for e in episodes] == [("correction", fix.id), ("run", first.id)]
    assert "test_ids_are_distinct" in episodes[0].failure and episodes[0].edits[0].startswith("write_file store.py")
    assert "401 != 200" in episodes[1].failure and episodes[1].edits == (
        "replace_text app.py\n--- old\nTOKEN = os.environ\n+++ new\ndef token():",
    )
    assert len(fix_episodes([first, fix], calls, {}, limit=1)) == 1

    user = build_messages(episodes[1])[1].content
    assert user.startswith("STEP GOAL\nBuild the notes API") and "FAILURE\n" in user and "CHANGES\nreplace_text app.py" in user


def test_a_lesson_needs_general_and_enough_text() -> None:
    good = LessonDraft(
        general=True, title="Read env vars when used", mistake="A module-level read runs at import time.", fix="Read it in the function."
    )
    assert good.usable() and good.content().endswith("Fix: Read it in the function.")
    assert not good.model_copy(update={"general": False}).usable()
    assert not LessonDraft(general=True, title="x", mistake="", fix="").usable()
