"""Requirement coverage of a step's own tests (Research R-035): which files are tests, which tests they hold, and which
requirements no existing test checks."""

from __future__ import annotations

from pathlib import Path

from hermclaw.coder.coverage import CoverageDraft, build_messages, gap_items, gaps_of, is_test_file, names_in, read_tests


def test_test_files_and_names_of_pytest_pester_and_jest() -> None:
    assert all(map(is_test_file, ["tests/test_api.py", "tests/Get-ServiceReport.Tests.ps1", "web/app.test.js", "pkg/x_test.go"]))
    assert not any(map(is_test_file, ["notes_api/__init__.py", "README.md", "docs/api.md"]))
    py = "class T:\n    def test_one(self):\n        pass\n\nasync def test_two():\n    pass\n\ndef helper():\n    pass\n"
    assert names_in(py) == ["test_one", "test_two"]
    ps = "Describe 'Get-ServiceReport' {\n    It 'returns an array for one service' {\n    }\n    It \"gives [] for none\" { }\n}\n"
    assert names_in(ps) == ["Get-ServiceReport", "returns an array for one service", "gives [] for none"]
    js = "test('lists tasks', () => {});\nit(`rejects empty titles`, () => {});\n"
    assert names_in(js) == ["lists tasks", "rejects empty titles"]


def test_a_requirement_is_a_gap_without_an_existing_test(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text("def test_create():\n    pass\n\ndef test_list():\n    pass\n")
    (tmp_path / "tests" / "conftest.py").write_text("import pytest\n")  # no tests: left out
    tests = read_tests(tmp_path, ["tests/test_api.py", "tests/conftest.py", "notes_api/__init__.py", "tests/gone.py"])
    assert list(tests) == ["tests/test_api.py"]
    draft = CoverageDraft.model_validate(
        {
            "requirements": [
                {"requirement": "POST /notes returns 201", "tests": ["tests/test_api.py::test_create"]},
                {"requirement": "GET /notes lists all notes", "tests": ["test_list"]},
                {"requirement": "two notes get distinct ids", "tests": []},
                {"requirement": "unknown id is 404", "tests": ["test_unknown_is_404"]},  # invented name
            ]
        }
    )
    gaps = gaps_of(draft, tests)
    assert gaps == ["two notes get distinct ids", "unknown id is 404"]
    items = gap_items(gaps)
    assert [i.source for i in items] == ["coverage", "coverage"]
    assert items[0].message.startswith("No test of this step checks: two notes get distinct ids.")
    user = build_messages("Notes API", "Write the API", tests)[1].content
    assert "tests/test_api.py::test_create" in user and "def test_list" in user
