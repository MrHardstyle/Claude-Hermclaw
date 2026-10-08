"""Planner inputs and tunables (Bauplan §15 planner input).

``PlannerInput`` is assembled by the orchestrator from repository intelligence, research and policy. It is the
only data the planner sees besides the job row itself. Everything here is generic: no repository- or
benchmark-specific knowledge is encoded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hermclaw.core.interfaces import RepoHit

TestFramework = Literal["pytest", "unittest", "npm", "phpunit", "go", "cargo", "generic"]


class ContextSnippet(BaseModel):
    """One retrieved code/context excerpt (usually a ``RepoHit`` from repository intelligence)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int = 1
    end_line: int = 1
    snippet: str = ""
    score: float = 0.0

    @classmethod
    def from_hit(cls, hit: RepoHit) -> ContextSnippet:
        return cls(path=hit.path, start_line=hit.start_line, end_line=hit.end_line, snippet=hit.snippet, score=hit.score)


class PlannerInput(BaseModel):
    """Compact, budgeted planner input (``job`` itself is loaded from the database)."""

    model_config = ConfigDict(extra="forbid")

    repository_inventory: dict[str, Any] = Field(default_factory=dict)
    retrieved_context: list[ContextSnippet] = Field(default_factory=list)
    research_summary: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] | None = Field(default=None, description="available capability names; None = all configured")
    constraints: list[str] = Field(default_factory=list)
    existing_tests: list[str] = Field(default_factory=list)
    risk_policy: dict[str, Any] = Field(default_factory=dict, description="overrides merged over the configured risk policy")
    known_paths: list[str] = Field(default_factory=list, description="complete repository file list for hint validation")
    known_symbols: list[str] = Field(default_factory=list)
    test_command: str | None = None
    test_framework: TestFramework | None = None


@dataclass(frozen=True)
class PlannerSettings:
    """Planner tunables. Defaults follow Bauplan §15 (max. 2 structured repair attempts)."""

    max_repair_attempts: int = 2
    max_steps: int = 30
    many_paths_threshold: int = 6
    chars_per_token: float = 3.2  # D-008 conservative estimate
    prompt_safety: float = 0.85
    max_echo_chars: int = 12_000  # previous (invalid) JSON answer echoed back in a repair turn
    max_errors_reported: int = 30


# --------------------------------------------------------------------------------------------- detection helpers
_FRAMEWORK_PATTERNS: tuple[tuple[re.Pattern[str], TestFramework], ...] = (
    (re.compile(r"\bpytest\b"), "pytest"),
    (re.compile(r"\bunittest\b"), "unittest"),
    (re.compile(r"\bphpunit\b"), "phpunit"),
    (re.compile(r"\b(npm|pnpm|yarn|npx|bun)\b|\b(jest|vitest|mocha)\b"), "npm"),
    (re.compile(r"\bgo\s+test\b"), "go"),
    (re.compile(r"\bcargo\s+test\b"), "cargo"),
)


def infer_framework(command: str) -> TestFramework:
    for pattern, framework in _FRAMEWORK_PATTERNS:
        if pattern.search(command):
            return framework
    return "generic"


def _first_command(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            found = _first_command(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("command", "cmd", "test_command", "commands", "test"):
            if key in value:
                found = _first_command(value[key])
                if found:
                    return found
        for key in sorted(value):
            found = _first_command(value[key])
            if found:
                return found
    return None


def detect_test_command(inputs: PlannerInput) -> tuple[str | None, TestFramework]:
    """The repository's test command: explicit input wins, else common inventory keys (deterministic)."""
    if inputs.test_command and inputs.test_command.strip():
        cmd = inputs.test_command.strip()
        return cmd, inputs.test_framework or infer_framework(cmd)
    inv = inputs.repository_inventory
    for key in ("test_command", "test_commands"):
        if key in inv:
            found = _first_command(inv[key])
            if found:
                return found, inputs.test_framework or infer_framework(found)
    tests = inv.get("tests")
    if isinstance(tests, dict):
        found = _first_command({k: v for k, v in tests.items() if k in ("command", "commands", "test_command", "cmd")})
        if found:
            return found, inputs.test_framework or infer_framework(found)
    return None, "generic"


_FILE_EXTENSIONS_TEXT = """py pyi pyx ipynb js mjs cjs jsx ts tsx mts cts vue svelte astro php phtml inc rb erb go rs java
    kt kts scala groovy
    c h cc cpp cxx hpp hh cs fs swift m mm sh bash zsh fish ps1 bat cmd sql psql html htm css scss sass less styl
    json jsonc json5 yaml yml toml ini cfg conf env xml xsd md mdx rst txt adoc lock gradle properties tf tfvars hcl
    j2 jinja jinja2 twig blade tpl mustache hbs service timer socket mount target dockerfile containerfile csv tsv
    svg png jpg jpeg gif webp ico mp4 webm proto graphql gql mod sum neon dist pem crt key pub log patch diff
"""
FILE_EXTENSIONS = frozenset(_FILE_EXTENSIONS_TEXT.split())
# Conventional file names without extension (generic tooling conventions, not repository-specific).
WELL_KNOWN_FILENAMES = frozenset(
    {
        "Makefile",
        "GNUmakefile",
        "Dockerfile",
        "Containerfile",
        "Jenkinsfile",
        "Vagrantfile",
        "Procfile",
        "Gemfile",
        "Rakefile",
        "Brewfile",
        "Justfile",
        "Caddyfile",
        "LICENSE",
        "README",
        "CHANGELOG",
        "CODEOWNERS",
    }
)
GLOB_CHARS = frozenset("*?[")
MAX_KNOWN_PATHS = 50_000
_MAX_HARVEST_DEPTH = 12


def has_file_shape(name: str) -> bool:
    """``name`` (last path segment) looks like a file: known extension, dotfile or conventional file name."""
    if not name:
        return False
    if name in WELL_KNOWN_FILENAMES or (name.startswith(".") and len(name) > 1):
        return True
    return "." in name and name.rsplit(".", 1)[-1].lower() in FILE_EXTENSIONS


def clean_repo_path(raw: str) -> str | None:
    """Normalised repository-relative path, or ``None`` if ``raw`` cannot be one (absolute, '..', blanks, URL)."""
    p = raw.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith(("/", "~")) or "://" in p or len(p) > 400 or any(c.isspace() for c in p):
        return None
    if ".." in p.split("/"):
        return None
    return p


def looks_like_repo_path(raw: str) -> bool:
    """Shape test for path-like strings found anywhere in the inventory (no globs, no prose, no URLs)."""
    p = clean_repo_path(raw)
    if p is None or any(c in GLOB_CHARS for c in p):
        return False
    return "/" in p or has_file_shape(p)


def _paths_from(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and isinstance(item.get("path"), str):
                out.append(item["path"])
    return out


def _harvest(value: Any, out: list[str], depth: int = 0) -> None:
    """Every path-shaped string (values and keys) of a JSON-like value, depth- and size-bounded."""
    if len(out) >= MAX_KNOWN_PATHS or depth > _MAX_HARVEST_DEPTH:
        return
    if isinstance(value, str):
        if looks_like_repo_path(value):
            out.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and looks_like_repo_path(key):
                out.append(key)
            _harvest(item, out, depth + 1)
    elif isinstance(value, list | tuple):
        for item in value:
            _harvest(item, out, depth + 1)


def collect_known_paths(inputs: PlannerInput) -> list[str]:
    """All repository paths the planner may legitimately reference.

    The prompt allows paths that appear in ``repository_inventory``, ``retrieved_context`` or ``existing_tests``,
    so the same sources ground ``repo_hints``: the explicit list, the inventory's ``files``/``paths`` lists
    (taken as-is), every other path-shaped string of the inventory, context snippet paths and test files
    (``tests/test_x.py::test_y`` counts as ``tests/test_x.py``).
    """
    harvested: list[str] = []
    _harvest({k: v for k, v in inputs.repository_inventory.items() if k not in ("files", "paths")}, harvested)
    candidates = [
        *inputs.known_paths,
        *_paths_from(inputs.repository_inventory.get("files")),
        *_paths_from(inputs.repository_inventory.get("paths")),
        *harvested,
        *(s.path for s in inputs.retrieved_context),
        *(t.split("::", 1)[0] for t in inputs.existing_tests if "/" in t or "." in t),
    ]
    seen: dict[str, None] = {}
    for raw in candidates:
        p = clean_repo_path(raw)
        if p is not None:
            seen.setdefault(p, None)
            if len(seen) >= MAX_KNOWN_PATHS:
                break
    return list(seen)


def collect_known_symbols(inputs: PlannerInput) -> list[str]:
    seen: dict[str, None] = {}
    raw: list[Any] = [*inputs.known_symbols]
    symbols = inputs.repository_inventory.get("symbols")
    if isinstance(symbols, list):
        raw.extend(symbols)
    for item in raw:
        name = item if isinstance(item, str) else item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name.strip():
            seen.setdefault(name.strip(), None)
    return list(seen)
