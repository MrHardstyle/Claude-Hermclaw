"""What a run's output tells the coder about the files it changed outside the step's paths."""

from __future__ import annotations

from types import SimpleNamespace

from hermclaw.tools.engine import TEMP_FILES_HINT, violation_lines


def test_a_run_that_created_files_outside_the_scope_says_where_temporary_files_belong() -> None:
    """BUG-324 (ladder A9 #4 on rc73): the Pester tests wrote test_output.json into the repository; every run was
    refused and reverted, and the coder ran them five times without learning where such files go."""
    created = SimpleNamespace(operation="create", path="test_output.json", reason="outside allowed_new_paths", reverted=True)
    lines = violation_lines([created])
    assert lines == ["[scope] create test_output.json: outside allowed_new_paths (reverted)", TEMP_FILES_HINT]
    assert "$TestDrive" in TEMP_FILES_HINT and "tmp_path" in TEMP_FILES_HINT
    modified = SimpleNamespace(operation="modify", path="README.md", reason="not a target", reverted=False)
    assert violation_lines([modified]) == ["[scope] modify README.md: not a target (NOT reverted)"]
