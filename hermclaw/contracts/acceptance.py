"""Machine-checkable acceptance evidence (Verifier evidence types, Bauplan §21).

Every planner step carries a list of ``AcceptanceCriterion``. The deterministic verifier evaluates them
without any task-specific production rules: presence, absence, command, test, diff, scope, schema,
security and artifact evidence.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from hermclaw.contracts.common import Contract


class _Base(Contract):
    description: str = Field(default="", max_length=500)


class PresenceEvidence(_Base):
    type: Literal["presence"] = "presence"
    path_glob: str
    pattern: str | None = Field(default=None, description="regex that must match at least min_matches times")
    min_matches: int = Field(default=1, ge=1)


class AbsenceEvidence(_Base):
    type: Literal["absence"] = "absence"
    path_glob: str
    pattern: str | None = Field(default=None, description="regex; if None the path itself must not exist")
    expected_matches: Literal[0] = 0


class CommandEvidence(_Base):
    type: Literal["command"] = "command"
    command: str = Field(min_length=1, max_length=2000)
    expect_exit_code: int = 0
    stdout_pattern: str | None = None
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    network: bool = False


class TestEvidence(_Base):
    __test__ = False  # not a pytest test class
    type: Literal["test"] = "test"
    command: str = Field(min_length=1, max_length=2000)
    framework: Literal["pytest", "unittest", "npm", "phpunit", "go", "cargo", "generic"] = "generic"
    min_passed: int = Field(default=1, ge=0)
    timeout_seconds: int = Field(default=600, ge=1, le=7200)


class DiffEvidence(_Base):
    type: Literal["diff"] = "diff"
    must_change: list[str] = Field(default_factory=list, description="globs that must appear in the diff")
    must_not_change: list[str] = Field(default_factory=list)
    max_changed_files: int | None = Field(default=None, ge=0)
    allow_empty: bool = False


class ScopeEvidence(_Base):
    type: Literal["scope"] = "scope"


class SchemaEvidence(_Base):
    type: Literal["schema"] = "schema"
    path: str
    format: Literal["json", "yaml", "toml"] = "json"
    json_schema: dict[str, Any] | None = None


class SecurityEvidence(_Base):
    type: Literal["security"] = "security"
    secret_scan: bool = True
    conflict_markers: bool = True


class ArtifactEvidence(_Base):
    type: Literal["artifact"] = "artifact"
    kind: str = Field(min_length=1)
    name_glob: str = "*"
    min_count: int = Field(default=1, ge=1)
    min_size_bytes: int = Field(default=1, ge=0)


AcceptanceCriterion = Annotated[
    PresenceEvidence
    | AbsenceEvidence
    | CommandEvidence
    | TestEvidence
    | DiffEvidence
    | ScopeEvidence
    | SchemaEvidence
    | SecurityEvidence
    | ArtifactEvidence,
    Field(discriminator="type"),
]

EVIDENCE_TYPES = ("presence", "absence", "command", "test", "diff", "scope", "schema", "security", "artifact")


class AcceptanceList(Contract):
    items: list[AcceptanceCriterion] = Field(default_factory=list)

    @field_validator("items")
    @classmethod
    def _not_too_many(cls, v: list[Any]) -> list[Any]:
        if len(v) > 30:
            raise ValueError("at most 30 acceptance criteria per step")
        return v
