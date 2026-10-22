"""Requirement coverage of the step's own tests: after the verifier passed, one model call lists the checkable
requirements of the job request and the step goal and names the delivered tests that check each one; a requirement
without an existing test is a gap the coder closes in one correction attempt.

Why: the tournament's large tasks failed on requirements the task states and the delivered tests never exercised,
with both coder models – one service still a JSON array, ``[]`` for none (A9), 400 for invalid bodies (A8), two notes
with distinct ids (A7). The verifier ran the delivered tests and passed, the review did not see the gap (Research
R-035, ladder A7 #6/#7). The model judges which test checks a requirement; whether a named test exists is checked
here, so an invented test name counts as a gap.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from hermclaw.context_builder import CorrectionItem
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.models.protocols import CallContext, ChatMessage, ChatModel

#: files that hold tests, by name (the step's changed files are matched against these)
TEST_GLOBS = (
    "tests/*",
    "test/*",
    "*/tests/*",
    "*/test/*",
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.Tests.ps1",
    "*.test.*",
    "*.spec.*",
)
_NAME_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(", re.MULTILINE),  # pytest / unittest
    re.compile(r"^\s*(?:It|Describe|Context)\s+[\"']([^\"'\n]+)[\"']", re.MULTILINE | re.IGNORECASE),  # Pester
    re.compile(r"\b(?:it|test)\s*\(\s*[\"'`]([^\"'`\n]+)[\"'`]"),  # Jest / Mocha / Vitest
    re.compile(r"^func\s+(Test\w+)\s*\(", re.MULTILINE),  # Go
)
_MAX_TEST_CHARS = 40_000
_MAX_REQUEST_CHARS = 6_000
_MAX_GAPS = 8

SYSTEM_PROMPT = """You check whether a coder's own tests cover the requirements of its task.
List every requirement of the TASK and STEP GOAL that a test can check on the behaviour of the program: inputs,
outputs, status codes, error cases, edge cases the text names (one item, none, invalid values), persistence, limits.
Keep each requirement short and concrete, one behaviour per item.
Leave out requirements about documentation, the README, file names or layout, the tests themselves, tooling or style.
For each requirement name the tests from TEST NAMES that check exactly that behaviour – a test that only touches the
feature without asserting the behaviour does not count. Copy the names exactly; leave the list empty when no test
checks it. Answer with JSON only."""


class RequirementCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    requirement: str = Field(min_length=1, max_length=300)
    tests: list[str] = Field(default_factory=list, max_length=12)


class CoverageDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", title="RequirementCoverage")

    requirements: list[RequirementCoverage] = Field(default_factory=list, max_length=40)


@dataclass(frozen=True)
class CoverageResult:
    requirements: int
    tests: int
    gaps: tuple[str, ...]
    alias: str


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch(path, g) or fnmatch(name, g) for g in TEST_GLOBS)


def names_in(text: str) -> list[str]:
    """Names of the tests in one file, in order, without duplicates."""
    found = [(m.start(), m.group(1).strip()) for p in _NAME_PATTERNS for m in p.finditer(text)]
    return list(dict.fromkeys(name for _, name in sorted(found)))


def read_tests(root: Path, changed: Sequence[str]) -> dict[str, str]:
    """The changed test files under ``root`` that exist and have tests, path → text."""
    out: dict[str, str] = {}
    for path in changed:
        file = root / path
        if not is_test_file(path) or not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if names_in(text):
            out[path] = text
    return out


def build_messages(request: str, goal: str, tests: dict[str, str]) -> list[ChatMessage]:
    names = [f"{path}::{name}" for path, text in tests.items() for name in names_in(text)]
    sources, used = [], 0
    for path, text in tests.items():
        left = _MAX_TEST_CHARS - used
        if left <= 200:
            sources.append(f"--- {path} (source left out: budget)")
            continue
        clip = text if len(text) <= left else text[:left] + "\n…[truncated]"
        sources.append(f"--- {path}\n{clip}")
        used += len(clip)
    user = "\n\n".join(
        [
            f"TASK\n{request[:_MAX_REQUEST_CHARS]}",
            f"STEP GOAL\n{goal[:_MAX_REQUEST_CHARS]}",
            "TEST NAMES\n" + "\n".join(names),
            "TEST SOURCES\n" + "\n\n".join(sources),
        ]
    )
    return [ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", DEFAULT_REDACTOR.text(user))]


def gaps_of(draft: CoverageDraft, tests: dict[str, str]) -> list[str]:
    """Requirements no existing test checks: an empty list or only names that are not in the files."""
    known = {name for text in tests.values() for name in names_in(text)}
    known |= {f"{path}::{name}" for path, text in tests.items() for name in names_in(text)}
    out = []
    for item in draft.requirements:
        named = {t.strip() for t in item.tests} | {t.strip().rsplit("::", 1)[-1] for t in item.tests}
        if not named & known:
            out.append(item.requirement)
    return list(dict.fromkeys(out))


def gap_items(gaps: Sequence[str]) -> list[CorrectionItem]:
    return [
        CorrectionItem(
            source="coverage",
            label="untested requirement",
            message=(
                f"No test of this step checks: {gap}. Add a test for it next to the existing tests and run the tests; "
                "if the new test fails, fix the code, not the test."
            ),
        )
        for gap in gaps[:_MAX_GAPS]
    ]


async def check_coverage(
    chat: ChatModel,
    alias: str,
    root: Path,
    changed: Sequence[str],
    *,
    request: str,
    goal: str,
    job_id: uuid.UUID,
    step_id: uuid.UUID,
    attempt_id: uuid.UUID,
    max_tokens: int,
    temperature: float | None = None,
) -> CoverageResult | None:
    """None when the step delivered no test file (nothing to judge)."""
    tests = read_tests(root, changed)
    if not tests:
        return None
    result = await chat.structured(
        alias,
        build_messages(request, goal, tests),
        CoverageDraft,
        ctx=CallContext(purpose="coverage", job_id=job_id, step_id=step_id, attempt_id=attempt_id),
        max_repairs=1,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    gaps = gaps_of(result.value, tests)
    count = sum(len(names_in(t)) for t in tests.values())
    return CoverageResult(len(result.value.requirements), count, tuple(gaps), result.result.alias or alias)
