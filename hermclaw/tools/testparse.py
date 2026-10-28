"""Parse test-runner output into counts (P17 17.6).

Supported: pytest, unittest, jest, vitest, mocha, node:test (TAP and spec reporter), phpunit, go test, cargo test,
Pester (PowerShell), the JUnit 5 console launcher, ``dotnet test`` (xUnit/NUnit/MSTest, C# and VB.NET), CTest,
GoogleTest, busted (Lua) and TAP producers such as bats;
everything else falls back to ``generic`` (exit code only).
Parsers only read the runner's own summary lines – no project-specific knowledge.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from hermclaw.tools.output import strip_ansi

Framework = Literal[
    "pytest",
    "unittest",
    "jest",
    "vitest",
    "mocha",
    "node",
    "phpunit",
    "go",
    "cargo",
    "pester",
    "junit",
    "dotnet",
    "ctest",
    "gtest",
    "busted",
    "tap",
    "generic",
]
TestStatus = Literal["passed", "failed", "error"]
FRAMEWORKS: tuple[Framework, ...] = (
    "pytest",
    "unittest",
    "jest",
    "vitest",
    "mocha",
    "node",
    "phpunit",
    "go",
    "cargo",
    "pester",
    "junit",
    "dotnet",
    "ctest",
    "gtest",
    "busted",
    "tap",
    "generic",
)


@dataclass(frozen=True)
class TestCounts:
    framework: Framework
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    #: the runner counted packages, not tests (``go test`` without ``-v``): a minimum number of tests is not verified
    packages_only: bool = False

    __test__ = False  # not a pytest class

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped


@dataclass(frozen=True)
class TestSummary:
    framework: Framework
    status: TestStatus
    passed: int
    failed: int
    errors: int
    skipped: int
    exit_code: int | None
    note: str = ""

    __test__ = False

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    def line(self) -> str:
        base = f"{self.framework}: {self.status} – {self.passed} passed, {self.failed} failed, {self.errors} errors, {self.skipped} skipped"
        return base + (f" ({self.note})" if self.note else "")


def _num(m: re.Match[str] | None, group: int | str = 1) -> int:
    if m is None:
        return 0
    value = m.group(group)
    return int(value) if value else 0


# ---------------------------------------------------------------------------------------------- pytest
_PYTEST_SUMMARY = re.compile(
    r"^=*\s*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun)(?:, )?)+)"
    r"\s+in\s+[\d.]+\s*s",
    re.M,
)
_PYTEST_NO_TESTS = re.compile(r"^=*\s*no tests ran\b", re.M)
_PYTEST_TOKEN = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")


def parse_pytest(out: str) -> TestCounts | None:
    matches = list(_PYTEST_SUMMARY.finditer(out))
    if not matches:
        if _PYTEST_NO_TESTS.search(out):
            return TestCounts("pytest")
        return None
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for num, word in _PYTEST_TOKEN.findall(matches[-1].group(1)):
        key = {"error": "errors", "xfailed": "skipped", "xpassed": "passed"}.get(word, word)
        counts[key] += int(num)
    return TestCounts("pytest", **counts)


# ---------------------------------------------------------------------------------------------- unittest
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s", re.M)
_UNITTEST_RESULT = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", re.M)


def parse_unittest(out: str) -> TestCounts | None:
    ran = list(_UNITTEST_RAN.finditer(out))
    if not ran:
        return None
    total = int(ran[-1].group(1))
    res = None
    for m in _UNITTEST_RESULT.finditer(out, ran[-1].end()):
        res = m
        break
    details: dict[str, int] = {}
    if res and res.group(2):
        for part in res.group(2).split(","):
            key, _, value = part.strip().partition("=")
            if value.strip().isdigit():
                details[key.strip()] = int(value)
    failed = details.get("failures", 0) + details.get("unexpected successes", 0)
    errors = details.get("errors", 0)
    skipped = details.get("skipped", 0) + details.get("expected failures", 0)
    passed = max(0, total - failed - errors - skipped)
    return TestCounts("unittest", passed=passed, failed=failed, errors=errors, skipped=skipped)


# ---------------------------------------------------------------------------------------------- jest
_JEST_TESTS = re.compile(r"^Tests:\s+(.*?\d+ total)\s*$", re.M)


def _jest_like(line: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for num, word in re.findall(r"(\d+) (passed|failed|skipped|pending|todo|total)", line):
        out[word] = out.get(word, 0) + int(num)
    return out


def parse_jest(out: str) -> TestCounts | None:
    matches = list(_JEST_TESTS.finditer(out))
    if not matches:
        return None
    c = _jest_like(matches[-1].group(1))
    return TestCounts(
        "jest", passed=c.get("passed", 0), failed=c.get("failed", 0), skipped=c.get("skipped", 0) + c.get("pending", 0) + c.get("todo", 0)
    )


# ---------------------------------------------------------------------------------------------- vitest
_VITEST_TESTS = re.compile(r"^\s*Tests\s+((?:\d+ \w+(?: \| )?)+)\s*\((\d+)\)\s*$", re.M)
_VITEST_FILES = re.compile(r"^\s*Test Files\s+((?:\d+ \w+(?: \| )?)+)\s*\((\d+)\)", re.M)


def parse_vitest(out: str) -> TestCounts | None:
    matches = list(_VITEST_TESTS.finditer(out))
    if not matches:
        return None
    c = _jest_like(matches[-1].group(1))
    errors = 0
    files = list(_VITEST_FILES.finditer(out))
    if files and _jest_like(files[-1].group(1)).get("failed", 0) and not c.get("failed", 0):
        errors = _jest_like(files[-1].group(1))["failed"]  # suites that failed to load
    return TestCounts(
        "vitest", passed=c.get("passed", 0), failed=c.get("failed", 0), errors=errors, skipped=c.get("skipped", 0) + c.get("todo", 0)
    )


# ---------------------------------------------------------------------------------------------- mocha
_MOCHA_PASSING = re.compile(r"^\s+(\d+) passing \(\d+(?:\.\d+)?\s*m?s\)", re.M)
_MOCHA_FAILING = re.compile(r"^\s+(\d+) failing\s*$", re.M)
_MOCHA_PENDING = re.compile(r"^\s+(\d+) pending\s*$", re.M)


def parse_mocha(out: str) -> TestCounts | None:
    passing = list(_MOCHA_PASSING.finditer(out))
    if not passing:
        return None
    return TestCounts(
        "mocha",
        passed=_num(passing[-1]),
        failed=_num(_MOCHA_FAILING.search(out, passing[-1].end())),
        skipped=_num(_MOCHA_PENDING.search(out)),
    )


# ---------------------------------------------------------------------------------------------- node:test
# ``node --test`` ends with "# tests 2 / # pass 1 / # fail 1 ..." (TAP reporter, the default without a TTY) or the same
# lines prefixed with "ℹ" (spec reporter). Without this parser a failing node run read "0 passed, 0 failed" (BUG-177).
_NODE_LINE = re.compile(r"^(?:#|\u2139)\s+(tests|pass|fail|cancelled|skipped|todo)\s+(\d+)\s*$", re.M)


def parse_node(out: str) -> TestCounts | None:
    found: dict[str, int] = {}
    for m in _NODE_LINE.finditer(out):
        found[m.group(1)] = int(m.group(2))  # the last summary wins
    if "tests" not in found or "pass" not in found:
        return None
    return TestCounts(
        "node",
        passed=found["pass"],
        failed=found.get("fail", 0),
        errors=found.get("cancelled", 0),
        skipped=found.get("skipped", 0) + found.get("todo", 0),
    )


# ---------------------------------------------------------------------------------------------- pester
# Pester 5/6 ends with "Tests Passed: 2, Failed: 1, Skipped: 0, Inconclusive: 0, NotRun: 0" (5.x without
# Inconclusive). Without this parser a PowerShell test step counted 0 passed tests (BUG-295).
_PESTER_SUMMARY = re.compile(r"^\s*Tests Passed: (\d+), Failed: (\d+), Skipped: (\d+)(?:,? Inconclusive: (\d+))?", re.M)


def parse_pester(out: str) -> TestCounts | None:
    found = _PESTER_SUMMARY.findall(out)
    if not found:
        return None
    passed, failed, skipped, inconclusive = found[-1]
    return TestCounts("pester", passed=int(passed), failed=int(failed), skipped=int(skipped) + int(inconclusive or 0))


# ---------------------------------------------------------------------------------------------- phpunit
_PHPUNIT_OK = re.compile(r"^OK \((\d+) tests?, \d+ assertions?\)", re.M)
_PHPUNIT_SUMMARY = re.compile(r"^Tests: (\d+), Assertions: \d+(.*)$", re.M)


def parse_phpunit(out: str) -> TestCounts | None:
    ok = list(_PHPUNIT_OK.finditer(out))
    if ok:
        return TestCounts("phpunit", passed=int(ok[-1].group(1)))
    matches = list(_PHPUNIT_SUMMARY.finditer(out))
    if not matches:
        return None
    total = int(matches[-1].group(1))
    rest = matches[-1].group(2)

    def field(name: str) -> int:
        m = re.search(rf"{name}: (\d+)", rest)
        return int(m.group(1)) if m else 0

    failed, errors = field("Failures"), field("Errors")
    skipped = field("Skipped") + field("Incomplete")
    return TestCounts("phpunit", passed=max(0, total - failed - errors - skipped), failed=failed, errors=errors, skipped=skipped)


# ---------------------------------------------------------------------------------------------- go test
_GO_CASE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): ", re.M)
_GO_PKG_OK = re.compile(r"^ok\s+\S+\s+(?:[\d.]+s|\(cached\))", re.M)
_GO_PKG_FAIL = re.compile(r"^FAIL\s+\S+\s+(?:[\d.]+s|\[build failed\]|\[setup failed\])", re.M)
_GO_BUILD_FAIL = re.compile(r"^FAIL\s+\S+\s+\[(?:build|setup) failed\]", re.M)


def parse_go(out: str) -> TestCounts | None:
    cases = _GO_CASE.findall(out)
    pkg_ok = _GO_PKG_OK.findall(out)
    pkg_fail = _GO_PKG_FAIL.findall(out)
    if not cases and not pkg_ok and not pkg_fail:
        return None
    build_failures = len(_GO_BUILD_FAIL.findall(out))
    if cases:
        return TestCounts("go", passed=cases.count("PASS"), failed=cases.count("FAIL"), skipped=cases.count("SKIP"), errors=build_failures)
    # without -v go test prints one line per package: five passing tests read as "1 passed" (R1-go on rc98, BUG-372)
    return TestCounts("go", passed=len(pkg_ok), failed=len(pkg_fail) - build_failures, errors=build_failures, packages_only=True)


# ---------------------------------------------------------------------------------------------- cargo test
_CARGO_RESULT = re.compile(r"^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; \d+ measured; \d+ filtered out", re.M)


def parse_cargo(out: str) -> TestCounts | None:
    matches = _CARGO_RESULT.findall(out)
    if not matches:
        return None
    passed = sum(int(m[0]) for m in matches)
    failed = sum(int(m[1]) for m in matches)
    ignored = sum(int(m[2]) for m in matches)
    errors = 1 if re.search(r"^error(?:\[E\d+\])?: could not compile", out, re.M) else 0
    return TestCounts("cargo", passed=passed, failed=failed, skipped=ignored, errors=errors)


# ---------------------------------------------------------------------------------------------- JUnit 5 console
_JUNIT_LINE = re.compile(r"^\[\s*(\d+) tests (successful|failed|skipped|aborted)\s*\]", re.M)


def parse_junit(out: str) -> TestCounts | None:
    """The console launcher's summary box (``[ 1 tests failed ]``); aborted tests (failed assumptions) count as skipped."""
    found = {kind: int(n) for n, kind in _JUNIT_LINE.findall(out)}
    if "successful" not in found and "failed" not in found:
        return None
    skipped = found.get("skipped", 0) + found.get("aborted", 0)
    return TestCounts("junit", passed=found.get("successful", 0), failed=found.get("failed", 0), skipped=skipped)


# ---------------------------------------------------------------------------------------------- dotnet test
_DOTNET_SUMMARY = re.compile(r"^\s*(?:Passed|Failed)!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+),\s+Skipped:\s+(\d+),\s+Total:\s+\d+", re.M)
# MSBuild's error line: "/w/Calc/Class1.cs(2,70): error CS0103: … [/w/Calc/Calc.csproj]" (BC… for VB.NET)
_DOTNET_BUILD_ERROR = re.compile(r"^.*\(\d+,\d+\): error [A-Z]+\d+: .*\[[^\]]+\.(?:cs|vb|fs)proj\]\s*$", re.M)


def parse_dotnet(out: str) -> TestCounts | None:
    """One ``Passed!``/``Failed!`` line per test assembly; a compile error (``error CS0103:``, ``error BC30451:``) is
    an error even when no assembly ran."""
    rows = _DOTNET_SUMMARY.findall(out)
    errors = 1 if _DOTNET_BUILD_ERROR.search(out) else 0
    if not rows:
        return TestCounts("dotnet", errors=1) if errors else None
    failed = sum(int(r[0]) for r in rows)
    passed = sum(int(r[1]) for r in rows)
    skipped = sum(int(r[2]) for r in rows)
    return TestCounts("dotnet", passed=passed, failed=failed, skipped=skipped, errors=errors)


# ---------------------------------------------------------------------------------------------- CTest / GoogleTest
_CTEST_SUMMARY = re.compile(r"^\d+% tests passed, (\d+) tests? failed out of (\d+)", re.M)
_CTEST_NONE = re.compile(r"^No tests were found!!!", re.M)
_GTEST_PASSED = re.compile(r"^\[  PASSED  \] (\d+) tests?\.", re.M)
_GTEST_FAILED = re.compile(r"^\[  FAILED  \] (\d+) tests?, listed below:", re.M)
_GTEST_SKIPPED = re.compile(r"^\[  SKIPPED \] (\d+) tests?, listed below:", re.M)


def parse_ctest(out: str) -> TestCounts | None:
    """CTest's own summary – it also prints each test's GoogleTest output, whose counts would double up."""
    m = _CTEST_SUMMARY.search(out)
    if m is None:
        return TestCounts("ctest") if _CTEST_NONE.search(out) else None
    failed, total = int(m.group(1)), int(m.group(2))
    return TestCounts("ctest", passed=total - failed, failed=failed)


def parse_gtest(out: str) -> TestCounts | None:
    passed = [int(n) for n in _GTEST_PASSED.findall(out)]
    failed = [int(n) for n in _GTEST_FAILED.findall(out)]
    if not passed and not failed:
        return None
    skipped = sum(int(n) for n in _GTEST_SKIPPED.findall(out))
    return TestCounts("gtest", passed=sum(passed), failed=sum(failed), skipped=skipped)


# ---------------------------------------------------------------------------------------------- busted (Lua)
_BUSTED_SUMMARY = re.compile(r"^(\d+) success(?:es)? / (\d+) failures? / (\d+) errors? / (\d+) pending : [\d.]+ seconds", re.M)


def parse_busted(out: str) -> TestCounts | None:
    m = _BUSTED_SUMMARY.search(out)
    if m is None:
        return None
    return TestCounts("busted", passed=int(m.group(1)), failed=int(m.group(2)), errors=int(m.group(3)), skipped=int(m.group(4)))


# ---------------------------------------------------------------------------------------------- TAP (bats, prove)
_TAP_PLAN = re.compile(r"^1\.\.(\d+)\s*$", re.M)
_TAP_RESULT = re.compile(r"^(ok|not ok) \d+\b(.*)$", re.M)
_TAP_SKIP = re.compile(r"#\s*(?:skip|todo)\b", re.I)


def parse_tap(out: str) -> TestCounts | None:
    """A TAP stream: the plan ``1..N`` and its ``ok``/``not ok`` lines; a planned test that never reported (the run
    died) is an error."""
    plan = _TAP_PLAN.search(out)
    results = _TAP_RESULT.findall(out)
    if plan is None or not results:
        return None
    skipped = sum(1 for _, rest in results if _TAP_SKIP.search(rest))
    failed = sum(1 for status, rest in results if status == "not ok" and not _TAP_SKIP.search(rest))
    passed = len(results) - failed - skipped
    missing = max(0, int(plan.group(1)) - len(results))
    return TestCounts("tap", passed=passed, failed=failed, skipped=skipped, errors=missing)


PARSERS: dict[Framework, Callable[[str], TestCounts | None]] = {
    "cargo": parse_cargo,
    "go": parse_go,
    "pytest": parse_pytest,
    "unittest": parse_unittest,
    "phpunit": parse_phpunit,
    "jest": parse_jest,
    "vitest": parse_vitest,
    "mocha": parse_mocha,
    "node": parse_node,
    "pester": parse_pester,
    "junit": parse_junit,
    "dotnet": parse_dotnet,
    "ctest": parse_ctest,  # before gtest: CTest repeats the output of every GoogleTest binary it runs
    "gtest": parse_gtest,
    "busted": parse_busted,
    "tap": parse_tap,  # last: the plan line and ok/not ok lines are the most generic summary
}


def parse_counts(output: str, framework: Framework | None = None) -> TestCounts | None:
    """Counts from runner output; ``framework`` restricts parsing to one runner (``generic`` = none)."""
    text = strip_ansi(output).replace("\r\n", "\n")
    if framework == "generic":
        return None
    if framework is not None:
        return PARSERS[framework](text)
    for parser in PARSERS.values():
        counts = parser(text)
        if counts is not None:
            return counts
    return None


def summarise(output: str, exit_code: int | None, *, timed_out: bool = False, framework: Framework | None = None) -> TestSummary:
    """Combine parsed counts and the exit code into a single verdict (exit code wins over optimistic counts)."""
    counts = parse_counts(output, framework)
    fw: Framework = counts.framework if counts else (framework or "generic")
    p, f, e, s = (counts.passed, counts.failed, counts.errors, counts.skipped) if counts else (0, 0, 0, 0)
    if timed_out:
        return TestSummary(fw, "error", p, f, e, s, exit_code, "timed out")
    if exit_code is None:
        return TestSummary(fw, "error", p, f, e, s, exit_code, "runner did not produce an exit code")
    if f or e:
        return TestSummary(fw, "failed", p, f, e, s, exit_code)
    if exit_code != 0:
        note = "no tests ran" if counts is not None and counts.total == 0 else f"exit code {exit_code}"
        return TestSummary(fw, "failed" if counts is None or counts.total else "error", p, f, e, s, exit_code, note)
    if counts is not None and counts.passed == 0:
        return TestSummary(fw, "error", p, f, e, s, exit_code, "no tests passed or ran")
    return TestSummary(fw, "passed", p, f, e, s, exit_code)
