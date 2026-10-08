"""ScopeContract – explicit, runtime-authorised write scope for a mutating step (Bauplan §17)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from hermclaw.contracts.common import Contract

Operation = Literal["create", "modify", "delete"]


def normalise_path(path: str) -> str:
    q = path.strip().replace("\\", "/")
    if q.startswith("/"):
        raise ValueError(f"scope paths must be non-empty, repository-relative and without '..': {path!r}")
    trailing = q.endswith("/") and q.strip("/") != ""
    # canonical form: drop '.' and empty segments ('src/./a.py', 'src//a.py' -> 'src/a.py'); keep a directory slash
    q = "/".join(seg for seg in q.split("/") if seg not in ("", ".")) + ("/" if trailing else "")
    if not q or ".." in q.split("/"):
        raise ValueError(f"scope paths must be non-empty, repository-relative and without '..': {path!r}")
    return q


def normalise_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        q = normalise_path(p)
        if q not in out:
            out.append(q)
    return out


class ScopeContract(Contract):
    source: Literal["planner_and_repo_intelligence", "runtime_expansion", "replanner", "manual"] = "planner_and_repo_intelligence"
    version: int = Field(default=1, ge=1)
    strict_target_paths: bool = True
    target_paths: list[str] = Field(default_factory=list, description="existing files (or globs) that may be modified")
    allowed_new_paths: list[str] = Field(default_factory=list, description="paths/globs where new files may be created")
    forbidden_paths: list[str] = Field(default_factory=list)
    allowed_operations: list[Operation] = Field(default_factory=lambda: list[Operation](["create", "modify"]))
    reason: str = ""

    @field_validator("target_paths", "allowed_new_paths", "forbidden_paths")
    @classmethod
    def _normalise(cls, paths: list[str]) -> list[str]:
        return normalise_paths(paths)


class ScopeExpansionRequest(Contract):
    paths: list[str] = Field(min_length=1, max_length=20)
    operations: list[Operation] = Field(default_factory=lambda: list[Operation](["modify"]))
    justification: str = Field(min_length=10, max_length=2000)
