"""Scores of coder tournament runs (weights agreed 2026-10-20: 50/15/10/10/10/5).

Rules on top of the weights:

* correctness comes first – effort and duration points only go to a run whose hidden checks all pass, so speed never
  makes up for an incomplete program;
* a failed hidden check named ``security``, or a change to a protected path, disqualifies the run (score 0);
* a run that ended on an infrastructure error is not the model's: it is reported apart and not scored.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class RunRecord:
    model: str
    task: str
    rep: int
    size: str
    job_id: str
    status: str  # job status (succeeded, failed, cancelled)
    error_code: str | None
    infrastructure: str | None  # why the run says nothing about the model, else None
    duration_minutes: float
    timed_out: bool
    hidden_passed: int
    hidden_failed: int
    hidden_failed_names: list[str]
    hidden_error: str | None  # the hidden check could not run (infrastructure of the grading)
    changed_files: list[str]
    unexpected_changes: list[str]
    protected_changes: list[str]
    corrections: int
    replans: int
    model_calls: int
    tool_calls: int
    failed_tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    model_seconds: float
    score: dict[str, float] = field(default_factory=dict)
    disqualified: str | None = None

    @property
    def hidden_total(self) -> int:
        return self.hidden_passed + self.hidden_failed

    @property
    def correct(self) -> bool:
        return self.hidden_total > 0 and self.hidden_failed == 0 and self.hidden_error is None

    @property
    def scored(self) -> bool:
        return self.infrastructure is None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def disqualification(r: RunRecord) -> str | None:
    if r.protected_changes:
        return "protected files changed: " + ", ".join(r.protected_changes[:5])
    security = [n for n in r.hidden_failed_names if "security" in n.lower()]
    if security:
        return "security check failed: " + ", ".join(security[:3])
    return None


def score_runs(runs: Sequence[RunRecord], weights: Mapping[str, float]) -> None:
    """Fill ``score`` of every scored run. Effort and duration are relative to the best correct run of the same task."""
    by_task: dict[str, list[RunRecord]] = {}
    for r in runs:
        if r.scored:
            by_task.setdefault(r.task, []).append(r)
    for task_runs in by_task.values():
        good = [r for r in task_runs if r.correct and disqualification(r) is None]
        best_calls = min((r.model_calls for r in good if r.model_calls), default=0)
        best_minutes = min((r.duration_minutes for r in good if r.duration_minutes), default=0.0)
        for r in task_runs:
            r.disqualified = disqualification(r)
            s = dict.fromkeys(weights, 0.0)
            if r.disqualified is None:
                ratio = r.hidden_passed / r.hidden_total if r.hidden_total and r.hidden_error is None else 0.0
                s["correct"] = weights["correct"] * ratio
                s["completed"] = weights["completed"] if r.status == "succeeded" else 0.0
                s["changes"] = weights["changes"] * max(0.0, 1.0 - 0.25 * len(r.unexpected_changes))
                if r.correct:
                    s["first_try"] = weights["first_try"] if r.status == "succeeded" and r.corrections == 0 and r.replans == 0 else 0.0
                    s["effort"] = weights["effort"] * min(1.0, best_calls / r.model_calls) if r.model_calls else 0.0
                    s["duration"] = weights["duration"] * min(1.0, best_minutes / r.duration_minutes) if r.duration_minutes else 0.0
            s["total"] = round(sum(s.values()), 2)
            r.score = {k: round(v, 2) for k, v in s.items()}


def _mean(values: Iterable[float]) -> float:
    vals = list(values)
    return round(statistics.fmean(vals), 2) if vals else 0.0


def summarise(runs: Sequence[RunRecord], models: Sequence[str]) -> list[dict[str, Any]]:
    """One row per model, best mean score first."""
    rows = []
    for model in models:
        mine = [r for r in runs if r.model == model]
        scored = [r for r in mine if r.scored]
        rows.append(
            {
                "model": model,
                "runs": len(scored),
                "infrastructure_runs": len(mine) - len(scored),
                "score": _mean(r.score.get("total", 0.0) for r in scored),
                "score_small": _mean(r.score.get("total", 0.0) for r in scored if r.size == "small"),
                "score_large": _mean(r.score.get("total", 0.0) for r in scored if r.size == "large"),
                "fully_correct": sum(1 for r in scored if r.correct and not r.disqualified),
                "first_try": sum(1 for r in scored if r.score.get("first_try")),
                "disqualified": sum(1 for r in scored if r.disqualified),
                "corrections": _mean(r.corrections for r in scored),
                "model_calls": _mean(r.model_calls for r in scored),
                "minutes": _mean(r.duration_minutes for r in scored),
                "unexpected_changes": sum(len(r.unexpected_changes) for r in scored),
                "tokens_per_second": round(sum(r.completion_tokens for r in scored) / max(1.0, sum(r.model_seconds for r in scored)), 1),
            }
        )
    return sorted(rows, key=lambda row: (-row["score"], -row["fully_correct"]))


def render_markdown(runs: Sequence[RunRecord], models: Sequence[str], title: str) -> str:
    rows = summarise(runs, models)
    out = [
        f"# {title}",
        "",
        "| Modell | Punkte | klein | groß | voll korrekt | 1. Versuch | K.o. | Korrekturen | Modellaufrufe | Minuten "
        "| unnötige Änderungen | Tok/s | Infra |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        out.append(
            f"| {row['model']} | {row['score']} | {row['score_small']} | {row['score_large']} | "
            f"{row['fully_correct']}/{row['runs']} | {row['first_try']} | {row['disqualified']} | {row['corrections']} | "
            f"{row['model_calls']} | {row['minutes']} | {row['unexpected_changes']} | {row['tokens_per_second']} | "
            f"{row['infrastructure_runs']} |"
        )
    out += ["", "## Läufe", "", "| Modell | Aufgabe | # | Status | versteckte Tests | Punkte | Minuten | Korr. | Aufrufe | Hinweis |"]
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(runs, key=lambda r: (r.model, r.task, r.rep)):
        note = r.infrastructure or r.disqualified or r.hidden_error or ", ".join(r.hidden_failed_names[:3])
        out.append(
            f"| {r.model} | {r.task} | {r.rep} | {r.status}{' (Zeitlimit)' if r.timed_out else ''} | "
            f"{r.hidden_passed}/{r.hidden_total} | {r.score.get('total', '–')} | {r.duration_minutes} | {r.corrections} | "
            f"{r.model_calls} | {note or ''} |"
        )
    return "\n".join(out) + "\n"
