"""BUG-344: the core of a shell command and the file span a reading command prints."""

from __future__ import annotations

import pytest

from hermclaw.tools.shellread import ShellRead, core_command, shell_read


@pytest.mark.parametrize(
    "command",
    [
        "python -m unittest discover -s tests 2>&1 | tail -80",
        "python -m unittest discover -s tests -v 2>&1 | tail -120",
        "python -m unittest discover -s tests 2>&1 | tail -220; true",
        "echo '=== failures ==='; python -m unittest discover -s tests 2>&1 | grep -A5 FAIL",
    ],
)
def test_slices_of_one_run_share_its_core(command: str) -> None:
    assert core_command(command) == "python -m unittest discover -s tests"


def test_what_selects_or_writes_stays_in_the_core() -> None:
    assert core_command("pytest -k a | tail") != core_command("pytest -k b | tail")
    assert core_command("pytest tests/test_a.py --tb=short | head -50") == "pytest tests/test_a.py"
    assert core_command("python gen.py | tee out.txt | tail -5") == "python gen.py | tee out.txt"
    assert core_command("cd sub && make test 2>/dev/null") == "cd sub ; make test"
    heredoc = "python - <<'PY'\nprint(1)\nPY"
    assert core_command(heredoc) == heredoc, "here-documents are kept as written"
    assert core_command("echo 'unbalanced") == "echo 'unbalanced"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat -n start_widget.ps1", ShellRead("start_widget.ps1")),
        ("sed -n '80,161p' start_widget.ps1", ShellRead("start_widget.ps1", 80, 161)),
        ("sed -n '5p' a.py", ShellRead("a.py", 5, 5)),
        ("sed -n '10,$p' a.py", ShellRead("a.py", 10, None)),
        ("head -n 40 a.py", ShellRead("a.py", 1, 40)),
        ("head a.py", ShellRead("a.py", 1, 10)),
        ("tail -30 a.py", ShellRead("a.py", -30, None)),
        ("tail -n +5 a.py", ShellRead("a.py", 5, None)),
        ("cat a.py | head -20", ShellRead("a.py", 1, 20)),
        ("nl -ba a.py", ShellRead("a.py")),
    ],
)
def test_reading_commands_name_their_span(command: str, expected: ShellRead) -> None:
    assert shell_read(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "cat a.py b.py",
        "grep x a.py",
        "cat -n a.py | tail -5",
        "sed -i 's/a/b/' a.py",
        "cat a.py > b.py",
        "cat a.py 2>/dev/null",
        "cat $(ls)",
    ],
)
def test_other_commands_are_no_reads(command: str) -> None:
    assert shell_read(command) is None
