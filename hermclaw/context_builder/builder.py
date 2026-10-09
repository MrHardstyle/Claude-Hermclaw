"""ContextBuilder – persistent state -> fresh, budgeted context per coder turn (Bauplan §18, Phase 16).

Never the whole chat, never the whole repository: every turn the builder collects the step contract, the scope,
the current repository facts, ranked code and test snippets, the current diff, the exact latest failure and a
compact tool history, fits each section into its fixed token budget and returns exactly two chat messages plus a
telemetry report. Output is deterministic for equal input (stable ordering everywhere, no clocks, no randomness).

Dependencies are the shared protocols only: :class:`RepoContextProvider` (``context_for``, ``search``, ``read``,
``inventory_summary``) and :class:`GitReader` (``status``, ``changed_files``, ``diff``). Provider failures and
timeouts never fail the build: the affected data is left out and a warning is recorded in the report.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any

from hermclaw.context_builder.budget import SectionBudgets, split_elastic
from hermclaw.context_builder.config import ContextBuilderConfig
from hermclaw.context_builder.diff import render_diff
from hermclaw.context_builder.failure import first_error_line
from hermclaw.context_builder.history import TurnRecord, render_history
from hermclaw.context_builder.render import (
    CorrectionItem,
    ToolPromptSpec,
    render_acceptance,
    render_completion,
    render_constraints,
    render_failure,
    render_goal,
    render_repo_facts,
    render_scope,
    render_tools,
)
from hermclaw.context_builder.report import BuiltContext, ContextReport, DroppedItem, SectionReport
from hermclaw.context_builder.sections import (
    MANDATORY_SECTIONS,
    MESSAGE_OVERHEAD_TOKENS,
    RESPONSE_PROTOCOL,
    SECTION_ORDER,
    RenderedSection,
    SectionName,
    render_message,
    user_closing,
)
from hermclaw.context_builder.snippets import Snippet, dedupe_snippets, fence_for, make_snippet, pack_snippets, render_packed
from hermclaw.context_builder.tokens import char_cost, clip_to_cost, estimate_tokens, tokens_for_cost
from hermclaw.contracts.scope import ScopeContract, normalise_path
from hermclaw.contracts.step import StepContract
from hermclaw.core.errors import HermclawError, ValidationFailed
from hermclaw.core.interfaces import GitReader, GitStatusEntry, RepoContextProvider, RepoHit, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.models.protocols import ChatMessage
from hermclaw.scope.engine import literal_path
from hermclaw.scope.expansion import is_test_path
from hermclaw.scope.guard import any_match

log = get_logger(__name__)

_PY_FRAME_RE = re.compile(r'File "(?P<path>[^"\n]+)", line (?P<line>\d+)')
_PATH_LINE_RE = re.compile(r"(?P<path>/?(?:[\w.\-]+/)*[\w\-][\w.\-]*\.[A-Za-z][A-Za-z0-9]{0,7}):(?P<line>\d+)")
_PYTEST_NODE_RE = re.compile(r"(?P<path>(?:[\w.\-]+/)*[\w\-][\w.\-]*\.[A-Za-z][A-Za-z0-9]{0,7})::[\w\[\]\-.]+")
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
_PACKAGE_STEMS = frozenset({"__init__", "index", "mod", "main"})
_REF_LABEL = "repo.read(failure-ref)"


# ---------------------------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class StepBrief:
    """The step-contract data a turn needs (built from ``StepContract`` or given directly)."""

    goal: str
    kind: str
    title: str = ""
    step_key: str = ""
    constraints: Sequence[str] = ()
    acceptance: Sequence[Any] = ()  # AcceptanceCriterion models (or equivalent mappings)
    scope: ScopeContract | None = None
    repo_hints: Sequence[str] = ()

    @classmethod
    def from_contract(cls, step: StepContract) -> StepBrief:
        return cls(
            goal=step.goal,
            kind=str(step.kind),
            title=step.title,
            step_key=step.step_key,
            constraints=tuple(step.constraints),
            acceptance=tuple(step.acceptance),
            scope=step.scope,
            repo_hints=tuple(step.repo_hints),
        )


@dataclass(frozen=True)
class TurnContextInput:
    step: StepBrief
    workspace: WorkspaceHandle
    turn: int
    max_turns: int
    tools: Sequence[ToolPromptSpec | Mapping[str, Any]] = ()  # {name, description, args_schema} specs
    completion_contract: str = ""
    history: Sequence[TurnRecord] = ()
    latest_failure: str | None = None  # exact text of the latest failing tool result / test output
    correction: Sequence[CorrectionItem] = ()  # verifier failures / review findings of the previous attempt

    def __post_init__(self) -> None:
        if self.turn < 1 or self.max_turns < 1:
            raise ValidationFailed("turn and max_turns must be >= 1", details={"turn": self.turn, "max_turns": self.max_turns})
        if not self.step.goal.strip():
            raise ValidationFailed("step goal must not be empty")


@dataclass(frozen=True)
class _FileRef:
    path: str
    line: int | None


# ---------------------------------------------------------------------------------------------- builder
class ContextBuilder:
    def __init__(
        self,
        repo: RepoContextProvider,
        git: GitReader,
        config: ContextBuilderConfig | None = None,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self.repo = repo
        self.git = git
        self.config = config or ContextBuilderConfig()
        self.plan = self.config.plan()
        self._redactor = redactor or DEFAULT_REDACTOR

    # ------------------------------------------------------------------ helpers
    def _red(self, text: str) -> str:
        return self._redactor.text(text) if text else text

    def _excluded(self, path: str) -> bool:
        return any_match(path, list(self.config.exclude_globs))

    def _clean_path(self, raw: str, workspace: WorkspaceHandle) -> str | None:
        """Repository-relative, normalised path or ``None`` (absolute outside the workspace, '..', excluded)."""
        p = raw.strip().replace("\\", "/")
        if p.startswith("/"):
            root = str(workspace.path).replace("\\", "/").rstrip("/") + "/"
            if not p.startswith(root):
                return None
            p = p[len(root) :]
        while p.startswith("./"):
            p = p[2:]
        try:
            p = normalise_path(p)
        except ValueError:
            return None
        if p.endswith("/") or self._excluded(p):
            return None
        return p

    async def _call[T](self, sem: asyncio.Semaphore, label: str, factory: Callable[[], Awaitable[T]], default: T) -> tuple[T, str | None]:
        async with sem:
            try:
                return await asyncio.wait_for(factory(), timeout=self.config.provider_timeout_seconds), None
            except TimeoutError:
                return default, f"{label}: timeout after {self.config.provider_timeout_seconds:g}s"
            except Exception as exc:  # provider errors never fail the turn
                return default, f"{label}: {type(exc).__name__}: {self._red(str(exc))[:200]}"

    # ------------------------------------------------------------------ public API
    async def build(self, inp: TurnContextInput) -> BuiltContext:
        cfg, plan, ws, step = self.config, self.plan, inp.workspace, inp.step
        sem = asyncio.Semaphore(cfg.max_concurrency)
        warnings: list[str] = []
        dropped: list[DroppedItem] = []

        failure = self._red(inp.latest_failure or "")
        correction = [
            replace(c, message=self._red(c.message), suggested_fix=self._red(c.suggested_fix), path=self._red(c.path))
            for c in inp.correction
        ]

        # ---- relevance inputs (16.3 / 16.8)
        targets, target_drops = self._literal_targets(step.scope, ws)
        dropped.extend(DroppedItem(SectionName.RELEVANT_CODE.value, p, r) for p, r in target_drops)
        refs = self._failure_refs(failure, correction, ws)
        acc_tests = self._acceptance_test_paths(step.acceptance, ws)
        queries = self._test_queries(targets, step.goal)
        query = f"{step.title}\n{step.goal}".strip()
        first_err = first_error_line(failure) if failure.strip() else ""
        if first_err:
            query += "\n" + first_err[:300]
        elastic_chars = plan.section_cost[SectionName.RELEVANT_CODE] + plan.section_cost[SectionName.RELEVANT_TESTS]

        # ---- phase 1: independent provider calls (concurrent, bounded, order-preserving)
        def read(path: str, start: int, end: int | None) -> Callable[[], Awaitable[str]]:
            return lambda: self.repo.read(ws, path, start, end, max_chars=cfg.read_max_chars)

        head_targets = [p for p in targets if not is_test_path(p)] + [p for p in targets if is_test_path(p)]
        file_heads = list(dict.fromkeys([*head_targets, *(p for p in acc_tests if p not in targets)]))
        jobs: list[tuple[str, Callable[[], Awaitable[Any]], Any]] = [
            ("repo.inventory_summary", lambda: self.repo.inventory_summary(ws), None),
            ("git.status", lambda: self.git.status(ws), []),
            ("git.changed_files", lambda: self.git.changed_files(ws), []),
            ("git.diff", lambda: self.git.diff(ws, None, max_bytes=cfg.diff_max_bytes), ""),
            ("repo.context_for", lambda: self.repo.context_for(ws, query, budget_chars=max(2000, elastic_chars)), []),
        ]
        for p in file_heads:
            lines = cfg.test_head_lines if is_test_path(p) else cfg.target_head_lines
            jobs.append((f"repo.read {p}", read(p, 1, lines), None))
        for ref in refs:
            if ref.line is None:
                jobs.append((f"{_REF_LABEL} {ref.path}", read(ref.path, 1, cfg.test_head_lines), None))
            else:
                lo = max(1, ref.line - cfg.failure_region_lines)
                jobs.append((f"{_REF_LABEL} {ref.path}", read(ref.path, lo, ref.line + cfg.failure_region_lines), None))
        for q in queries:
            jobs.append((f"repo.search {q}", self._search(ws, q), []))
        results = await asyncio.gather(*(self._call(sem, label, factory, default) for label, factory, default in jobs))
        for (label, _, _), (_, warning) in zip(jobs, results, strict=True):
            if warning and not label.startswith(_REF_LABEL):  # failure refs may name files outside the repo
                warnings.append(warning)
        values = [v for v, _ in results]
        inventory, status, changed, diff, hits = values[:5]
        pos = 5
        head_texts = dict(zip(file_heads, values[pos : pos + len(file_heads)], strict=True))
        pos += len(file_heads)
        ref_texts = values[pos : pos + len(refs)]
        pos += len(refs)
        search_results: list[list[RepoHit]] = values[pos:]

        # ---- candidate snippets
        candidates: list[Snippet] = []
        for i, ref in enumerate(refs):
            text = ref_texts[i]
            if not isinstance(text, str) or not text.strip():
                dropped.append(DroppedItem(_section_for(ref.path).value, ref.path, "read_failed"))
                continue
            start = 1 if ref.line is None else max(1, ref.line - cfg.failure_region_lines)
            candidates.append(make_snippet(ref.path, start, text, 3.0 - i * 0.001, "failure"))
        for i, p in enumerate(file_heads):
            text = head_texts.get(p)
            if not isinstance(text, str) or not text.strip():
                dropped.append(DroppedItem(_section_for(p).value, p, "read_failed"))
                continue
            origin = "target" if p in targets else "acceptance"
            candidates.append(make_snippet(p, 1, text, 2.0 - i * 0.001, origin))
        target_set = set(targets)
        hit_cands, hit_drops = await self._hit_snippets(
            sem, ws, _as_hits(hits), "context", base=0.0, boost=target_set, warnings=warnings, tests_only=False
        )
        candidates.extend(hit_cands)
        dropped.extend(hit_drops)
        for q_hits in search_results:
            s_cands, s_drops = await self._hit_snippets(
                sem, ws, _as_hits(q_hits), "search", base=1.0, boost=set(), warnings=warnings, tests_only=True
            )
            candidates.extend(s_cands)
            dropped.extend(s_drops)

        async def reader(path: str, start: int, end: int) -> str | None:
            text, warning = await self._call(sem, f"repo.read {path}", read(path, start, end), None)
            if warning:
                warnings.append(warning)
            return text if isinstance(text, str) else None

        code_cands = [c for c in candidates if not is_test_path(c.path)]
        test_cands = [c for c in candidates if is_test_path(c.path)]
        code_d = await dedupe_snippets(code_cands, merge_gap=cfg.merge_gap_lines, reader=reader, max_lines=cfg.max_snippet_lines)
        test_d = await dedupe_snippets(test_cands, merge_gap=cfg.merge_gap_lines, reader=reader, max_lines=cfg.max_snippet_lines)
        dropped.extend(DroppedItem(SectionName.RELEVANT_CODE.value, lbl, r) for lbl, r in code_d.dropped)
        dropped.extend(DroppedItem(SectionName.RELEVANT_TESTS.value, lbl, r) for lbl, r in test_d.dropped)
        code_snips = [self._redact_snippet(s) for s in code_d.snippets]
        test_snips = [self._redact_snippet(s) for s in test_d.snippets]

        # ---- fixed-budget sections
        cost = plan.section_cost
        rendered: dict[SectionName, RenderedSection] = {}
        system = RenderedSection(SectionName.SYSTEM_CONTRACT, body=cfg.system_contract.strip(), items_included=1)
        rendered[SectionName.SYSTEM_CONTRACT] = system
        rendered[SectionName.STEP_GOAL] = render_goal(
            goal=self._red(step.goal),
            kind=step.kind,
            title=self._red(step.title),
            step_key=step.step_key,
            repo_hints=[self._red(h) for h in step.repo_hints],
            turn=inp.turn,
            max_turns=inp.max_turns,
            budget=cost[SectionName.STEP_GOAL],
        )
        rendered[SectionName.SCOPE] = render_scope(step.scope, cfg.exclude_globs, cost[SectionName.SCOPE], max_items=cfg.max_list_items)
        rendered[SectionName.CONSTRAINTS] = render_constraints([self._red(c) for c in step.constraints], cost[SectionName.CONSTRAINTS])
        rendered[SectionName.ACCEPTANCE] = render_acceptance(list(step.acceptance), cost[SectionName.ACCEPTANCE])
        for name in (SectionName.SCOPE, SectionName.ACCEPTANCE):
            self._post_redact(rendered[name], cost[name])
        rendered[SectionName.CURRENT_REPO_FACTS] = render_repo_facts(
            workspace=ws,
            inventory=self._red_obj(inventory) if isinstance(inventory, Mapping) else None,
            status=[e for e in _as_status(status) if not self._excluded_raw(e.path)],
            changed=[p for p in _as_paths(changed) if not self._excluded_raw(p)],
            budget=cost[SectionName.CURRENT_REPO_FACTS],
            max_items=cfg.max_list_items,
        )
        rendered[SectionName.CURRENT_DIFF] = self._render_diff(diff if isinstance(diff, str) else "", cost[SectionName.CURRENT_DIFF])
        rendered[SectionName.LATEST_FAILURE] = render_failure(
            failure, correction, cost[SectionName.LATEST_FAILURE], item_chars=cfg.correction_item_chars
        )
        records = [
            replace(r, args_digest=self._red(r.args_digest), result_digest=self._red(r.result_digest), tool=self._red(r.tool))
            for r in inp.history
        ]
        hist = render_history(records, cost[SectionName.SHORT_STEP_HISTORY], cfg.history)
        hist_sec = RenderedSection(SectionName.SHORT_STEP_HISTORY, body=hist.body, truncated=hist.truncated)
        hist_sec.items_included = hist.full_turns + hist.summarised_turns
        if not inp.history:
            hist_sec.omitted_reason = "empty"
        rendered[SectionName.SHORT_STEP_HISTORY] = hist_sec
        tools = [t if isinstance(t, ToolPromptSpec) else ToolPromptSpec.from_mapping(t) for t in inp.tools]
        rendered[SectionName.AVAILABLE_TOOLS] = render_tools(tools, cost[SectionName.AVAILABLE_TOOLS])
        rendered[SectionName.COMPLETION_CONDITIONS] = render_completion(
            self._red(inp.completion_contract), turn=inp.turn, max_turns=inp.max_turns, budget=cost[SectionName.COMPLETION_CONDITIONS]
        )

        # ---- elastic sections with redistributed budget (16.2)
        used = {name: char_cost(sec.body) for name, sec in rendered.items() if sec.present}
        split = split_elastic(plan, cfg.budgets, used)
        # unused code budget -> tests; then unused tests budget -> code (only if code had to leave snippets out).
        # The reported section budget is what the section was allowed when it was (last) packed.
        code_allowed = split.code_cost
        code_pack = pack_snippets(code_snips, code_allowed, min_cost=cfg.min_snippet_cost, max_items=cfg.max_code_snippets)
        tests_allowed = split.tests_cost + (code_allowed - code_pack.cost)
        test_pack = pack_snippets(test_snips, tests_allowed, min_cost=cfg.min_snippet_cost, max_items=cfg.max_test_snippets)
        spare = tests_allowed - test_pack.cost
        if spare > 0 and any(reason == "budget" for _, reason in code_pack.dropped):
            code_allowed = code_pack.cost + spare
            code_pack = pack_snippets(code_snips, code_allowed, min_cost=cfg.min_snippet_cost, max_items=cfg.max_code_snippets)
        final_budget = dict(cost)
        final_budget[SectionName.RELEVANT_CODE] = code_allowed
        final_budget[SectionName.RELEVANT_TESTS] = tests_allowed
        for name, pack, total in (
            (SectionName.RELEVANT_CODE, code_pack, len(code_snips)),
            (SectionName.RELEVANT_TESTS, test_pack, len(test_snips)),
        ):
            sec = RenderedSection(name, body=render_packed(pack.selected), truncated=pack.truncated, items_included=len(pack.selected))
            for label, reason in pack.dropped:
                sec.drop(label, reason)
            if not total:
                sec.omitted_reason = "empty"
            elif not pack.selected:
                sec.omitted_reason = "budget"
            rendered[name] = sec

        # ---- messages
        for name, sec in rendered.items():
            dropped.extend(DroppedItem(name.value, item, reason) for item, reason in sec.dropped)
        system_msg = render_message(
            [rendered[n] for n in (SectionName.SYSTEM_CONTRACT, SectionName.AVAILABLE_TOOLS, SectionName.COMPLETION_CONDITIONS)],
            suffix=RESPONSE_PROTOCOL,
        )
        user_order = [
            n
            for n in SECTION_ORDER
            if n not in (SectionName.SYSTEM_CONTRACT, SectionName.AVAILABLE_TOOLS, SectionName.COMPLETION_CONDITIONS)
        ]
        user_msg = render_message([rendered[n] for n in user_order], suffix=user_closing(inp.turn, inp.max_turns))
        estimated = estimate_tokens(system_msg) + estimate_tokens(user_msg) + 2 * MESSAGE_OVERHEAD_TOKENS
        if estimated > plan.total_tokens:  # pragma: no cover - budgets are disjoint; kept as a hard guarantee
            raise HermclawError(f"context budget exceeded: {estimated} > {plan.total_tokens} tokens", code="CONTEXT_BUDGET_EXCEEDED")

        sections = [
            SectionReport(
                name=n.value,
                present=rendered[n].present,
                budget_tokens=tokens_for_cost(final_budget[n]) if final_budget[n] > 0 else 0,
                estimated_tokens=estimate_tokens(rendered[n].body),
                chars=len(rendered[n].body),
                truncated=rendered[n].truncated,
                items_included=rendered[n].items_included,
                items_dropped=rendered[n].items_dropped,
                omitted_reason=None
                if rendered[n].present
                else (rendered[n].omitted_reason or ("empty" if n not in MANDATORY_SECTIONS else None)),
            )
            for n in SECTION_ORDER
        ]
        fingerprint = hashlib.sha256((system_msg + "\x00" + user_msg).encode("utf-8", errors="surrogateescape")).hexdigest()
        report = ContextReport(
            turn=inp.turn,
            max_turns=inp.max_turns,
            context_tokens=plan.context_tokens,
            max_output_tokens=plan.max_output_tokens,
            safety_margin_tokens=plan.safety_margin_tokens,
            total_budget_tokens=plan.total_tokens,
            framing_tokens=plan.framing_tokens,
            estimated_prompt_tokens=estimated,
            redistributed_tokens=tokens_for_cost(split.redistributed_cost) if split.redistributed_cost > 0 else 0,
            sections=sections,
            dropped=dropped,
            merged_snippets=code_d.merged + test_d.merged,
            warnings=warnings,
            fingerprint=fingerprint,
        )
        if warnings:
            log.info("context built with provider warnings", extra={"warnings": warnings[:5], "turn": inp.turn})
        return BuiltContext(messages=[ChatMessage("system", system_msg), ChatMessage("user", user_msg)], report=report)

    # ------------------------------------------------------------------ relevance helpers
    def _excluded_raw(self, path: str) -> bool:
        try:
            return self._excluded(normalise_path(path))
        except ValueError:
            return True

    def _post_redact(self, sec: RenderedSection, budget: int) -> None:
        """Redact a rendered body; if the mask made it longer than its budget, clip it (marked with …)."""
        red_body = self._red(sec.body)
        if red_body == sec.body:
            return
        if char_cost(red_body) > budget:
            red_body = clip_to_cost(red_body, max(0, budget - 1)) + "…"
            sec.truncated = True
        sec.body = red_body

    def _red_obj(self, value: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = self._redactor.obj(dict(value))
        return out

    def _redact_snippet(self, s: Snippet) -> Snippet:
        text = self._red(s.text)
        if text == s.text:
            return s
        exact = s.exact and len(text.splitlines()) == len(s.text.splitlines())
        return replace(s, text=text, exact=exact)

    def _literal_targets(self, scope: ScopeContract | None, ws: WorkspaceHandle) -> tuple[list[str], list[tuple[str, str]]]:
        if scope is None:
            return [], []
        out: list[str] = []
        drops: list[tuple[str, str]] = []
        for pattern in scope.target_paths:
            lit = literal_path(pattern)
            if lit is None:
                continue  # globs/directories: covered by repository relevance, not read blindly
            p = self._clean_path(lit, ws)
            if p is None:
                drops.append((lit, "excluded"))
                continue
            if p in out:
                continue
            if len(out) >= self.config.max_target_files:
                drops.append((p, "limit"))
                continue
            out.append(p)
        return out, drops

    def _failure_refs(self, failure: str, correction: Sequence[CorrectionItem], ws: WorkspaceHandle) -> list[_FileRef]:
        texts = [failure, *(c.message for c in correction)]
        refs: list[_FileRef] = []
        seen: set[tuple[str, int | None]] = set()
        for text in texts:
            if not text:
                continue
            found: list[tuple[int, str, int | None]] = []
            for m in _PY_FRAME_RE.finditer(text):
                found.append((m.start(), m.group("path"), int(m.group("line"))))
            for m in _PATH_LINE_RE.finditer(text):
                found.append((m.start(), m.group("path"), int(m.group("line"))))
            for m in _PYTEST_NODE_RE.finditer(text):
                found.append((m.start(), m.group("path"), None))
            for _, raw, line in sorted(found, key=lambda t: (t[0], t[1], t[2] or 0)):
                if self._add_ref(refs, seen, raw, line, ws):
                    return refs
        for c in correction:  # findings name a file without a line: show its head
            if c.path and self._add_ref(refs, seen, c.path, None, ws):
                return refs
        return refs

    def _add_ref(self, refs: list[_FileRef], seen: set[tuple[str, int | None]], raw: str, line: int | None, ws: WorkspaceHandle) -> bool:
        """Append a cleaned reference; returns True when the reference limit is reached."""
        p = self._clean_path(raw, ws)
        if p is None or line == 0 or (p, line) in seen:
            return False
        seen.add((p, line))
        refs.append(_FileRef(p, line))
        return len(refs) >= self.config.max_failure_refs

    def _acceptance_test_paths(self, acceptance: Sequence[Any], ws: WorkspaceHandle) -> list[str]:
        out: list[str] = []
        for c in acceptance:
            command = getattr(c, "command", None) if not isinstance(c, Mapping) else c.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
            try:
                tokens = shlex.split(command)
            except ValueError:
                tokens = command.split()
            for tok in tokens:
                raw = tok.split("::", 1)[0]
                if "/" not in raw and "." not in raw:
                    continue
                if not PurePosixPath(raw).suffix:
                    continue
                p = self._clean_path(raw, ws)
                if p is None or not is_test_path(p) or p in out:
                    continue
                out.append(p)
                if len(out) >= self.config.max_acceptance_test_files:
                    return out
        return out

    def _test_queries(self, targets: Sequence[str], goal: str) -> list[str]:
        queries: list[str] = []
        for p in targets:
            if is_test_path(p):
                continue
            pp = PurePosixPath(p)
            stem = pp.stem
            if stem in _PACKAGE_STEMS and pp.parent.name:
                stem = pp.parent.name
            if len(stem) >= 3 and stem not in queries:
                queries.append(stem)
        for m in _IDENT_RE.finditer(goal):
            ident = m.group(0)
            follows_call = goal[m.end() : m.end() + 1] == "("
            code_like = "_" in ident.strip("_") or re.search(r"[a-z][A-Z]", ident) is not None or follows_call
            if code_like and ident not in queries:
                queries.append(ident)
        return queries[: self.config.max_test_queries]

    def _search(self, ws: WorkspaceHandle, query: str) -> Callable[[], Awaitable[list[RepoHit]]]:
        return lambda: self.repo.search(ws, query, k=self.config.search_k)

    async def _hit_snippets(
        self,
        sem: asyncio.Semaphore,
        ws: WorkspaceHandle,
        hits: Sequence[RepoHit],
        origin: str,
        *,
        base: float,
        boost: set[str],
        warnings: list[str],
        tests_only: bool,
    ) -> tuple[list[Snippet], list[DroppedItem]]:
        cfg = self.config
        drops: list[DroppedItem] = []
        max_score = max((_score(h) for h in hits if _score(h) > 0), default=1.0)
        prepared: list[tuple[RepoHit, str, int, int, float]] = []
        for h in hits:
            p = self._clean_path(h.path, ws)
            if p is None:
                drops.append(DroppedItem(_section_for(h.path).value, h.path[:200], "excluded"))
                continue
            if tests_only and not is_test_path(p):
                continue
            start = max(1, _int(h.start_line, 1))
            end = max(start, _int(h.end_line, start))
            end = min(end, start + cfg.max_snippet_lines - 1)
            norm = _score(h) / max_score
            score = base + norm + (1.0 if p in boost else 0.0)
            prepared.append((h, p, start, end, score))

        # the index may be stale (the worker edits files every turn): ranges are always read fresh from the workspace;
        # the provider's snippet is only a fallback and then never merged line-wise (exact=False)
        prepared.sort(key=lambda t: (-t[4], t[1], t[2], t[3]))
        for _h, p, start, end, _ in prepared[cfg.max_hits_per_query :]:
            drops.append(DroppedItem(_section_for(p).value, f"{p}:{start}-{end}", "limit"))
        prepared = prepared[: cfg.max_hits_per_query]
        reads = await asyncio.gather(
            *(
                self._call(sem, f"repo.read {p}", _reader(self.repo, ws, p, start, end, max_chars=cfg.read_max_chars), None)
                for _, p, start, end, _ in prepared
            )
        )
        out: list[Snippet] = []
        for (h, p, start, end, score), (text, warning) in zip(prepared, reads, strict=True):
            if warning:
                warnings.append(warning)
            if isinstance(text, str) and text.strip():
                out.append(make_snippet(p, start, text, score, origin))
            elif isinstance(h.snippet, str) and h.snippet.strip():
                out.append(Snippet(p, start, end, h.snippet, score, (origin,), exact=False))
            else:
                drops.append(DroppedItem(_section_for(p).value, f"{p}:{start}-{end}", "read_failed"))
        return out, drops

    def _render_diff(self, diff: str, budget: int) -> RenderedSection:
        sec = RenderedSection(SectionName.CURRENT_DIFF)
        text = self._red(diff)
        if not text.strip():
            sec.omitted_reason = "empty"
            return sec
        fence = fence_for(text)  # fenced: diff content can never pose as a section heading
        wrap = 2 * len(fence) + len("diff") + 2
        inner = budget - wrap if budget > wrap + 40 else budget
        dr = render_diff(text, inner, min_file_cost=self.config.min_diff_file_cost, exclude=self._excluded_raw)
        sec.body = f"{fence}diff\n{dr.body}\n{fence}" if dr.body and inner != budget else dr.body
        sec.truncated = dr.truncated
        sec.items_included = dr.files_shown
        for item, reason in dr.dropped:
            sec.drop(item, reason)
        return sec


def _reader(
    repo: RepoContextProvider, ws: WorkspaceHandle, path: str, start: int, end: int, *, max_chars: int
) -> Callable[[], Awaitable[str]]:
    return lambda: repo.read(ws, path, start, end, max_chars=max_chars)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _score(hit: RepoHit) -> float:
    """Finite, non-negative provider score (NaN/inf/negative/non-numeric -> 0) so that ranking stays total."""
    try:
        value = float(hit.score)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _section_for(path: str) -> SectionName:
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return SectionName.RELEVANT_TESTS if is_test_path(p) else SectionName.RELEVANT_CODE


def _as_hits(value: Any) -> list[RepoHit]:
    if not isinstance(value, list | tuple):
        return []
    return [h for h in value if isinstance(h, RepoHit) and isinstance(h.path, str) and h.path.strip()]


def _as_status(value: Any) -> list[GitStatusEntry]:
    return [e for e in value if isinstance(e, GitStatusEntry)] if isinstance(value, list | tuple) else []


def _as_paths(value: Any) -> list[str]:
    return [p for p in value if isinstance(p, str) and p.strip()] if isinstance(value, list | tuple) else []


__all__ = ["ContextBuilder", "SectionBudgets", "StepBrief", "TurnContextInput"]
