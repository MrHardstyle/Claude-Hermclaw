"""Isolation probe command for tests that keep failing (BUG-291)."""

from __future__ import annotations

from hermclaw.coder.isolation import isolation_probe

SUMMARY = (
    "FAILED tests/test_app.py::TestHTTPHandler::test_get_tasks - AssertionError: 4 != 2\n"
    "FAILED tests/test_app.py::TestHTTPHandler::test_get_tasks_filtered_open - AssertionError: 2 != 1\n"
    "2 failed, 22 passed in 0.81s\n"
)


def test_the_first_failing_pytest_test_is_run_alone_with_the_coders_launcher() -> None:
    probe = isolation_probe("python -m pytest -q tests/test_app.py 2>&1 | tail -80", SUMMARY)
    assert probe == (
        "tests/test_app.py::TestHTTPHandler::test_get_tasks",
        "python -m pytest -q -p no:cacheprovider tests/test_app.py::TestHTTPHandler::test_get_tasks",
    )
    assert isolation_probe("pytest tests", SUMMARY) == (
        "tests/test_app.py::TestHTTPHandler::test_get_tasks",
        "pytest -q -p no:cacheprovider tests/test_app.py::TestHTTPHandler::test_get_tasks",
    )
    param = "FAILED tests/test_x.py::test_y[a-1] - assert 0\n"
    assert isolation_probe("pytest", param) == (
        "tests/test_x.py::test_y[a-1]",
        "pytest -q -p no:cacheprovider 'tests/test_x.py::test_y[a-1]'",
    )


def test_no_probe_without_pytest_or_when_the_run_was_already_isolated() -> None:
    assert isolation_probe("npm test", SUMMARY) is None
    assert isolation_probe("cd sub && pytest", SUMMARY) is None
    assert isolation_probe("pytest -q", "1 failed\n") is None
    only = "FAILED tests/test_app.py::TestHTTPHandler::test_get_tasks - AssertionError: 4 != 2\n"
    assert isolation_probe("pytest tests/test_app.py::TestHTTPHandler::test_get_tasks", only) is None
