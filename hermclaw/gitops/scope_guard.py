"""Path-level staging decisions for safe staging (Bauplan §17, P06 6.7).

The scope semantics themselves (glob matching, forbidden wins, target vs. new paths, allowed operations) are
owned by :class:`hermclaw.scope.guard.ScopeGuard` – the single authority shared by tools, verifier and git
staging, so a path a tool was allowed to write is exactly a path staging accepts. This module adds the
git-specific guards that only matter when building an index, and turns the guard's decision into a structured
refusal reason:

1. invalid / absolute / ``..`` path                                     -> ``invalid_path``
2. any ``.git`` path segment (also nested repositories' internals)      -> ``git_internal``
3. untracked directory entry (``dir/``: embedded repository)            -> ``embedded_repository``
4. matches ``policies.scope.always_forbidden``                          -> ``always_forbidden``
5. matches ``scope.forbidden_paths``                                    -> ``forbidden``
6. operation not in ``scope.allowed_operations``                        -> ``operation_not_allowed``
7. :meth:`ScopeGuard.decide` refuses                                    -> ``outside_scope``

The engine additionally refuses conflicted entries (``conflicted``) and symlinks that point outside the
workspace (``unsafe_symlink``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from hermclaw.contracts.scope import ScopeContract, normalise_path
from hermclaw.core.config import ScopePolicy
from hermclaw.scope.guard import ScopeGuard, path_matches

Operation = Literal["create", "modify", "delete"]
RefusalReason = Literal[
    "invalid_path",
    "git_internal",
    "always_forbidden",
    "forbidden",
    "operation_not_allowed",
    "outside_scope",
    "embedded_repository",
    "conflicted",
    "unsafe_symlink",
]
_WILDCARDS = frozenset("*?[")


@dataclass(frozen=True, slots=True)
class PathDecision:
    path: str
    operation: Operation
    allowed: bool
    reason: RefusalReason | None = None
    matched: str | None = None
    detail: str | None = None


def first_match(path: str, patterns: Iterable[str]) -> str | None:
    """First pattern (scope glob semantics) that matches ``path``."""
    for pattern in patterns:
        try:
            if path_matches(path, pattern):
                return pattern
        except ValueError:
            continue
    return None


def normalise_repo_path(path: str) -> str | None:
    """The repository-relative path itself, or ``None`` if it is unsafe or ambiguous.

    Paths are *not* rewritten: a name that the scope normaliser would change (surrounding whitespace,
    backslashes, ``./`` prefixes) is refused, otherwise the decision would be made for a different path than
    the one that gets staged.
    """
    if not path or "\x00" in path or "\\" in path or path != path.strip() or path.startswith(("/", "./")):
        return None
    parts = path.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    try:
        if normalise_path(path) != path:
            return None
    except ValueError:
        return None
    return path


class StagingGuard:
    """Wraps a :class:`ScopeGuard` for one scope contract and the scope policy."""

    def __init__(self, scope: ScopeContract, policy: ScopePolicy) -> None:
        self.scope = scope
        self.policy = policy
        self._guard = ScopeGuard(scope, policy)

    def decide(self, path: str, operation: Operation) -> PathDecision:
        norm = normalise_repo_path(path)
        if norm is None:
            return PathDecision(path, operation, False, "invalid_path")
        if ".git" in norm.rstrip("/").split("/"):
            return PathDecision(norm, operation, False, "git_internal", ".git")
        if norm.endswith("/"):
            return PathDecision(norm, operation, False, "embedded_repository")
        hit = first_match(norm, self.policy.always_forbidden)
        if hit:
            return PathDecision(norm, operation, False, "always_forbidden", hit)
        hit = first_match(norm, self.scope.forbidden_paths)
        if hit:
            return PathDecision(norm, operation, False, "forbidden", hit)
        if operation not in self.scope.allowed_operations:
            return PathDecision(norm, operation, False, "operation_not_allowed")
        ok, why = self._guard.decide(norm, operation)
        if not ok:
            # defence in depth: the authority may know more forbidden rules than the table above
            return PathDecision(norm, operation, False, "outside_scope", None, why)
        matched = first_match(norm, [*self.scope.target_paths, *self.scope.allowed_new_paths])
        return PathDecision(norm, operation, True, None, matched, why)


def decide_path(path: str, operation: Operation, scope: ScopeContract, always_forbidden: Sequence[str]) -> PathDecision:
    """Functional shortcut used by tests and callers without a policy object."""
    return StagingGuard(scope, ScopePolicy(always_forbidden=list(always_forbidden))).decide(path, operation)


def gitattributes_lines(patterns: Iterable[str], attribute: str = "-diff") -> list[str]:
    """Translate anchored scope globs into ``$GIT_DIR/info/attributes`` lines.

    Used to mark ``always_forbidden`` files (keys, ``.env`` …) as ``-diff`` so their content never shows up in
    diffs that are handed to models, events or the UI ("Binary files differ" instead).
    """
    lines: list[str] = []
    for raw in patterns:
        pat = raw.strip().replace("\\", "/")
        while pat.startswith("./"):
            pat = pat[2:]
        pat = pat.lstrip("/")
        if not pat or pat == ".git" or pat.startswith(".git/") or any(ch.isspace() for ch in pat):
            continue
        if pat.endswith("/"):
            pat = pat.rstrip("/") + "/**"
        anchored = pat if pat.startswith("**/") else "/" + pat
        lines.append(f"{anchored} {attribute}")
        if not pat.endswith("/**"):
            lines.append(f"{anchored}/** {attribute}")
    return lines
