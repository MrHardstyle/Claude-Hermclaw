"""Per-job settings of a coder tournament run (``jobs.metadata[TOURNAMENT]``).

A tournament job measures the coder model alone: the plan is prescribed (no planner or triage call), every coding
step runs on the alias the run names, the heavy review is off unless asked for, and no replan is granted – a step
that blocks ends the job. The key is runtime-reserved (``hermclaw.core.job_metadata``), so only the operator CLI
(``hermclaw coder-tournament``) can set it, never ``POST /api/jobs``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hermclaw.core import job_metadata


class TournamentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: dict[str, Any]  # a PlanContract; checked by the planner's deterministic validation when it is adopted
    coder_alias: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    review: bool = False
    replans: int = Field(default=0, ge=0, le=5)
    task: str = ""
    run: str = ""


def tournament_of(metadata: Mapping[str, Any] | None) -> TournamentSettings | None:
    """The job's tournament settings, ``None`` for an ordinary job (or a malformed entry, which never applies)."""
    raw = (metadata or {}).get(job_metadata.TOURNAMENT)
    if not isinstance(raw, Mapping):
        return None
    try:
        return TournamentSettings.model_validate(dict(raw))
    except ValueError:
        return None
