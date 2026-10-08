"""ArtifactContract (P13.11)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from hermclaw.contracts.common import Contract


class ArtifactContract(Contract):
    id: UUID
    job_id: UUID | None = None
    step_id: UUID | None = None
    kind: str = Field(description="diff|log|report|image|video|backup|rollback|research|other")
    name: str
    media_type: str = "application/octet-stream"
    size_bytes: int = 0
    sha256: str = ""
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
