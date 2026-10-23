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
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select

from hermclaw.contracts.review import ReviewContract
from hermclaw.contracts.step import StepContract
from hermclaw.contracts.tools import OPS_TOOLS
from hermclaw.contracts.verification import VerificationReport
from hermclaw.core.errors import HermclawError, ResourceUnavailable
from hermclaw.core.interfaces import GitReader, RepoContextProvider, RepoHit, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.memory.store import MemoryStore
from hermclaw.models.gateway import MODEL_LOAD_FAILED, MODEL_UNAVAILABLE
from hermclaw.persistence.models import CommandRun, Job, ToolCallRow
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


_DOC_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".adoc")
_DOC_MAX_BYTES = 64_000


def requirement_doc_paths(step: StepContract, goal: str) -> list[str]:
    """Documentation files among the step's ``repo_hints`` that the job or the step goal names (file name or stem,
    e.g. "die Regeln im README"): the job's rules live there, not in the goal text (BUG-290)."""
    text = f"{goal}\n{step.title}\n{step.goal}".lower()
    out: list[str] = []
    for hint in step.repo_hints:
        path = hint.strip().removeprefix("./")
        name = path.rsplit("/", 1)[-1].lower()
        stem = name.rsplit(".", 1)[0]
        if not name.endswith(_DOC_SUFFIXES) or any(c in path for c in "*?["):
            continue
        if name in text or (len(stem) >= 3 and re.search(rf"(?<![\w.-]){re.escape(stem)}(?![\w-])", text)):
            out.append(path)
    return list(dict.fromkeys(out))


def _protected(path: str, globs: Sequence[str]) -> bool:
    if not globs:
        return False
    try:
        return any_match(path, list(globs))
    except ValueError:  # not a repository-relative path → never shown
        return True


#: review errors that say nothing about the change: the model host is down
_HOST_DOWN = frozenset({MODEL_UNAVAILABLE, MODEL_LOAD_FAILED})


class WorkspaceReviewer:
    """Collects the review input for a workspace and runs the heavy review (``ReviewerPort``)."""

    def __init__(
        self, reviewer: HeavyReviewer, git: GitReader, repo: RepoContextProvider | None = None, memory: MemoryStore | None = None
    ) -> None:
        self.reviewer = reviewer
        self.git = git
        self.repo = repo
        self.memory = memory

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
        earlier: Sequence[str] = (),
    ) -> ReviewContract:
        """Effective (fail-closed) review contract – the summary carries the reason when the review errored.

        An unreachable model host is no verdict about the change (BUG-198): it raises :class:`ResourceUnavailable`, so
        the step attempt is retried once the host is back instead of costing a correction round or a replan. It never
        passes either."""
        outcome = await self.review_outcome(
            step, workspace, verification, job_id=job_id, step_id=step_id, attempt_id=attempt_id, earlier=earlier
        )
        if outcome.fail_closed and outcome.error_code in _HOST_DOWN:
            raise ResourceUnavailable(f"heavy review: model host unavailable ({outcome.error_code}): {outcome.reason[:300]}")
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
        earlier: Sequence[str] = (),
    ) -> ReviewOutcome:
        try:
            inp = await self.collect(step, workspace, verification, job_id=job_id, step_id=step_id, attempt_id=attempt_id, earlier=earlier)
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
        earlier: Sequence[str] = (),
    ) -> ReviewInput:
        settings = self.reviewer.settings
        async with asyncio.timeout(settings.context_timeout_seconds):
            goal = await self._goal(job_id)
            diff = await self.git.diff(workspace, max_bytes=settings.git_diff_max_bytes)
            changed = list(dict.fromkeys(await self.git.changed_files(workspace)))
            commands = await self._commands(step_id, attempt_id)
        snippets = await self._snippets(step, workspace, changed)
        docs = await asyncio.to_thread(self._requirement_docs, step, workspace, goal)
        conventions = await self._conventions(diff, workspace)
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
            earlier_findings=tuple(earlier),
            requirement_docs=docs,
            conventions=conventions,
        )

    async def _conventions(self, diff: str, workspace: WorkspaceHandle) -> list[str]:
        """Active lessons and conventions whose code patterns match a line the diff adds (D-027); never fails the
        review."""
        policy = self.reviewer.config.policies.memory
        if self.memory is None or not diff or not policy.enabled or policy.coder_entries <= 0:
            return []
        added = "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
        try:
            entries = await self.memory.code_entries(scopes=[f"repo:{workspace.repository_key}"])
        except Exception as exc:
            log.warning("knowledge for the review unavailable", extra={"error_type": type(exc).__name__})
            return []
        hits = [h for h, patterns in entries if any(p.search(added) for p in patterns)]
        return [h.render(policy.max_entry_chars) for h in hits[: policy.coder_entries]]

    def _requirement_docs(self, step: StepContract, workspace: WorkspaceHandle, goal: str) -> list[tuple[str, str]]:
        root = Path(workspace.path).resolve()
        protected = self.reviewer.config.policies.scope.always_forbidden
        out: list[tuple[str, str]] = []
        for rel in requirement_doc_paths(step, goal):
            target = (root / rel).resolve()
            if _protected(rel, protected) or not target.is_relative_to(root) or not target.is_file():
                continue
            with target.open("rb") as fh:
                out.append((rel, fh.read(_DOC_MAX_BYTES).decode("utf-8", errors="replace")))
        return out

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
                    select(CommandRun.command, CommandRun.exit_code, CommandRun.timed_out, CommandRun.target, CommandRun.created_at)
                    .where(cond)
                    .order_by(CommandRun.created_at.desc())
                    .limit(limit)
                )
            ).all()
        out: list[tuple[Any, str]] = []
        for command, exit_code, timed_out, target, created in rows:
            status = "timeout" if timed_out else f"exit {exit_code if exit_code is not None else '?'}"
            out.append((created, f"[{status}] {target}: {clip(one_line(redact(command)), 600)}"))
        # operations tools (SSH/DB/container) are the executed-command evidence of remote steps (ssh/deploy)
        tcond = ToolCallRow.attempt_id == attempt_id if attempt_id is not None else ToolCallRow.step_id == step_id
        async with self.reviewer.sessionmaker() as session:
            ops = (
                await session.execute(
                    select(ToolCallRow.tool, ToolCallRow.arguments, ToolCallRow.status, ToolCallRow.result_summary, ToolCallRow.started_at)
                    .where(tcond, ToolCallRow.tool.in_([t.value for t in OPS_TOOLS]), ToolCallRow.status != "running")
                    .order_by(ToolCallRow.started_at.desc())
                    .limit(limit)
                )
            ).all()
        for tool, arguments, status, summary, started in ops:
            args = " ".join(f"{k}={v}" for k, v in sorted((arguments or {}).items()) if v is not None)
            result = f" → {clip(one_line(redact(summary)), 300)}" if summary else ""
            out.append((started, f"[{status}] ops {tool}: {clip(one_line(redact(args)), 400)}{result}"))
        out.sort(key=lambda item: item[0])
        return [text for _, text in out[-limit:]]

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
