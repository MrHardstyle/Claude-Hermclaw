"""Parse test-runner output into counts (P17 17.6).

Supported: pytest, unittest, jest, vitest, mocha, phpunit, go test, cargo test; everything else falls back to
``generic`` (exit code only). Parsers only read the runner's own summary lines – no project-specific knowledge.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from hermclaw.tools.output import strip_ansi

Framework = Literal["pytest", "unittest", "jest", "vitest", "mocha", "phpunit", "go", "cargo", "generic"]
TestStatus = Literal["passed", "failed", "error"]
FRAMEWORKS: tuple[Framework, ...] = ("pytest", "unittest", "jest", "vitest", "mocha", "phpunit", "go", "cargo", "generic")


@dataclass(frozen=True)
class TestCounts:
    framework: Framework
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0

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
        "mocha", passed=_num(passing[-1]), failed=_num(_MOCHA_FAILING.search(out, passing[-1].end())), skipped=_num(_MOCHA_PENDING.search(out))
    )


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
        return TestCounts(
            "go", passed=cases.count("PASS"), failed=cases.count("FAIL"), skipped=cases.count("SKIP"), errors=build_failures
        )
    return TestCounts("go", passed=len(pkg_ok), failed=len(pkg_fail) - build_failures, errors=build_failures)


# ---------------------------------------------------------------------------------------------- cargo test
_CARGO_RESULT = re.compile(
    r"^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; \d+ measured; \d+ filtered out", re.M
)


def parse_cargo(out: str) -> TestCounts | None:
    matches = _CARGO_RESULT.findall(out)
    if not matches:
        return None
    passed = sum(int(m[0]) for m in matches)
    failed = sum(int(m[1]) for m in matches)
    ignored = sum(int(m[2]) for m in matches)
    errors = 1 if re.search(r"^error(?:\[E\d+\])?: could not compile", out, re.M) else 0
    return TestCounts("cargo", passed=passed, failed=failed, skipped=ignored, errors=errors)


PARSERS: dict[Framework, Callable[[str], TestCounts | None]] = {
    "cargo": parse_cargo,
    "go": parse_go,
    "pytest": parse_pytest,
    "unittest": parse_unittest,
    "phpunit": parse_phpunit,
    "jest": parse_jest,
    "vitest": parse_vitest,
    "mocha": parse_mocha,
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
