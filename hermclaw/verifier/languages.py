"""Language detection and repository facts used by generic verifier checks (no project knowledge)."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable

LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".json": "json",
    ".jsonc": "json",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".sh": "shell",
    ".bash": "shell",
    ".php": "php",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".swift": "swift",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".css": "css",
    ".scss": "css",
    ".html": "html",
    ".htm": "html",
    ".vue": "vue",
    ".svelte": "svelte",
    ".sql": "sql",
    ".xml": "xml",
    ".md": "markdown",
    ".markdown": "markdown",
    ".rst": "markup",
    ".adoc": "markup",
    ".txt": "text",
    ".ini": "config",
    ".cfg": "config",
    ".conf": "config",
    ".properties": "config",
    ".env": "config",
    ".zsh": "zsh",
    ".fish": "fish",
    ".ps1": "powershell",
}

SPECIAL_NAMES: dict[str, str] = {"dockerfile": "dockerfile", "makefile": "make", "gnumakefile": "make", "containerfile": "dockerfile"}

# languages in which an unquoted value after ``key =`` is an expression (variable, call), not a literal
CODE_LANGUAGES = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "go",
        "rust",
        "ruby",
        "java",
        "kotlin",
        "scala",
        "swift",
        "csharp",
        "c",
        "cpp",
        "php",
        "vue",
        "svelte",
    }
)
# plain-text formats where a line of ``=======`` is a heading underline, not a conflict separator
MARKUP_LANGUAGES = frozenset({"markdown", "markup", "text"})

_SHEBANG = re.compile(rb"^#!\s*(?:/usr/bin/env\s+(?:-S\s+)?)?(?:\S*/)?([A-Za-z0-9_.+-]+)")
_SHEBANG_LANG = {
    "python": "python",
    "python3": "python",
    "bash": "shell",
    "sh": "shell",
    "dash": "shell",
    "node": "javascript",
    "php": "php",
    "zsh": "zsh",
    "ruby": "ruby",
}

_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing"})
_TEST_FILE = re.compile(
    r"^(?:test_.+\.py|.+_test\.py|.+_test\.go|.+\.(?:test|spec)\.[cm]?[jt]sx?|.+Test\.php|.+_spec\.rb|.+Tests?\.(?:java|kt|cs)"
    r"|conftest\.py|pytest\.ini|phpunit\.xml(?:\.dist)?|jest\.config\.[cm]?[jt]s|vitest\.config\.[cm]?[jt]s)$"
)


def detect_language(path: str, head: bytes | None = None) -> str | None:
    """Language of ``path`` from its extension, special file names or (extension-less files) the shebang line."""
    base = posixpath.basename(path)
    lower = base.lower()
    if lower in SPECIAL_NAMES:
        return SPECIAL_NAMES[lower]
    if lower.startswith(".env"):
        return "config"
    ext = posixpath.splitext(lower)[1]
    if ext in LANGUAGE_BY_EXTENSION:
        return LANGUAGE_BY_EXTENSION[ext]
    if head and head.startswith(b"#!"):
        m = _SHEBANG.match(head.split(b"\n", 1)[0])
        if m:
            interp = m.group(1).decode("ascii", errors="ignore").lower()
            interp = re.sub(r"[0-9.]+$", "", interp) if interp.startswith("python") else interp
            return _SHEBANG_LANG.get(interp)
    return None


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    if any(p.lower() in _TEST_DIRS for p in parts[:-1]):
        return True
    return bool(_TEST_FILE.match(parts[-1]))


def repo_has_tests(files: Iterable[str]) -> list[str]:
    """Up to five test files/dirs that show the repository has an automated test suite (empty: no tests)."""
    found: list[str] = []
    for f in files:
        if is_test_path(f):
            found.append(f)
            if len(found) >= 5:
                break
    return found


LOCK_FILES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pipfile.lock",
        "uv.lock",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        "flake.lock",
        "bun.lockb",
        "mix.lock",
        "packages.lock.json",
    }
)


def is_lock_or_data_file(path: str) -> bool:
    """Files full of hashes/encoded blobs where the entropy heuristic only produces noise."""
    base = posixpath.basename(path).lower()
    if base in LOCK_FILES or base.endswith((".lock", ".sum")):
        return True
    return base.endswith((".svg", ".ipynb", ".map", ".min.js", ".min.css", ".snap", ".har", ".drawio"))
