"""Workspace adapter of the heavy review – the ``ReviewerPort`` the implement step handler (P19/P23) calls.

``WorkspaceReviewer.review(step, workspace, verification, job_id=…, step_id=…, attempt_id=…)`` collects the review
input from the real services and delegates to ``HeavyReviewer``:

- job goal (``jobs.title`` + ``jobs.prompt``) from PostgreSQL,
- unified diff + changed files against ``workspace.base_sha`` via the ``GitReader`` protocol,
- executed commands of the attempt (``command_runs``) – change evidence for steps that mutate outside the repository,
- relevant code/test snippets via the ``RepoContextProvider`` protocol (optional; protected paths are never shown).

Collecting is bounded by ``ReviewSettings.context_timeout_seconds``. If the diff cannot be read the review fails
closed (``REVIEW_CONTEXT_ERROR``, persisted like every other review run); missing snippets only reduce context.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select

from hermclaw.contracts.review import ReviewContract
from hermclaw.contracts.step import StepContract
from hermclaw.contracts.verification import VerificationReport
from hermclaw.core.errors import HermclawError
from hermclaw.core.interfaces import GitReader, RepoContextProvider, RepoHit, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.persistence.models import CommandRun, Job
from hermclaw.review.reviewer import HeavyReviewer
from hermclaw.review.text import clip, one_line, redact
from hermclaw.review.types import REVIEW_CONTEXT_ERROR, CodeSnippet, ReviewInput, ReviewOutcome
from hermclaw.scope.guard import any_match

log = get_logger(__name__)

_TEST_FILE = re.compile(r"(^test_.*|.*_test\.[^/]+|.*\.(test|spec)\.[^/]+|.*_spec\.[^/]+)$", re.IGNORECASE)
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing"})
_QUERY_CHARS = 2_000


def is_test_path(path: str) -> bool:
    """Generic test-file heuristic (directory ``tests``/``__tests__``/``spec`` or ``test_*``/``*_test.*``/``*.spec.*``)."""
    parts = path.replace("\\", "/").strip("/").split("/")
    return any(p.lower() in _TEST_DIRS for p in parts[:-1]) or bool(_TEST_FILE.match(parts[-1]))


def _protected(path: str, globs: Sequence[str]) -> bool:
    if not globs:
        return False
    try:
        return any_match(path, list(globs))
    except ValueError:  # not a repository-relative path → never shown
        return True


class WorkspaceReviewer:
    """Collects the review input for a workspace and runs the heavy review (``ReviewerPort``)."""

    def __init__(self, reviewer: HeavyReviewer, git: GitReader, repo: RepoContextProvider | None = None) -> None:
        self.reviewer = reviewer
        self.git = git
        self.repo = repo

    def should_review(self, step_kind: str, verifier_passed: bool) -> bool:
        return self.reviewer.should_review(step_kind, verifier_passed)

    async def review(
        self,
        step: StepContract,
        workspace: WorkspaceHandle,
        verification: VerificationReport,
        *,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID | None,
    ) -> ReviewContract:
        """Effective (fail-closed) review contract – the summary carries the reason when the review errored."""
        outcome = await self.review_outcome(step, workspace, verification, job_id=job_id, step_id=step_id, attempt_id=attempt_id)
        return outcome.review

    async def review_outcome(
        self,
        step: StepContract,
        workspace: WorkspaceHandle,
        verification: VerificationReport,
        *,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID | None,
    ) -> ReviewOutcome:
        try:
            inp = await self.collect(step, workspace, verification, job_id=job_id, step_id=step_id, attempt_id=attempt_id)
        except Exception as exc:  # any collection failure fails closed (never a pass); CancelledError propagates
            detail = exc.message if isinstance(exc, HermclawError) else str(exc)
            reason = (
                f"fail-closed: the review input could not be collected ({type(exc).__name__}): "
                f"{clip(one_line(redact(detail)), 500) or 'no details'}"
            )
            return await self.reviewer.fail_closed(
                job_id=job_id,
                step_id=step_id,
                attempt_id=attempt_id,
                step_kind=step.kind.value,
                error_code=REVIEW_CONTEXT_ERROR,
                reason=reason,
            )
        return await self.reviewer.review(inp)

    async def collect(
        self,
        step: StepContract,
        workspace: WorkspaceHandle,
        verification: VerificationReport,
        *,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt_id: uuid.UUID | None,
    ) -> ReviewInput:
        settings = self.reviewer.settings
        async with asyncio.timeout(settings.context_timeout_seconds):
            goal = await self._goal(job_id)
            diff = await self.git.diff(workspace, max_bytes=settings.git_diff_max_bytes)
            changed = list(dict.fromkeys(await self.git.changed_files(workspace)))
            commands = await self._commands(step_id, attempt_id)
        snippets = await self._snippets(step, workspace, changed)
        return ReviewInput(
            job_id=job_id,
            step_id=step_id,
            attempt_id=attempt_id,
            goal=goal,
            step=step,
            diff=diff,
            verification=verification,
            snippets=snippets,
            command_log=commands,
            changed_files=changed,
        )

    # ------------------------------------------------------------------------------------------------ sources
    async def _goal(self, job_id: uuid.UUID) -> str:
        async with self.reviewer.sessionmaker() as session:
            row = (await session.execute(select(Job.title, Job.prompt).where(Job.id == job_id))).first()
        if row is None:
            return ""
        title, prompt = row
        return f"{title}\n\n{prompt}".strip()

    async def _commands(self, step_id: uuid.UUID, attempt_id: uuid.UUID | None) -> list[str]:
        limit = self.reviewer.settings.max_command_log
        cond = CommandRun.attempt_id == attempt_id if attempt_id is not None else CommandRun.step_id == step_id
        async with self.reviewer.sessionmaker() as session:
            rows = (
                await session.execute(
                    select(CommandRun.command, CommandRun.exit_code, CommandRun.timed_out, CommandRun.target)
                    .where(cond)
                    .order_by(CommandRun.created_at.desc())
                    .limit(limit)
                )
            ).all()
        out: list[str] = []
        for command, exit_code, timed_out, target in reversed(rows):
            status = "timeout" if timed_out else f"exit {exit_code if exit_code is not None else '?'}"
            out.append(f"[{status}] {target}: {clip(one_line(redact(command)), 600)}")
        return out

    async def _snippets(self, step: StepContract, workspace: WorkspaceHandle, changed: Sequence[str]) -> list[CodeSnippet]:
        if self.repo is None:
            return []
        settings = self.reviewer.settings
        protected = self.reviewer.config.policies.scope.always_forbidden
        query_parts = [step.title, step.goal, *(c.description for c in step.acceptance if getattr(c, "description", ""))]
        query_parts.append("changed files: " + ", ".join(changed[:30]))
        query = clip(one_line(" \n".join(p for p in query_parts if p)), _QUERY_CHARS)
        try:
            async with asyncio.timeout(settings.context_timeout_seconds):
                hits = await self.repo.context_for(workspace, query, budget_chars=settings.snippet_budget_chars)
        except Exception as exc:  # snippets are optional context: a failing provider only reduces the context
            log.warning("review snippets unavailable (%s): %s", type(exc).__name__, clip(redact(str(exc)), 300))
            return []
        return self._to_snippets(hits, protected)

    def _to_snippets(self, hits: Sequence[RepoHit], protected: Sequence[str]) -> list[CodeSnippet]:
        snippets: list[tuple[int, int, CodeSnippet]] = []
        seen: set[tuple[str, int]] = set()
        for idx, hit in enumerate(hits):
            path = hit.path.strip()
            if not path or not hit.snippet.strip() or _protected(path, protected):
                continue
            key = (path, hit.start_line)
            if key in seen:
                continue
            seen.add(key)
            kind: Literal["code", "test"] = "test" if is_test_path(path) else "code"
            snippet = CodeSnippet(
                path=path,
                content=hit.snippet,
                start_line=max(1, hit.start_line),
                end_line=hit.end_line if hit.end_line >= hit.start_line else None,
                kind=kind,
            )
            snippets.append((0 if kind == "test" else 1, idx, snippet))
        snippets.sort(key=lambda t: (t[0], t[1]))  # tests first (they show the expected behaviour), then rank
        return [s for _, _, s in snippets[: self.reviewer.settings.max_snippets]]
