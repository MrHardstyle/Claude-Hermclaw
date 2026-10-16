"""ScopeGuard – the single authority deciding whether a path may be written (Bauplan §17).

Matching semantics (shared by tools, verifier, git staging):
- patterns are repository-relative POSIX globs; ``**`` matches any number of directories,
  ``*`` / ``?`` / ``[..]`` match within one path segment (``[!..]`` / ``[^..]`` negate, ``[[]`` is a literal ``[``);
  a pattern without glob characters matches the exact path or (if it ends with ``/``) everything below that directory.
- paths and patterns are compared in canonical form (:func:`~hermclaw.contracts.scope.normalise_path`: ``'.'`` and
  empty segments dropped), so ``src//a.py`` is judged exactly like ``src/a.py``; an invalid path is refused.
- forbidden (policy ``always_forbidden`` + contract ``forbidden_paths``) always wins.
- ``modify``/``delete`` require a match in ``target_paths``; ``create`` requires a match in
  ``allowed_new_paths`` (or ``target_paths`` when the file is explicitly listed there, also glob-escaped).
- operations not in ``allowed_operations`` are refused.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from hermclaw.contracts.scope import ScopeContract, normalise_path
from hermclaw.core.config import ScopePolicy
from hermclaw.core.errors import ScopeViolation

Operation = Literal["create", "modify", "delete"]


def _class_regex(pattern: str, start: int) -> tuple[str, int] | None:
    """Regex for the glob character class opening at ``pattern[start] == '['`` and the index of its closing ``]``.

    Glob semantics: ``[!..]`` / ``[^..]`` negate, a ``]`` right after the opening (or the negation mark) is a member,
    ``a-z`` is a range (an inverted range is empty) and every other member is literal – ``[[]`` matches ``[``. A class
    never matches ``/`` (it stays within one path segment). ``None`` when the class is not closed: the ``[`` is then a
    literal character. Members are escaped, so the result always compiles and never warns (no nested sets).
    """
    i = start + 1
    negate = i < len(pattern) and pattern[i] in "!^"
    if negate:
        i += 1
    body_start = i
    if i < len(pattern) and pattern[i] == "]":
        i += 1
    end = pattern.find("]", i)
    if end == -1:
        return None
    body = pattern[body_start:end]
    members: list[str] = []
    k = 0
    while k < len(body):
        if k + 2 < len(body) and body[k + 1] == "-":
            low, high = body[k], body[k + 2]
            if low <= high:
                members.append(f"{re.escape(low)}-{re.escape(high)}")
            k += 3
        else:
            members.append(re.escape(body[k]))
            k += 1
    content = "".join(members)
    if negate:
        return f"[^/{content}]", end
    return (f"(?!/)[{content}]" if content else "(?!)"), end


@lru_cache(maxsize=4096)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    if pattern.endswith("/"):
        pattern = pattern + "**"
    i, out = 0, ["^"]
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and (cls := _class_regex(pattern, i)) is not None:
            out.append(cls[0])
            i = cls[1]
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


@lru_cache(maxsize=4096)
def _canonical_pattern(pattern: str) -> str | None:
    """A pattern in the canonical path form (``'src//*.py'`` -> ``'src/*.py'``); ``None`` if it can match no path."""
    try:
        return normalise_path(pattern)
    except ValueError:  # absolute, '..', NUL or empty: no canonical repository path can match it
        return None


def path_matches(path: str, pattern: str) -> bool:
    """Whether ``path`` matches the scope glob ``pattern``; both are compared in canonical form (``normalise_path``).

    Raises :class:`ValueError` for an invalid ``path`` (absolute, ``..``, NUL, empty); an invalid pattern matches nothing.
    """
    p = normalise_path(path)
    pat = _canonical_pattern(pattern)
    if pat is None:
        return False
    if not any(ch in pat for ch in "*?[") and not pat.endswith("/"):
        return p == pat
    return bool(_glob_regex(pat).match(p))


def any_match(path: str, patterns: list[str]) -> bool:
    return any(path_matches(path, pat) for pat in patterns)


def acceptance_glob_matches(path: str, glob: str) -> bool:
    """Whether ``path`` matches a planner-written acceptance glob.

    Scope semantics, plus the gitignore convention for wildcard patterns without ``/``: they match the file name in
    any directory (``*`` = any file, ``*.py`` = any Python file). Planners write them that way; with scope semantics
    ``must_change: ["*"]`` matched only files in the repository root and failed a step that changed ``src/app.js``
    (BUG-166). Literal paths and patterns with ``/`` keep exact scope semantics.
    """
    if any_match(path, [glob]):
        return True
    g = glob.strip().rstrip("/")
    return bool(g) and "/" not in g and any(c in g for c in "*?[") and any_match(path.rsplit("/", 1)[-1], [g])


_ESCAPED_GLOB_CHAR_RE = re.compile(r"\[([\[*?])\]")


def _lists_file(pattern: str, path: str) -> bool:
    """Whether ``pattern`` explicitly lists the single file ``path`` (``'src/a.py'`` or glob-escaped ``'r/[[]id].ts'``)."""
    pat = _canonical_pattern(pattern)
    if pat is None or pat.endswith("/") or any(c in "*?[" for c in _ESCAPED_GLOB_CHAR_RE.sub("", pat)):
        return False
    return _ESCAPED_GLOB_CHAR_RE.sub(r"\1", pat) == path


class ScopeGuard:
    def __init__(self, contract: ScopeContract, policy: ScopePolicy | None = None) -> None:
        self.contract = contract
        self.policy = policy or ScopePolicy()

    @property
    def forbidden(self) -> list[str]:
        return [*self.policy.always_forbidden, *self.contract.forbidden_paths]

    def is_forbidden(self, path: str) -> bool:
        return any_match(path, self.forbidden)

    def decide(self, path: str, operation: Operation) -> tuple[bool, str]:
        try:
            p = normalise_path(path)
        except ValueError as exc:
            return False, str(exc)
        if self.is_forbidden(p):
            return False, f"path '{p}' is forbidden"
        if operation not in self.contract.allowed_operations:
            return False, f"operation '{operation}' not allowed by scope v{self.contract.version}"
        if operation in ("modify", "delete"):
            if any_match(p, self.contract.target_paths):
                return True, "target path"
            if not self.contract.strict_target_paths and any_match(p, self.contract.allowed_new_paths):
                return True, "non-strict scope: allowed new path"
            return False, f"path '{p}' is not a target path of scope v{self.contract.version}"
        # create
        if any_match(p, self.contract.allowed_new_paths) or any(_lists_file(pat, p) for pat in self.contract.target_paths):
            return True, "allowed new path"
        return False, f"creating '{p}' is outside allowed_new_paths of scope v{self.contract.version}"

    def allowed(self, path: str, operation: Operation) -> bool:
        return self.decide(path, operation)[0]

    def check(self, path: str, operation: Operation) -> None:
        ok, reason = self.decide(path, operation)
        if not ok:
            raise ScopeViolation(reason, details={"path": path, "operation": operation, "scope_version": self.contract.version})

    def audit(self, changes: list[tuple[str, Operation]]) -> list[dict[str, str]]:
        """Return violations for a list of (path, operation) changes (P15 15.9)."""
        violations = []
        for path, op in changes:
            ok, reason = self.decide(path, op)
            if not ok:
                violations.append({"path": path, "operation": op, "reason": reason})
        return violations
