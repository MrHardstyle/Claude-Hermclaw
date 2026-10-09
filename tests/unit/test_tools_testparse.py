"""P17 17.6: test-runner output parsing from recorded outputs (pytest, unittest, jest, vitest, mocha, phpunit, go, cargo)."""

from __future__ import annotations

import pytest

from hermclaw.tools.testparse import parse_counts, summarise

PYTEST_PASS = """\
============================= test session starts ==============================
collected 12 items

tests/test_a.py ............                                             [100%]

============================== 12 passed in 0.42s ==============================
"""
PYTEST_MIXED = """\
FAILED tests/test_a.py::test_x - AssertionError: assert 1 == 2
ERROR tests/test_b.py::test_y - fixture 'db' not found
=========== 2 failed, 7 passed, 3 skipped, 1 xfailed, 1 error, 2 warnings in 3.10s ===========
"""
PYTEST_QUIET = "..F.\n1 failed, 3 passed in 0.12s\n"
PYTEST_NONE = "collected 0 items\n\n============================ no tests ran in 0.01s =============================\n"
PYTEST_COLOR = "\x1b[32m\x1b[1m5 passed\x1b[0m\x1b[32m in 0.10s\x1b[0m\n"
UNITTEST_OK = "....\n----------------------------------------------------------------------\nRan 4 tests in 0.002s\n\nOK\n"
UNITTEST_FAIL = (
    "F.E.s\n======\nFAIL: test_x\n----------------------------------------------------------------------\n"
    "Ran 5 tests in 0.010s\n\nFAILED (failures=1, errors=1, skipped=1)\n"
)
JEST = """\
Test Suites: 1 failed, 2 passed, 3 total
Tests:       1 failed, 2 skipped, 10 passed, 13 total
Snapshots:   0 total
Time:        2.345 s
"""
VITEST = """\
 ✓ src/a.test.ts (3 tests) 5ms
 ❯ src/b.test.ts (2 tests | 1 failed) 7ms

 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 4 passed | 1 skipped (6)
   Start at  10:00:00
"""
MOCHA = "\n  7 passing (32ms)\n  1 pending\n  2 failing\n\n  1) suite x:\n     AssertionError\n"
PHPUNIT_OK = (
    "PHPUnit 10.5.0 by Sebastian Bergmann.\n\n.....  5 / 5 (100%)\n\nTime: 00:00.010, Memory: 6.00 MB\n\nOK (5 tests, 9 assertions)\n"
)
PHPUNIT_FAIL = "FAILURES!\nTests: 10, Assertions: 20, Failures: 2, Errors: 1, Skipped: 1.\n"
GO = """\
=== RUN   TestAdd
--- PASS: TestAdd (0.00s)
=== RUN   TestSub
--- FAIL: TestSub (0.00s)
    calc_test.go:12: got 1 want 2
=== RUN   TestSkip
--- SKIP: TestSkip (0.00s)
FAIL
FAIL\texample.com/calc\t0.005s
ok  \texample.com/util\t0.002s
"""
GO_PKGS = "ok  \texample.com/a\t0.010s\nok  \texample.com/b\t(cached)\n"
GO_BUILD = "# example.com/x\n./x.go:3:1: syntax error\nFAIL\texample.com/x [build failed]\n"
CARGO = """\
running 3 tests
test a ... ok
test b ... FAILED
test c ... ignored

test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.01s

running 2 tests
test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""


@pytest.mark.parametrize(
    ("output", "framework", "expected"),
    [
        (PYTEST_PASS, "pytest", (12, 0, 0, 0)),
        (PYTEST_MIXED, "pytest", (7, 2, 1, 4)),
        (PYTEST_QUIET, "pytest", (3, 1, 0, 0)),
        (PYTEST_NONE, "pytest", (0, 0, 0, 0)),
        (PYTEST_COLOR, "pytest", (5, 0, 0, 0)),
        (UNITTEST_OK, "unittest", (4, 0, 0, 0)),
        (UNITTEST_FAIL, "unittest", (2, 1, 1, 1)),
        (JEST, "jest", (10, 1, 0, 2)),
        (VITEST, "vitest", (4, 1, 0, 1)),
        (MOCHA, "mocha", (7, 2, 0, 1)),
        (PHPUNIT_OK, "phpunit", (5, 0, 0, 0)),
        (PHPUNIT_FAIL, "phpunit", (6, 2, 1, 1)),
        (GO, "go", (1, 1, 0, 1)),
        (GO_PKGS, "go", (2, 0, 0, 0)),
        (GO_BUILD, "go", (0, 0, 1, 0)),
        (CARGO, "cargo", (3, 1, 0, 1)),
    ],
)
def test_parse_counts_auto_detects(output: str, framework: str, expected: tuple[int, int, int, int]) -> None:
    counts = parse_counts(output)
    assert counts is not None
    assert counts.framework == framework
    assert (counts.passed, counts.failed, counts.errors, counts.skipped) == expected


def test_explicit_framework_and_generic() -> None:
    assert parse_counts(PYTEST_PASS, "jest") is None
    assert parse_counts(PYTEST_PASS, "generic") is None
    assert parse_counts("random build log\nall good\n") is None


def test_summarise_verdicts() -> None:
    assert summarise(PYTEST_PASS, 0).status == "passed"
    s = summarise(PYTEST_MIXED, 1)
    assert s.status == "failed" and s.failed == 2 and s.errors == 1
    # exit code wins over optimistic counts
    s = summarise(PYTEST_PASS, 2)
    assert s.status == "failed" and "exit code 2" in s.note
    # no tests collected (pytest exit 5) is an error, not a pass
    s = summarise(PYTEST_NONE, 5)
    assert s.status == "error" and s.note == "no tests ran"
    assert summarise(PYTEST_NONE, 0).status == "error"
    # generic: exit code only
    assert summarise("ok", 0).status == "passed" and summarise("ok", 0).framework == "generic"
    assert summarise("boom", 1).status == "failed"
    s = summarise("partial output", None, timed_out=True)
    assert s.status == "error" and s.note == "timed out"
    assert summarise("x", None).status == "error"
    line = summarise(JEST, 1).line()
    assert line.startswith("jest: failed – 10 passed, 1 failed")
