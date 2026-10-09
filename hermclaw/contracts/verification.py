"""VerificationContract – deterministic verifier report (Bauplan §21)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from hermclaw.contracts.common import Contract

CheckStatus = Literal["pass", "fail", "skip", "error"]


class VerificationCheck(Contract):
    check_type: str = Field(
        description="scope|forbidden|syntax|compile|lint|unit|integration|secrets|conflicts|presence|absence|command|test|diff|schema|artifact|generated|changed_files|deletions|test_evidence|side_effects|contract|changes|workspace|verifier"
    )
    name: str
    status: CheckStatus
    message: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    blocking: bool = True


class VerificationReport(Contract):
    passed: bool
    checks: list[VerificationCheck]
    changed_files: list[str] = Field(default_factory=list)
    summary: str = ""

    @property
    def failures(self) -> list[VerificationCheck]:
        return [c for c in self.checks if c.status in ("fail", "error") and c.blocking]
