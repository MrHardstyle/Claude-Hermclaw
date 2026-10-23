"""Renderers for the small, fixed-budget sections (16.1): goal, scope, constraints, acceptance, repo facts,
latest failure (+ correction evidence), tools and completion conditions. Every renderer is pure, deterministic
and returns a body whose cost never exceeds the given budget."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from hermclaw.context_builder.failure import clip_line, preserve_failure, truncate_middle
from hermclaw.context_builder.history import strip_reasoning
from hermclaw.context_builder.sections import RenderedSection, SectionName
from hermclaw.context_builder.snippets import fence_for
from hermclaw.context_builder.tokens import char_cost, clip_to_cost
from hermclaw.contracts.review import ReviewContract
from hermclaw.contracts.scope import ScopeContract
from hermclaw.contracts.verification import VerificationReport
from hermclaw.core.interfaces import GitStatusEntry, WorkspaceHandle
from hermclaw.scope.engine import literal_path
from hermclaw.scope.guard import acceptance_glob_matches


# ---------------------------------------------------------------------------------------------- input types
@dataclass(frozen=True)
class ToolPromptSpec:
    name: str
    description: str = ""
    args_schema: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ToolPromptSpec:
        """Accepts ``{name, description, args_schema}`` as well as ``parameters`` / ``input_schema`` spellings."""
        schema = data.get("args_schema") or data.get("parameters") or data.get("input_schema") or {}
        return cls(name=str(data.get("name", "")), description=str(data.get("description", "") or ""), args_schema=dict(schema))


_SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}


@dataclass(frozen=True)
class CorrectionItem:
    """One piece of evidence the current attempt must address (verifier failure or review finding)."""

    source: str  # verifier | review | runtime
    label: str  # check type/name or finding severity
    message: str
    path: str = ""
    suggested_fix: str = ""
    command: str = ""  # the command of a failed lint/test check, re-run before the attempt may complete (BUG-276)

    @classmethod
    def from_verification(cls, report: VerificationReport, evidence_chars: int = 800) -> list[CorrectionItem]:
        """One item per failed check with its evidence excerpt: a message like "lint reported problems (exit code 1)"
        alone never told the coder which lines to fix, and it fixed none in four attempts (live G1, BUG-247)."""
        from hermclaw.review.correction import evidence_excerpt, recheck_command  # the review package builds on this module's types

        items = []
        for c in report.failures:
            message = c.message or c.status
            excerpt = evidence_excerpt({k: v for k, v in c.evidence.items() if k != "path"}, evidence_chars)
            if excerpt:
                message += f" — evidence: {excerpt}"
            items.append(
                cls(
                    source="verifier",
                    label=f"{c.check_type}:{c.name}",
                    message=message,
                    path=str(c.evidence.get("path", "")),
                    command=recheck_command(c.check_type, c.evidence),
                )
            )
        return items

    @classmethod
    def from_review(cls, review: ReviewContract) -> list[CorrectionItem]:
        findings = sorted(enumerate(review.findings), key=lambda t: (_SEVERITY_ORDER.get(str(t[1].severity), 9), t[0]))
        return [
            cls(
                source="review",
                label=str(f.severity),
                message=f.summary + (f" — evidence: {f.evidence}" if f.evidence else ""),
                path=f.path,
                suggested_fix=f.suggested_fix,
            )
            for _, f in findings
        ]


# ---------------------------------------------------------------------------------------------- helpers
def fit_lines(section: RenderedSection, lines: Sequence[str], budget: int, *, what: str = "items") -> None:
    """Append ``lines`` in order while they fit; then a ``(+N more … not shown)`` note if room remains."""
    out: list[str] = []
    used = 0
    noted = False
    for i, line in enumerate(lines):
        c = char_cost(line) + (1 if out else 0)
        if used + c <= budget:
            out.append(line)
            used += c
            continue
        dropped = list(lines[i:])
        while True:
            note = f"(+{len(dropped)} more {what} not shown)"
            note_cost = char_cost(note) + (1 if out else 0)
            if used + note_cost <= budget or not out:
                break
            removed = out.pop()
            used -= char_cost(removed) + (1 if out else 0)
            dropped.insert(0, removed)
        if used + note_cost <= budget:
            out.append(note)
            noted = True
        for r in dropped:
            section.drop(clip_line(r, 80), "budget")
        section.truncated = True
        break
    section.items_included += len(out) - (1 if noted else 0)
    section.body = "\n".join(out)


def _bullets(items: Sequence[str], limit: int, item_chars: int) -> str:
    shown = [clip_line(i, item_chars) for i in items[:limit]]
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return ", ".join(shown) + more if shown else "(none)"


# ---------------------------------------------------------------------------------------------- STEP GOAL
def render_goal(
    *,
    goal: str,
    kind: str,
    title: str,
    step_key: str,
    repo_hints: Sequence[str],
    budget: int,
    job_request: str = "",
    dependency_files: Sequence[str] = (),
    knowledge: Sequence[str] = (),
) -> RenderedSection:
    sec = RenderedSection(SectionName.STEP_GOAL)
    head_parts = [p for p in (f"Step {step_key}" if step_key else "", f"kind: {kind}" if kind else "", title) if p]
    header = " · ".join(head_parts)
    meta = f"{header}\n" if header else ""
    hints = f"\nPlanner hints: {_bullets(list(repo_hints), 12, 160)}" if repo_hints else ""
    if dependency_files:
        hints += (
            f"\nFiles earlier steps delivered (use their real interfaces – functions, JSON fields, paths; read them "
            f"instead of guessing): {_bullets(list(dependency_files), 12, 160)}"
        )
    fixed = char_cost(meta) + char_cost("Goal:\n")
    hint_cost = char_cost(hints)
    if fixed + hint_cost > budget:
        hints, hint_cost = "", 0
        sec.truncated = bool(repo_hints)
    request = ""
    if job_request.strip():
        # the step goal comes first; the job request gets what is left, but never more than half of the section
        label = f"\n\n{JOB_REQUEST_LABEL}\n"
        room = min(budget // 2, budget - fixed - hint_cost - char_cost(goal.strip())) - char_cost(label)
        if room > 40:
            text = truncate_middle(job_request.strip(), room)
            sec.truncated = sec.truncated or text != job_request.strip()
            request = f"{label}{text}"
        else:
            sec.truncated = True
    notes = ""
    if knowledge:
        # after goal and request, at most a third of the section; whole entries only
        room = min(budget // 3, budget - fixed - hint_cost - char_cost(goal.strip()) - char_cost(request))
        label = f"\n\n{KNOWLEDGE_LABEL}\n"
        kept: list[str] = []
        for entry in knowledge:
            candidate = label + "\n\n".join([*kept, entry.strip()])
            if char_cost(candidate) > room:
                sec.truncated = True
                break
            kept.append(entry.strip())
        notes = label + "\n\n".join(kept) if kept else ""
    goal_text = truncate_middle(goal.strip(), max(0, budget - fixed - hint_cost - char_cost(request) - char_cost(notes)))
    if goal_text != goal.strip():
        sec.truncated = True
    sec.body = clip_to_cost(f"{meta}Goal:\n{goal_text}{hints}{request}{notes}", budget)
    sec.items_included = 1
    return sec


JOB_REQUEST_LABEL = (
    "Job request (the user's own words for the whole job; this step delivers its part of it – take names, fields, "
    "ranges, formats and messages from here when the goal above leaves them out):"
)

KNOWLEDGE_LABEL = (
    "Knowledge from earlier jobs that fits this step (lessons and conventions; hints – the goal, the job request and the files decide):"
)


# ---------------------------------------------------------------------------------------------- SCOPE
def _display(pattern: str) -> str:
    lit = literal_path(pattern)
    return lit if lit is not None else pattern


def render_scope(scope: ScopeContract | None, exclude_globs: Sequence[str], budget: int, *, max_items: int) -> RenderedSection:
    """The grant only; what was written so far is progress (:func:`render_progress`), so this section stays the same
    turn after turn and the model server can reuse its cache for the prompt up to here (D-021)."""
    sec = RenderedSection(SectionName.SCOPE)
    if scope is None:
        body = (
            "No write scope is granted: this turn is read-only and every mutating tool is rejected.\n"
            "If a change is required, use request_scope_expansion with a concrete justification."
        )
        sec.body = clip_to_cost(body, budget)
        sec.truncated = char_cost(body) > budget
        return sec
    limit = max(1, max_items)
    while True:
        forbidden = list(dict.fromkeys([*exclude_globs, *scope.forbidden_paths]))
        lines = [
            f"Scope version {scope.version} ({'strict target paths' if scope.strict_target_paths else 'target globs'}).",
            f"Allowed operations: {', '.join(scope.allowed_operations) or '(none)'}",
            f"May modify: {_bullets([_display(p) for p in scope.target_paths], limit, 200)}",
            f"May create: {_bullets([_display(p) for p in scope.allowed_new_paths], limit, 200)}",
            f"Forbidden (never write; secret files are never shown): {_bullets(forbidden, limit, 120)}",
            "Everything else is outside the scope; request_scope_expansion asks the runtime for more (with justification).",
        ]
        if scope.reason:
            lines.insert(1, f"Reason: {clip_line(scope.reason, 300)}")
        body = "\n".join(lines)
        if char_cost(body) <= budget or limit == 1:
            break
        limit = max(1, limit // 2)
        sec.truncated = True
    if char_cost(body) > budget:
        body = clip_to_cost(body, max(0, budget - 1)) + "…"
        sec.truncated = True
    sec.body = body
    sec.items_included = len(scope.target_paths) + len(scope.allowed_new_paths)
    return sec


# ---------------------------------------------------------------------------------------------- CONSTRAINTS
def render_constraints(constraints: Sequence[str], budget: int, *, item_chars: int = 2400) -> RenderedSection:
    """Constraints include the cited findings of research steps (up to 2400 chars each, ``research_findings``):
    clipping them to a few hundred characters dropped the facts the coder needed (BUG-231)."""
    sec = RenderedSection(SectionName.CONSTRAINTS)
    items = [c.strip() for c in constraints if c and c.strip()]
    if not items:
        sec.omitted_reason = "empty"
        return sec
    fit_lines(sec, [f"- {clip_line(c, item_chars)}" for c in dict.fromkeys(items)], budget, what="constraints")
    return sec


# ---------------------------------------------------------------------------------------------- ACCEPTANCE
def _code(text: str, limit: int = 300) -> str:
    return f"`{clip_line(text, limit)}`"


def describe_criterion(c: Any) -> str:
    """One line per acceptance criterion (generic over all evidence types)."""
    data: dict[str, Any] = c.model_dump() if isinstance(c, BaseModel) else dict(c) if isinstance(c, Mapping) else {"type": str(c)}
    t = str(data.get("type", "?"))
    if t == "presence":
        pat = data.get("pattern")
        line = f"presence: {data.get('path_glob')} must exist" + (
            f" and match /{clip_line(pat, 200)}/ at least {data.get('min_matches', 1)}x" if pat else ""
        )
    elif t == "absence":
        pat = data.get("pattern")
        line = f"absence: {data.get('path_glob')} " + (f"must not match /{clip_line(pat, 200)}/" if pat else "must not exist")
    elif t == "command":
        line = f"command: {_code(str(data.get('command', '')))} exits {data.get('expect_exit_code', 0)}"
        if data.get("stdout_pattern"):
            line += f", stdout matches /{clip_line(str(data['stdout_pattern']), 200)}/"
    elif t == "test":
        cmd = _code(str(data.get("command", "")))
        line = f"test ({data.get('framework', 'generic')}): {cmd} passes (>= {data.get('min_passed', 1)} passed)"
    elif t == "diff":
        bits = []
        if data.get("must_change"):
            bits.append(f"must change {', '.join(map(str, data['must_change'][:10]))}")
        if data.get("must_not_change"):
            bits.append(f"must not change {', '.join(map(str, data['must_not_change'][:10]))}")
        if data.get("max_changed_files") is not None:
            bits.append(f"at most {data['max_changed_files']} changed files")
        bits.append("empty diff allowed" if data.get("allow_empty") else "diff must not be empty")
        line = "diff: " + "; ".join(bits)
    elif t == "scope":
        line = "scope: every change stays inside SCOPE"
    elif t == "schema":
        line = f"schema: {data.get('path')} is valid {data.get('format', 'json')}" + (
            " matching the given JSON schema" if data.get("json_schema") else ""
        )
    elif t == "security":
        bits = [
            b
            for b, on in (("no secrets", data.get("secret_scan", True)), ("no conflict markers", data.get("conflict_markers", True)))
            if on
        ]
        line = "security: " + (", ".join(bits) or "checks configured by the runtime")
    elif t == "artifact":
        line = f"artifact: >= {data.get('min_count', 1)} '{data.get('kind')}' artifact(s) named {data.get('name_glob', '*')}"
    else:
        line = f"{t}: {clip_line(json.dumps(data, sort_keys=True, default=str), 300)}"
    desc = str(data.get("description") or "").strip()
    return line + (f" — {clip_line(desc, 200)}" if desc else "")


def change_progress(c: Any, changed: Sequence[str]) -> str:
    """Which required changes of a diff criterion the workspace already has (empty for other criteria).

    The coder saw the criteria but not which of them its change already met: with green tests it re-ran checks instead
    of finishing, or finished without a required file (live battery J2/J3 on 0.1.0rc14, BUG-182)."""
    data: dict[str, Any] = c.model_dump() if isinstance(c, BaseModel) else dict(c) if isinstance(c, Mapping) else {}
    if data.get("type") != "diff" or not data.get("must_change"):
        return ""
    missing = [str(g) for g in data["must_change"] if not any(acceptance_glob_matches(p, str(g)) for p in changed)]
    if missing:
        return f" — NOT YET CHANGED: {', '.join(missing[:10])}"
    return " — met (already changed)"


NO_CHANGES = "Changed vs base: none (no changes yet)"


def render_progress(
    *,
    scope: ScopeContract | None,
    acceptance: Sequence[Any],
    status: Sequence[GitStatusEntry],
    changed: Sequence[str],
    max_items: int,
) -> list[str]:
    """What the attempt changed so far, at the head of CURRENT DIFF (D-021): the changed files with their git status,
    the new files of the scope already written and still missing (BUG-228) and per diff criterion whether its required
    change is there (BUG-182). Kept out of the sections before RELEVANT CODE, which stay the same from turn to turn."""
    codes = {e.path: e.status.strip() or e.status for e in status}
    paths = list(dict.fromkeys([*changed, *(e.path for e in status)]))
    if paths:
        shown = [f"{codes.get(p, 'M')} {p}" if p in codes else p for p in paths[:max_items]]
        more = f" (+{len(paths) - max_items} more)" if len(paths) > max_items else ""
        lines = [f"Changed vs base ({len(paths)}): {', '.join(shown)}{more}"]
    else:
        lines = [NO_CHANGES]
    if scope is not None:
        done = set(paths)
        written = [p for p in scope.allowed_new_paths if p in done]
        missing = [p for p in scope.allowed_new_paths if literal_path(p) and p not in done]
        if written:
            lines.append(f"New files written: {_bullets([_display(p) for p in written], max_items, 200)}")
            if missing:
                lines.append(f"Not written yet: {', '.join(_display(p) for p in missing[:max_items])}")
    marks = [(i, change_progress(c, paths)) for i, c in enumerate(acceptance, 1)]
    lines.extend(f"Acceptance {i}{mark}" for i, mark in marks if mark)
    return lines


def render_acceptance(acceptance: Sequence[Any], budget: int) -> RenderedSection:
    sec = RenderedSection(SectionName.ACCEPTANCE)
    if not acceptance:
        sec.omitted_reason = "empty"
        return sec
    lines = [f"{i}. {describe_criterion(c)}" for i, c in enumerate(acceptance, 1)]
    fit_lines(sec, ["The runtime verifies these criteria deterministically after complete_step:", *lines], budget, what="criteria")
    sec.items_included = max(0, sec.items_included - 1)
    return sec


# ---------------------------------------------------------------------------------------------- CURRENT REPO FACTS
def _fact_value(value: Any, limit: int) -> str:
    if isinstance(value, Mapping):
        items = [f"{k}={_fact_value(value[k], 60)}" for k in sorted(value, key=str)[:limit]]
        more = f" (+{len(value) - limit} more)" if len(value) > limit else ""
        return ", ".join(items) + more
    if isinstance(value, list | tuple | set | frozenset):
        seq = sorted(value, key=str) if isinstance(value, set | frozenset) else list(value)
        shown = [_fact_value(v, 10) for v in seq[:limit]]
        more = f" (+{len(seq) - limit} more)" if len(seq) > limit else ""
        return ", ".join(shown) + more
    return clip_line(str(value), 200)


def render_repo_facts(
    *,
    workspace: WorkspaceHandle,
    inventory: Mapping[str, Any] | None,
    budget: int,
    max_items: int,
) -> RenderedSection:
    """Repository, branch, base and inventory; the changed files are progress (:func:`render_progress`, D-021)."""
    sec = RenderedSection(SectionName.CURRENT_REPO_FACTS)
    lines = [
        f"Repository: {workspace.repository_key} · branch {workspace.branch} · base {workspace.base_branch}@{workspace.base_sha[:12]}",
    ]
    for key in sorted(inventory or {}, key=str):
        value = (inventory or {})[key]
        if value in (None, "", [], {}):
            continue
        lines.append(f"{clip_line(str(key), 60)}: {_fact_value(value, max_items)}")
    fit_lines(sec, lines, budget, what="facts")
    return sec


# ---------------------------------------------------------------------------------------------- LATEST FAILURE
def _correction_line(item: CorrectionItem, limit: int) -> str:
    where = f" {item.path}" if item.path else ""
    text = strip_reasoning(item.message)
    if item.suggested_fix:
        text += f" — suggested fix: {strip_reasoning(item.suggested_fix)}"
    rendered, _ = preserve_failure(text.strip(), limit)
    return f"- [{item.source}:{clip_line(item.label, 80)}]{where}: " + rendered.rstrip("\n").replace("\n", "\n  ")


def render_failure(latest_failure: str | None, correction: Sequence[CorrectionItem], budget: int, *, item_chars: int) -> RenderedSection:
    sec = RenderedSection(SectionName.LATEST_FAILURE)
    failure = (latest_failure or "").strip("\n")
    if not failure.strip() and not correction:
        sec.omitted_reason = "empty"
        return sec
    corr_lines = [_correction_line(c, item_chars) for c in correction]
    corr_title = "Correction evidence (must be fixed in this attempt):"
    corr_cost = char_cost("\n".join([corr_title, *corr_lines])) if corr_lines else 0
    parts: list[str] = []
    if failure.strip():
        title = "Exact output of the latest failure (verbatim):\n"
        reserve = min(corr_cost + 2, int(budget * 0.35)) if corr_lines else 0
        fence = fence_for(failure)  # fenced: tool output can never pose as a section heading
        wrap = char_cost(title) + 2 * (len(fence) + 1)
        room = budget - reserve - wrap
        if room >= 40:
            text, cut = preserve_failure(failure, room)
            parts.append(f"{title}{fence}\n{text.rstrip(chr(10))}\n{fence}")
        else:  # pathological tiny budget: no framing
            text, cut = preserve_failure(failure, max(0, budget - reserve))
            parts.append(text.rstrip("\n"))
        sec.truncated = cut
        sec.items_included += 1
    used = char_cost("\n\n".join(parts))
    if corr_lines:
        corr = RenderedSection(SectionName.LATEST_FAILURE)
        fit_lines(corr, [corr_title, *corr_lines], max(0, budget - used - (2 if parts else 0)), what="correction items")
        if corr.body:
            parts.append(corr.body)
        sec.items_included += max(0, corr.items_included - 1)
        sec.items_dropped += corr.items_dropped
        sec.dropped.extend(corr.dropped)
        sec.truncated = sec.truncated or corr.truncated
    sec.body = "\n\n".join(parts)
    return sec


# ---------------------------------------------------------------------------------------------- AVAILABLE TOOLS
def _schema_type(schema: Mapping[str, Any], depth: int = 0) -> str:
    if depth > 2:
        return "any"
    if "enum" in schema and isinstance(schema["enum"], list):
        return "|".join(json.dumps(v) for v in schema["enum"][:8]) + ("|…" if len(schema["enum"]) > 8 else "")
    for comb in ("anyOf", "oneOf"):
        if isinstance(schema.get(comb), list):
            types = [_schema_type(s, depth + 1) for s in schema[comb] if isinstance(s, Mapping)]
            types = [t for t in dict.fromkeys(types) if t != "null"]
            return "|".join(types) or "any"
    if "$ref" in schema:
        return "object"
    t = schema.get("type")
    if isinstance(t, list):
        return "|".join(str(x) for x in t if x != "null") or "any"
    if t == "array":
        items = schema.get("items")
        return f"array<{_schema_type(items, depth + 1) if isinstance(items, Mapping) else 'any'}>"
    return str(t) if t else "any"


def _signature(spec: ToolPromptSpec, *, typed: bool) -> str:
    props = spec.args_schema.get("properties", {}) if isinstance(spec.args_schema, Mapping) else {}
    required = set(spec.args_schema.get("required", []) or []) if isinstance(spec.args_schema, Mapping) else set()
    params = []
    for name, sub in props.items() if isinstance(props, Mapping) else []:
        opt = "" if name in required else "?"
        if typed and isinstance(sub, Mapping):
            params.append(f"{name}{opt}: {_schema_type(sub)}")
        else:
            params.append(f"{name}{opt}")
    return f"{spec.name}({', '.join(params)})"


def render_tools(tools: Sequence[ToolPromptSpec], budget: int) -> RenderedSection:
    sec = RenderedSection(SectionName.AVAILABLE_TOOLS)
    unique: list[ToolPromptSpec] = []
    seen: set[str] = set()
    for t in tools:
        if t.name and t.name not in seen:
            seen.add(t.name)
            unique.append(t)
        elif t.name:
            sec.drop(t.name, "duplicate")
    if not unique:
        sec.body = clip_to_cost("(no tools are available for this turn)", budget)
        return sec
    header = "Parameters marked ? are optional."
    levels: list[list[str]] = [
        [f"- {_signature(t, typed=True)}: {clip_line(t.description, 400)}" for t in unique],
        [f"- {_signature(t, typed=True)}: {clip_line(t.description, 120)}" for t in unique],
        [f"- {_signature(t, typed=False)}: {clip_line(t.description, 60)}" for t in unique],
        [f"- {_signature(t, typed=False)}" for t in unique],
    ]
    for level, lines in enumerate(levels):
        body = "\n".join([header, *lines])
        if char_cost(body) <= budget:
            sec.body = body
            sec.truncated = level > 0
            sec.items_included = len(unique)
            return sec
    names = "Tools: " + ", ".join(t.name for t in unique)
    sec.body = names if char_cost(names) <= budget else clip_to_cost(names, max(0, budget - 1)) + "…"
    sec.truncated = True
    sec.items_included = len(unique)
    return sec


# ---------------------------------------------------------------------------------------------- COMPLETION CONDITIONS
def render_completion(text: str, *, budget: int) -> RenderedSection:
    """The completion contract; the turn count is in the closing line of the user message (D-021)."""
    sec = RenderedSection(SectionName.COMPLETION_CONDITIONS)
    contract = text.strip() or "Call complete_step when the step goal is reached and the acceptance criteria hold."
    body = truncate_middle(contract, budget)
    sec.truncated = body != contract
    if char_cost(body) > budget:
        body = clip_to_cost(body, budget)
        sec.truncated = True
    sec.body = body
    sec.items_included = 1
    return sec
