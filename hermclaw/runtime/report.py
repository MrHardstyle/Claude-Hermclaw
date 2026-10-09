"""Deterministic final job report (Bauplan §1 item 21 "vollständigen Abschlussbericht erstellen").

Built only from persisted facts (jobs, plan versions, steps, attempts, verification, review, research, git, model
invocations). No model text other than final step summaries is included; no reasoning is ever stored or shown.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.persistence.models import (
    Artifact,
    GitOperation,
    Job,
    ModelInvocation,
    PlanVersion,
    ResearchRun,
    ResearchSource,
    ReviewFindingRow,
    ReviewRun,
    Step,
    StepAttempt,
    VerificationRun,
)


def _cell(text: Any, limit: int = 160) -> str:
    s = DEFAULT_REDACTOR.text(str(text if text is not None else "")).replace("\n", " ").replace("|", "\\|").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


async def collect_report_data(s: AsyncSession, job_id: uuid.UUID) -> dict[str, Any]:
    job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
    steps = (await s.execute(select(Step).where(Step.job_id == job_id).order_by(Step.created_at, Step.step_key))).scalars().all()
    attempts = (await s.execute(select(StepAttempt).where(StepAttempt.job_id == job_id))).scalars().all()
    by_step: dict[uuid.UUID, list[StepAttempt]] = {}
    for a in attempts:
        by_step.setdefault(a.step_id, []).append(a)
    versions = (await s.execute(select(PlanVersion).where(PlanVersion.job_id == job_id).order_by(PlanVersion.version))).scalars().all()
    verifications = (
        (await s.execute(select(VerificationRun).where(VerificationRun.job_id == job_id).order_by(VerificationRun.created_at)))
        .scalars()
        .all()
    )
    reviews = (await s.execute(select(ReviewRun).where(ReviewRun.job_id == job_id).order_by(ReviewRun.created_at))).scalars().all()
    findings = (
        (
            await s.execute(
                select(ReviewFindingRow).join(ReviewRun, ReviewRun.id == ReviewFindingRow.review_run_id).where(ReviewRun.job_id == job_id)
            )
        )
        .scalars()
        .all()
    )
    research = (await s.execute(select(ResearchRun).where(ResearchRun.job_id == job_id).order_by(ResearchRun.created_at))).scalars().all()
    sources = (
        (
            await s.execute(
                select(ResearchSource)
                .join(ResearchRun, ResearchRun.id == ResearchSource.research_run_id)
                .where(ResearchRun.job_id == job_id)
                .order_by(ResearchSource.authority_score.desc())
            )
        )
        .scalars()
        .all()
    )
    git_ops = (await s.execute(select(GitOperation).where(GitOperation.job_id == job_id).order_by(GitOperation.created_at))).scalars().all()
    model_stats = (
        await s.execute(
            select(ModelInvocation.alias, func.count(), func.coalesce(func.sum(ModelInvocation.completion_tokens), 0))
            .where(ModelInvocation.job_id == job_id)
            .group_by(ModelInvocation.alias)
        )
    ).all()
    return {
        "job": job,
        "steps": steps,
        "attempts": by_step,
        "versions": versions,
        "verifications": verifications,
        "reviews": reviews,
        "findings": findings,
        "research": research,
        "sources": sources,
        "git_ops": git_ops,
        "model_stats": model_stats,
    }


def render_report(data: dict[str, Any], *, extra: dict[str, Any] | None = None) -> str:
    job: Job = data["job"]
    extra = extra or {}
    out: list[str] = [f"# Abschlussbericht: {_cell(job.title, 200)}", ""]
    out += [
        f"- Job-ID: `{job.id}`",
        f"- Status beim Abschluss: `{extra.get('final_status', job.status)}`",
        f"- Plan-Versionen: {len(data['versions'])} · Replans: {job.replan_count}",
        f"- Erstellt: {job.created_at:%Y-%m-%d %H:%M UTC}" if job.created_at else "- Erstellt: –",
        f"- Bericht erzeugt: {datetime.now(UTC):%Y-%m-%d %H:%M UTC}",
        "",
        "## Auftrag",
        "",
        DEFAULT_REDACTOR.text(job.prompt.strip())[:4000],
        "",
    ]
    if extra.get("triage"):
        t = extra["triage"]
        out += [
            "## Triage (Fast Router)",
            "",
            f"- Intent: `{_cell(t.get('intent'))}` · Risiko: `{_cell(t.get('risk'))}`",
            f"- {_cell(t.get('summary'), 400)}",
            "",
        ]
    out += ["## Schritte", "", "| Step | Art | Status | Versuche | Ergebnis |", "|---|---|---|---|---|"]
    for st in data["steps"]:
        tries = data["attempts"].get(st.id, [])
        last = max(tries, key=lambda a: a.attempt_no) if tries else None
        result = (last.summary if last and last.summary else st.error_message) or ""
        flag = " (ersetzt)" if st.superseded else ""
        out.append(f"| {st.step_key}{flag} – {_cell(st.title, 60)} | {st.kind} | {st.status} | {len(tries)} | {_cell(result)} |")
    out.append("")
    if data["verifications"]:
        passed = sum(1 for v in data["verifications"] if v.passed)
        out += ["## Verifikation", "", f"{passed}/{len(data['verifications'])} Verifier-Läufe bestanden.", ""]
        for v in data["verifications"][-10:]:
            out.append(f"- {'✔' if v.passed else '✘'} {_cell(v.summary, 200)}")
        out.append("")
    if data["reviews"]:
        out += ["## Heavy Review", ""]
        for r in data["reviews"][-10:]:
            out.append(f"- Verdict `{r.verdict}`{' (Invariante erzwungen)' if r.invariant_override else ''}: {_cell(r.summary, 200)}")
        blocking = [f for f in data["findings"] if f.severity in ("major", "blocker")]
        if blocking:
            out += ["", "Major/Blocker-Findings:"]
            out += [f"- [{f.severity}] `{_cell(f.path, 80)}`: {_cell(f.summary, 200)}" for f in blocking[:20]]
        out.append("")
    if data["research"]:
        out += ["## Research und Quellen", ""]
        for r in data["research"]:
            out.append(f"- Frage: {_cell(r.question, 200)} ({r.status})")
        for src in data["sources"][:25]:
            out.append(f"  - [{_cell(src.title or src.url, 100)}]({src.url}) – {src.source_type}, Autorität {src.authority_score:.2f}")
        out.append("")
    commits = [g for g in data["git_ops"] if g.operation == "commit" and g.status == "ok"]
    pushes = [g for g in data["git_ops"] if g.operation == "push" and g.status == "ok"]
    out += ["## Git", ""]
    if commits:
        out += [f"- Commit `{(g.sha_after or '')[:12]}` auf `{g.ref}`" for g in commits]
    else:
        out.append("- keine Commits (keine Repository-Änderungen)")
    out += [f"- Push `{g.ref}` → `{(g.sha_after or '')[:12]}`" for g in pushes]
    if extra.get("merge_request_url"):
        out.append(f"- Merge Request: {extra['merge_request_url']}")
    if extra.get("merge_request_error"):
        out.append(f"- Merge Request nicht erstellt: {_cell(extra['merge_request_error'], 300)}")
    out.append("")
    if data["model_stats"]:
        out += ["## Modellnutzung", "", "| Alias | Aufrufe | Completion-Tokens |", "|---|---|---|"]
        out += [f"| {a} | {n} | {int(t)} |" for a, n, t in data["model_stats"]]
        out.append("")
    if extra.get("notes"):
        out += ["## Hinweise", ""] + [f"- {_cell(n, 400)}" for n in extra["notes"]] + [""]
    return "\n".join(out)


async def write_report_artifact(s: AsyncSession, job_id: uuid.UUID, text: str, artifacts_dir: Path) -> Artifact:
    """Persist the report below ``<artifacts_dir>/<job_id>/`` and register it as artifact (kind ``report``)."""
    import asyncio

    target = artifacts_dir / str(job_id) / "final_report.md"
    data = text.encode("utf-8")

    def _write() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".md.tmp")
        tmp.write_bytes(data)
        tmp.replace(target)

    await asyncio.to_thread(_write)
    existing = (
        await s.execute(select(Artifact).where(Artifact.job_id == job_id, Artifact.kind == "report", Artifact.name == "final_report.md"))
    ).scalar_one_or_none()
    art = existing or Artifact(job_id=job_id, kind="report", name="final_report.md", path=str(target), media_type="text/markdown")
    art.size_bytes = len(data)
    art.sha256 = hashlib.sha256(data).hexdigest()
    art.path = str(target)
    if existing is None:
        s.add(art)
    await s.flush()
    return art
