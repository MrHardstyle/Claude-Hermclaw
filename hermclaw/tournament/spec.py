"""Specification of a coder tournament (YAML, ``tests/live/tournament/coder-tournament.yaml``).

::

    repository: hermclaw-test
    hidden_root: hidden                  # relative to the specification file
    models: [coder-main, coder-ornith-35b]
    defaults: {reps: 3, limit_minutes: 20}
    tasks:
      - id: A1
        title: Python – fix one lint finding
        base_branch: tournament/t-lint
        prompt: "..."                    # the job text every candidate gets
        plan: {goal: ..., summary: ..., steps: [...]}   # PlanContract, identical for every candidate
        hidden: {dir: A1, command: "python -m pytest -q -p no:cacheprovider _hidden", image: python}
        expected_changes: [accounts/stats.py]   # changed files outside these globs count as unnecessary
        protected: [data/**]             # a change here disqualifies the run
        size: small                      # small | large (report groups)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from hermclaw.contracts.plan import PlanContract

WEIGHTS: dict[str, float] = {"correct": 50, "first_try": 15, "effort": 10, "duration": 10, "changes": 10, "completed": 5}


class HiddenCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: str
    command: str
    image: str | None = None  # sandbox image alias of policies.sandbox.images; default: the job's image
    timeout_seconds: int = Field(default=600, ge=10, le=3600)


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,20}$")
    title: str
    prompt: str
    base_branch: str
    plan: dict[str, Any]
    hidden: HiddenCheck
    expected_changes: list[str] = Field(default_factory=list)
    protected: list[str] = Field(default_factory=list)
    size: Literal["small", "large"] = "small"
    limit_minutes: float | None = None
    reps: int | None = None
    repository: str | None = None


class TournamentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    hidden_root: str = "hidden"
    models: list[str] = Field(min_length=1)
    defaults: dict[str, Any] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=lambda: dict(WEIGHTS))
    tasks: list[TaskSpec] = Field(min_length=1)
    source: Path | None = None

    def limit_minutes(self, task: TaskSpec) -> float:
        return float(task.limit_minutes or self.defaults.get("limit_minutes", 20))

    def reps(self, task: TaskSpec) -> int:
        return int(task.reps or self.defaults.get("reps", 3))

    def hidden_dir(self, task: TaskSpec) -> Path:
        base = Path(self.hidden_root)
        if not base.is_absolute() and self.source is not None:
            base = self.source.parent / base
        return base / task.hidden.dir


def load_spec(path: str | Path) -> TournamentSpec:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    spec = TournamentSpec.model_validate({**data, "source": p.resolve()})
    for task in spec.tasks:
        PlanContract.model_validate(task.plan)  # schema errors surface before the first job
        if not spec.hidden_dir(task).is_dir():
            raise ValueError(f"task {task.id}: hidden check directory {spec.hidden_dir(task)} missing")
    if set(spec.weights) != set(WEIGHTS):
        raise ValueError(f"weights must name exactly {sorted(WEIGHTS)}")
    return spec
