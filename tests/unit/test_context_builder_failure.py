"""16.5 error preservation, 16.6 tool summary, 16.7 current diff, 16.9 telemetry payload."""

from __future__ import annotations

import json
import re

from hermclaw.context_builder import (
    HistoryLimits,
    TurnRecord,
    char_cost,
    estimate_tokens,
    first_error_line,
    preserve_failure,
    render_history,
    truncate_middle,
)
from hermclaw.context_builder.diff import render_diff, split_diff
from hermclaw.context_builder.failure import clip_line
from hermclaw.context_builder.report import PAYLOAD_MAX_DROPPED, ContextReport, DroppedItem, SectionReport
from hermclaw.core.redaction import DEFAULT_REDACTOR
from tests.unit.test_context_builder_support import FakeGit, make_builder, make_input

PYTEST_OUT = (
    "============================= test session starts ==============================\n"
    + "".join(f"tests/test_mod.py::test_case_{i} PASSED\n" for i in range(400))
    + "=================================== FAILURES ===================================\n"
    "_________________________________ test_f3 __________________________________\n"
    ">       assert app.f3(1) == 4\n"
    "E       AssertionError: assert 5 == 4\n"
    "E        +  where 5 = <function f3>(1)\n"
    "\n"
    "tests/test_app.py:12: AssertionError\n"
    + "".join(f"captured log line {i}\n" for i in range(400))
    + "=========================== short test summary info ============================\n"
    "FAILED tests/test_app.py::test_f3 - AssertionError: assert 5 == 4\n"
    "======================== 1 failed, 400 passed in 3.21s =========================\n"
)


def test_failure_kept_verbatim_when_it_fits() -> None:
    text, cut = preserve_failure(PYTEST_OUT, char_cost(PYTEST_OUT))
    assert text == PYTEST_OUT and not cut


def test_large_failure_keeps_first_error_and_summary_with_markers() -> None:
    budget = 3000
    text, cut = preserve_failure(PYTEST_OUT, budget)
    assert cut and char_cost(text) <= budget
    assert "E       AssertionError: assert 5 == 4\n" in text
    assert "E        +  where 5 = <function f3>(1)\n" in text  # following detail lines of the first error
    assert "FAILED tests/test_app.py::test_f3 - AssertionError: assert 5 == 4\n" in text
    assert "======================== 1 failed, 400 passed in 3.21s =========================" in text
    assert text.startswith("============================= test session starts")  # head kept
    markers = re.findall(r"\[… (\d+) chars omitted …\]", text)
    assert markers and all(int(m) > 0 for m in markers)
    # every kept line is an original line (verbatim), apart from the markers
    original = set(PYTEST_OUT.splitlines())
    for line in text.splitlines():
        assert line in original or line.startswith("[… ")
    # omitted counts add up
    kept_chars = sum(len(line) + 1 for line in text.splitlines() if not line.startswith("[… "))
    assert kept_chars + sum(int(m) for m in markers) == len(PYTEST_OUT)


def test_first_error_line_detection() -> None:
    assert first_error_line(PYTEST_OUT) == ">       assert app.f3(1) == 4"
    assert first_error_line("compiling\nsrc/a.c:3:1: error: expected ';'\n") == "src/a.c:3:1: error: expected ';'"
    assert first_error_line("all good\n") == "all good"
    assert first_error_line("") == ""


def test_single_huge_line_and_tiny_budgets_stay_within_budget() -> None:
    huge = "Error: " + "x" * 50_000 + "\n" + "tail summary: 1 failed\n"
    text, cut = preserve_failure(huge, 1000)
    assert cut and char_cost(text) <= 1000 and text.startswith("Error: x") and "1 failed" in text
    for budget in (0, 5, 40, 120):
        t, _ = preserve_failure(PYTEST_OUT, budget)
        assert char_cost(t) <= budget
    one_line = "E" * 10_000
    t2, _ = preserve_failure(one_line, 500)
    assert char_cost(t2) <= 500 and "chars omitted" in t2


def test_truncate_middle_and_clip_line() -> None:
    text = "".join(f"{i:05d}\n" for i in range(2000))
    out = truncate_middle(text, 600)
    assert char_cost(out) <= 600 and out.startswith("00000") and out.rstrip().endswith("01999") and "chars omitted" in out
    assert truncate_middle("short", 100) == "short"
    assert clip_line("a\n\nb\n", 100) == "a | b"
    assert clip_line("abcdef", 4) == "abc…"


def test_history_last_turns_full_older_summarised() -> None:
    records = [TurnRecord(i, "read_file", f'{{"path": "f{i}.py"}}', True, f"{i * 10} lines") for i in range(1, 9)]
    records += [
        TurnRecord(9, "write_file", '{"path": "app.py"}', True, "written", mutated_paths=("app.py",)),
        TurnRecord(10, "run_test", '{"command": "pytest -q"}', False, "1 failed, 9 passed", "test_failed"),
        TurnRecord(11, "run_test", '{"command": "pytest -q"}', False, "1 failed, 9 passed", "test_failed"),
        TurnRecord(12, "replace_text", '{"path": "app.py"}', False, "old text not found", "not_found"),
    ]
    h = render_history(list(reversed(records)), 10_000, HistoryLimits(full_turns=4))
    lines = h.body.splitlines()
    assert h.full_turns == 4 and h.summarised_turns == 8 and not h.truncated
    assert lines[0] == "- turns 1-8 (summary): read_file x8 (8 ok)"
    assert [ln.split(":")[0] for ln in lines if ln.startswith("- turn ")] == ["- turn 9", "- turn 10", "- turn 11", "- turn 12"]
    assert '- turn 12: replace_text {"path": "app.py"} -> FAILED [not_found]: old text not found' in lines
    assert "(changed: app.py)" in h.body
    # older failures are counted per tool with error codes and the last failure as key result
    h2 = render_history(records, 10_000, HistoryLimits(full_turns=1))
    assert "run_test x2 (0 ok, 2 failed: test_failed x2)" in h2.body
    assert "last run_test failure: 1 failed, 9 passed" in h2.body
    assert "files changed so far: app.py" in h2.body
    assert "write_file x1 (1 ok)" in h2.body


def test_history_shrinks_to_budget() -> None:
    records = [TurnRecord(i, "run_command", "a" * 3000, i % 3 != 0, "b" * 3000, None if i % 3 else "exit_1") for i in range(1, 60)]
    full = render_history(records, 1_000_000)
    assert not full.truncated and full.full_turns == 6  # digests are always clipped to the per-line limits
    assert char_cost(full.body) < 6000
    for budget in (3000, 1500, 400, 60):
        h = render_history(records, budget)
        assert char_cost(h.body) <= budget and h.truncated
        assert len(h.body) < len(full.body)
    assert render_history([], 100).body == ""


def test_diff_small_files_complete_large_truncated_per_file() -> None:
    small = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    large = "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -1,3000 +1,3000 @@\n" + "+line of code here\n" * 3000
    other = "diff --git a/c.py b/c.py\nnew file mode 100644\n--- /dev/null\n+++ b/c.py\n@@ -0,0 +1 @@\n+z\n"
    pre, files = split_diff(small + large + other)
    assert pre == "" and [f.path for f in files] == ["a.py", "big.py", "c.py"]
    out = render_diff(small + large + other, 2000)
    assert char_cost(out.body) <= 2000 and out.truncated and out.files_shown == 3
    assert small.strip() in out.body and other.strip() in out.body
    assert "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py" in out.body
    assert re.search(r'\[… \d+ chars of the diff of big.py omitted; use git_diff with paths=\["big.py"\] …\]', out.body)
    assert out.body.index("a.py") < out.body.index("big.py") < out.body.index("c.py")  # diff order kept
    fits = render_diff(small, 10_000)
    assert fits.body == small.rstrip("\n") and not fits.truncated
    assert render_diff("", 100).body == ""


def test_diff_with_many_files_lists_the_rest() -> None:
    diff = "".join(f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1 +1 @@\n" + "+x\n" * 300 for i in range(40))
    out = render_diff(diff, 3000, min_file_cost=400)
    assert char_cost(out.body) <= 3000 and out.truncated
    assert 0 < out.files_shown < 40 and out.files_total == 40
    assert re.search(r"\[\+\d+ more changed files not shown: f\d+\.py", out.body)
    assert len(out.dropped) == 40 - out.files_shown
    for budget in (50, 300, 800):
        assert char_cost(render_diff(diff, budget).body) <= budget
    # non-git preamble text only
    assert render_diff("binary blob changed\n", 100).body == "binary blob changed"


async def test_diff_is_requested_with_byte_limit_and_redacted() -> None:
    calls: list[int] = []

    class Git(FakeGit):
        async def diff(self, workspace, paths=None, *, max_bytes=200_000):  # type: ignore[no-untyped-def]
            calls.append(max_bytes)
            return "diff --git a/s.py b/s.py\n--- a/s.py\n+++ b/s.py\n@@ -1 +1 @@\n+API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n"

    builder, _, _ = make_builder(git=Git(), diff_max_bytes=12345)
    built = await builder.build(make_input())
    assert calls == [12345]
    user = built.messages[1].content
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in user and "***REDACTED***" in user


def _report() -> ContextReport:
    return ContextReport(
        turn=2,
        max_turns=20,
        context_tokens=32768,
        max_output_tokens=6144,
        safety_margin_tokens=1639,
        total_budget_tokens=24985,
        framing_tokens=250,
        estimated_prompt_tokens=1000,
        redistributed_tokens=100,
        sections=[SectionReport("RELEVANT CODE", True, 6000, 900, 2880, True, 3, 2)],
        dropped=[DroppedItem("RELEVANT CODE", f"f{i}.py:1-10 token=abcdef123456", "budget") for i in range(100)],
        warnings=["repo.search: RuntimeError: password=hunter2hunter2"],
        fingerprint="abc",
    )


def test_event_payload_is_compact_redacted_and_survives_event_redaction() -> None:
    payload = _report().to_event_payload()
    assert payload["kind"] == "context_report" and payload["unit"] == "tokens"
    assert payload["budget"] == {
        "context_window": 32768,
        "max_output": 6144,
        "safety_margin": 1639,
        "total": 24985,
        "framing": 250,
        "redistributed": 100,
    }
    assert payload["estimated_prompt"] == 1000 and payload["truncated"] is True
    assert len(payload["dropped"]) == PAYLOAD_MAX_DROPPED and payload["dropped_total"] == 100
    blob = json.dumps(payload)
    assert "hunter2" not in blob and "abcdef123456" not in blob
    assert "token" not in "".join(_all_keys(payload))  # the shared event redactor masks keys containing "token"
    assert DEFAULT_REDACTOR.obj(payload) == payload  # nothing is lost when append_event redacts again


def _all_keys(value: object) -> list[str]:
    if isinstance(value, dict):
        return [str(k) for k in value] + [k for v in value.values() for k in _all_keys(v)]
    if isinstance(value, list):
        return [k for v in value for k in _all_keys(v)]
    return []


async def test_report_reflects_built_context() -> None:
    builder, _, _ = make_builder()
    built = await builder.build(make_input(latest_failure=PYTEST_OUT * 20))
    report = built.report
    failure = report.section("LATEST FAILURE")
    assert failure.truncated and failure.present and failure.estimated_tokens <= failure.budget_tokens
    assert report.truncated
    assert report.total_budget_tokens == builder.plan.total_tokens
    assert report.estimated_prompt_tokens == sum(estimate_tokens(m.content) for m in built.messages) + 16
    assert len(report.fingerprint) == 64
    payload = report.to_event_payload()
    assert payload["sections"][0]["name"] == "SYSTEM CONTRACT"
    assert all(isinstance(s["estimated"], int) for s in payload["sections"])


def test_diff_drops_excluded_paths_including_renames() -> None:
    diff = (
        "diff --git a/.env b/.env\nnew file mode 100644\nBinary files /dev/null and b/.env differ\n"
        "diff --git a/id_rsa b/keys.txt\nsimilarity index 100%\nrename from id_rsa\nrename to keys.txt\n"
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    )

    def exclude(path: str) -> bool:
        return path in (".env", "id_rsa")

    out = render_diff(diff, 5000, exclude=exclude)
    assert ".env" not in out.body and "id_rsa" not in out.body and "keys.txt" not in out.body
    assert "diff --git a/a.py b/a.py" in out.body and out.body.endswith("[changes to 2 excluded path(s) not shown]")
    assert out.dropped == [(".env", "excluded"), ("keys.txt", "excluded")] and out.files_total == 3
    only_excluded = render_diff(diff.split("diff --git a/a.py", maxsplit=1)[0], 5000, exclude=exclude)
    assert only_excluded.body == "[changes to 2 excluded path(s) not shown]"
    assert char_cost(render_diff(diff, 30, exclude=exclude).body) <= 30
