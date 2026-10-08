"""ResearchContract – sources, claims, synthesis (Bauplan §23)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from hermclaw.contracts.common import Contract

SourceType = Literal["official_docs", "official_repo", "standard", "vendor", "secondary", "forum", "unknown"]


class SourceRecord(Contract):
    source_id: str
    title: str
    url: str
    domain: str
    retrieved_at: datetime
    published_at: datetime | None = None
    source_type: SourceType = "unknown"
    authority_score: float = Field(ge=0, le=1)
    relevance_score: float = Field(ge=0, le=1)
    content_hash: str


class Claim(Contract):
    claim: str = Field(min_length=3, max_length=2000)
    source_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    used_for_decision: bool = False


class ClaimExtraction(Contract):
    """LLM output schema for claim extraction from one source."""

    claims: list[str] = Field(default_factory=list, max_length=12)


class ResearchSynthesis(Contract):
    """LLM output schema for synthesis; every statement must reference claim indices."""

    answer: str = Field(min_length=3, max_length=8000)
    key_points: list[str] = Field(default_factory=list, max_length=20)
    used_claims: list[int] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list, max_length=10)


class QueryPlan(Contract):
    queries: list[str] = Field(min_length=1, max_length=6)


class ResearchContract(Contract):
    question: str
    queries: list[str] = Field(default_factory=list)
    sources: list[SourceRecord] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    contradictions: list[list[int]] = Field(default_factory=list)
    synthesis: str = ""
    status: Literal["completed", "partial", "failed"] = "completed"
