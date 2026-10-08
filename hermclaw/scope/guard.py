"""ScopeGuard – the single authority deciding whether a path may be written (Bauplan §17).

Matching semantics (shared by tools, verifier, git staging):
- patterns are repository-relative POSIX globs; ``**`` matches any number of directories,
  ``*`` / ``?`` / ``[..]`` match within one path segment; a pattern without glob characters matches the
  exact path or (if it ends with ``/``) everything below that directory.
- forbidden (policy ``always_forbidden`` + contract ``forbidden_paths``) always wins.
- ``modify``/``delete`` require a match in ``target_paths``; ``create`` requires a match in
  ``allowed_new_paths`` (or ``target_paths`` when the file is explicitly listed there).
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
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                out.append("[" + pattern[i + 1 : j].replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def path_matches(path: str, pattern: str) -> bool:
    p = normalise_path(path)
    pat = pattern.strip()
    while pat.startswith("./"):
        pat = pat[2:]
    if not pat:
        return False
    if not any(ch in pat for ch in "*?[") and not pat.endswith("/"):
        return p == pat
    return bool(_glob_regex(pat).match(p))


def any_match(path: str, patterns: list[str]) -> bool:
    return any(path_matches(path, pat) for pat in patterns)


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
        if any_match(p, self.contract.allowed_new_paths) or p in self.contract.target_paths:
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
