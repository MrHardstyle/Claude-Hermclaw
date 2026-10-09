"""Path normalisation, containment checks and glob matching for workspace-relative paths."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path, PurePosixPath

from hermclaw.core.errors import NotFoundError, PolicyViolation, ValidationFailed

_TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing", "e2e", "integration_tests"})
_TEST_FILE_RE = re.compile(
    r"""(?ix)
    (^test_.+\.py$) | (.+_test\.py$) | (^conftest\.py$)
    | (.+\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$)
    | (.+Test\.php$) | (.+_test\.go$) | (.+_spec\.rb$) | (.+_test\.rb$)
    | (.+Tests?\.(java|kt|cs)$)
    """
)


class RepoPathError(PolicyViolation):
    code = "REPO_PATH_FORBIDDEN"


def normalize_rel(path: str) -> str:
    """Canonical workspace-relative POSIX path; raises for absolute paths, ``..`` escapes and control characters."""
    if not isinstance(path, str) or not path.strip():
        raise ValidationFailed("empty path", code="REPO_PATH_INVALID")
    if "\0" in path or any(ord(c) < 32 for c in path):
        raise ValidationFailed("path contains control characters", code="REPO_PATH_INVALID")
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
        raise RepoPathError(f"absolute paths are not allowed: {path[:200]}", code="REPO_PATH_OUTSIDE")
    parts: list[str] = []
    for part in PurePosixPath(p).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise RepoPathError(f"path escapes the workspace: {path[:200]}", code="REPO_PATH_OUTSIDE")
        parts.append(part)
    if not parts:
        raise ValidationFailed("empty path", code="REPO_PATH_INVALID")
    return "/".join(parts)


@lru_cache(maxsize=4096)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-like glob (``**``, ``*``, ``?``, ``[...]``) into an anchored regex."""
    pat = pattern.strip()
    while pat.startswith("./"):
        pat = pat[2:]
    if pat.endswith("/"):
        pat += "**"
    i, out = 0, ["^"]
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def matches_glob(path: str, pattern: str) -> bool:
    pat = pattern.strip()
    if not pat:
        return False
    if "/" not in pat.rstrip("/"):
        # a bare pattern (``*.py``) matches the basename anywhere, like gitignore/ripgrep globs
        return bool(glob_regex(pat).match(path.rsplit("/", 1)[-1])) or bool(glob_regex(pat).match(path))
    return bool(glob_regex(pat).match(path))


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(matches_glob(path, p) for p in patterns)


def is_test_path(path: str) -> bool:
    """Generic test-file convention check (directories named ``tests``/``spec``… or test file name patterns)."""
    parts = path.split("/")
    name = parts[-1]
    if _TEST_FILE_RE.match(name):
        return True
    return any(p.lower() in _TEST_DIR_NAMES for p in parts[:-1])


def resolve_in_root(root: Path, rel: str, *, sensitive_globs: Iterable[str] = ()) -> tuple[str, Path]:
    """Resolve a workspace-relative path safely: no escape (also not via symlinks), no ``.git``, no secret files."""
    norm = normalize_rel(rel)
    if norm == ".git" or norm.startswith(".git/") or "/.git/" in f"/{norm}/":
        raise RepoPathError(f"git metadata is not readable through repository intelligence: {norm}")
    if matches_any(norm, sensitive_globs):
        raise RepoPathError(f"path is protected (secret/credential file): {norm}")
    root_real = Path(os.path.realpath(root))
    candidate = root_real / norm
    real = Path(os.path.realpath(candidate))
    try:
        real.relative_to(root_real)
    except ValueError as exc:
        raise RepoPathError(f"path resolves outside the workspace: {norm}", code="REPO_PATH_OUTSIDE") from exc
    if real != candidate:
        # a symlink somewhere in the chain – the target must be inside and must not be protected
        rel_target = real.relative_to(root_real).as_posix()
        if rel_target == ".git" or rel_target.startswith(".git/") or matches_any(rel_target, sensitive_globs):
            raise RepoPathError(f"symlink points to a protected path: {norm}")
    if not real.exists():
        raise NotFoundError(f"file not found: {norm}", code="REPO_FILE_NOT_FOUND")
    return norm, real
