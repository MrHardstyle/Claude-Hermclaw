"""ScopeContract – explicit, runtime-authorised write scope for a mutating step (Bauplan §17)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from hermclaw.contracts.common import Contract

Operation = Literal["create", "modify", "delete"]


def normalise_path(path: str) -> str:
    """Canonical repository-relative POSIX form of a scope path or pattern (idempotent).

    ``'.'`` and empty segments are dropped (``'src/./a.py'``, ``'src//a.py'``, ``'./src/a.py'`` -> ``'src/a.py'``) and a
    trailing directory slash is kept. Absolute paths, ``'..'`` segments, NUL bytes and paths without any real segment
    (``''``, ``'.'``, ``'./'``) raise :class:`ValueError`.
    """
    q = path.strip().replace("\\", "/")
    segments = [seg for seg in q.split("/") if seg not in ("", ".")]
    if q.startswith("/") or "\x00" in q or not segments or ".." in segments:
        raise ValueError(f"scope paths must be non-empty, repository-relative and without '..' or NUL bytes: {path!r}")
    return "/".join(segments) + ("/" if q.endswith("/") else "")


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
