"""Planner prompt contract (P14 14.1, Bauplan §15).

System prompt: Gemma is the planner, answers with exactly one JSON object conforming to the PlanContract
schema, never writes code, uses only the listed step kinds and capabilities, gives every mutating step
machine-checkable acceptance criteria, and only references repository paths/symbols it was shown.

User message: one compact JSON document with the keys of Bauplan §15
``{job, repository_inventory, retrieved_context, research_summary, capabilities, constraints, existing_tests,
risk_policy}``. Every section has a character budget derived from the planner model profile. Truncation is
deterministic (same input -> byte-identical prompt) and never cuts inside a line. Secrets are redacted before
anything is sent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from hermclaw.contracts.acceptance import EVIDENCE_TYPES
from hermclaw.contracts.common import MUTATING_STEP_KINDS
from hermclaw.core.config import CapabilityConfig, ModelProfileConfig
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.models.protocols import ChatMessage
from hermclaw.planner.inputs import ContextSnippet, PlannerInput, PlannerSettings

TRUNCATION_MARKER = "…[{n} more line(s) truncated]"
PLANNER_INPUT_KEYS = (
    "job",
    "repository_inventory",
    "retrieved_context",
    "research_summary",
    "capabilities",
    "constraints",
    "existing_tests",
    "risk_policy",
)
MUTATING_KINDS_TEXT = ", ".join(sorted(k.value for k in MUTATING_STEP_KINDS))


# --------------------------------------------------------------------------------------------- truncation
def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def truncate_lines(text: str, max_chars: int) -> tuple[str, int]:
    """Keep whole leading lines within ``max_chars`` (marker included). Returns (text, dropped_line_count)."""
    if len(text) <= max_chars:
        return text, 0
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    used = 0
    for idx, line in enumerate(lines):
        marker = TRUNCATION_MARKER.format(n=len(lines) - idx)
        if used + len(line) + len(marker) > max_chars:
            break
        kept.append(line)
        used += len(line)
    dropped = len(lines) - len(kept)
    if dropped == 0:  # pragma: no cover - only reachable when len(text) <= max_chars
        return text, 0
    body = "".join(kept)
    if body and not body.endswith("\n"):
        body += "\n"
    return body + TRUNCATION_MARKER.format(n=dropped), dropped


def truncate_lines_tail(text: str, max_chars: int) -> tuple[str, int]:
    """Keep whole trailing lines (useful for test output, where the failure summary is at the end)."""
    if len(text) <= max_chars:
        return text, 0
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    used = 0
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        marker = TRUNCATION_MARKER.format(n=idx + 1) + "\n"
        if used + len(line) + len(marker) > max_chars:
            break
        kept.append(line)
        used += len(line)
    dropped = len(lines) - len(kept)
    if dropped == 0:  # pragma: no cover - only reachable when len(text) <= max_chars
        return text, 0
    return TRUNCATION_MARKER.format(n=dropped) + "\n" + "".join(reversed(kept)), dropped


_LIST_CAPS = (400, 200, 100, 50, 25, 12, 6, 3, 1)
_STR_CAPS = (4000, 2000, 1000, 500, 250, 120, 60)


def _shrink(value: Any, list_cap: int, str_cap: int) -> Any:
    if isinstance(value, str):
        return truncate_lines(value, str_cap)[0] if len(value) > str_cap else value
    if isinstance(value, dict):
        return {str(k): _shrink(v, list_cap, str_cap) for k, v in value.items()}
    if isinstance(value, list | tuple):
        items = [_shrink(v, list_cap, str_cap) for v in list(value)[:list_cap]]
        if len(value) > list_cap:
            items.append(f"…[{len(value) - list_cap} more item(s) truncated]")
        return items
    return value


def shrink_json(value: Any, max_chars: int) -> tuple[Any, bool]:
    """Deterministically shrink a JSON-like value until its compact form fits ``max_chars``."""
    if len(compact_json(value)) <= max_chars:
        return value, False
    for list_cap in _LIST_CAPS:
        for str_cap in _STR_CAPS:
            candidate = _shrink(value, list_cap, str_cap)
            if len(compact_json(candidate)) <= max_chars:
                return candidate, True
    if isinstance(value, dict):
        keys = [str(k) for k in value]
        stub: dict[str, Any] = {"truncated": True, "keys": keys}
        while keys and len(compact_json(stub)) > max_chars:
            keys.pop()
            stub = {"truncated": True, "keys": keys}
        return stub, True
    return {"truncated": True}, True


# --------------------------------------------------------------------------------------------- budgets
@dataclass(frozen=True)
class PromptBudget:
    """Character budgets per section of the planner user message."""

    total_chars: int
    job_chars: int
    inventory_chars: int
    context_chars: int
    context_item_chars: int
    research_chars: int
    tests_chars: int
    constraints_chars: int
    misc_chars: int
    section_total: int = 0  # the total the section budgets were derived from (< total_chars after scaling)

    @classmethod
    def for_profile(cls, profile: ModelProfileConfig, settings: PlannerSettings, *, system_chars: int = 0) -> PromptBudget:
        input_tokens = max(profile.context_tokens - profile.max_output_tokens, 2048)
        total = int(input_tokens * settings.chars_per_token * settings.prompt_safety) - system_chars
        total = max(total, 6000)
        return cls.from_total(total)

    @classmethod
    def from_total(cls, total: int) -> PromptBudget:
        """Section shares sum to 0.99 of ``total`` (capabilities and risk policy each use ``misc_chars``)."""
        context = int(total * 0.45)
        return cls(
            total_chars=total,
            job_chars=max(int(total * 0.10), 1000),
            inventory_chars=max(int(total * 0.17), 800),
            context_chars=max(context, 1000),
            context_item_chars=max(min(context // 4, 6000), 400),
            research_chars=max(int(total * 0.10), 400),
            tests_chars=max(int(total * 0.05), 300),
            constraints_chars=max(int(total * 0.04), 300),
            misc_chars=max(int(total * 0.04), 600),
            section_total=total,
        )

    def scaled(self, factor: float) -> PromptBudget:
        """The same budget split for a smaller total (used when the assembled message still exceeds the total)."""
        scaled = PromptBudget.from_total(max(int(self.total_chars * factor), 1))
        return PromptBudget(**{**scaled.__dict__, "total_chars": self.total_chars})


@dataclass
class PromptBuild:
    messages: list[ChatMessage]
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def user_payload_chars(self) -> int:
        return int(self.stats.get("user_chars", 0))


# --------------------------------------------------------------------------------------------- section builders
def budget_strings(items: Iterable[str], max_chars: int) -> tuple[list[str], int]:
    """Keep whole items (each line-truncated to the budget) in order until the budget is used up."""
    out: list[str] = []
    used = 2
    omitted = 0
    for raw in items:
        text = DEFAULT_REDACTOR.text(raw)
        if len(text) > max_chars // 2:
            text = truncate_lines(text, max(max_chars // 2, 40))[0]
        cost = len(compact_json(text)) + 1
        if used + cost > max_chars:
            omitted += 1
            continue
        out.append(text)
        used += cost
    return out, omitted


def context_section(snippets: Sequence[ContextSnippet], budget: PromptBudget) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Retrieved context ordered by relevance (score desc, path, line), each snippet cut at line boundaries."""
    ordered = sorted(snippets, key=lambda s: (-s.score, s.path, s.start_line, s.end_line))
    out: list[dict[str, Any]] = []
    used = 2
    stats = {"items": len(ordered), "included": 0, "omitted": 0, "truncated_items": 0}
    for snip in ordered:
        text = DEFAULT_REDACTOR.text(snip.snippet)
        text, dropped = truncate_lines(text, budget.context_item_chars)
        if dropped:
            stats["truncated_items"] += 1
        item = {"path": snip.path, "lines": f"{snip.start_line}-{max(snip.end_line, snip.start_line)}", "text": text}
        cost = len(compact_json(item)) + 1
        if used + cost > budget.context_chars:
            stats["omitted"] += 1
            continue
        out.append(item)
        used += cost
        stats["included"] += 1
    if stats["omitted"]:
        out.append({"omitted_items": stats["omitted"], "note": "lower-ranked context omitted for size"})
    return out, stats


def capability_section(capabilities: Sequence[CapabilityConfig], kind_capability: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for cap in capabilities:
        kinds = sorted(k for k, c in kind_capability.items() if c == cap.name)
        out.append(
            {
                "name": cap.name,
                "step_kinds": kinds,
                "network": cap.network,
                "runs_on": cap.worker_kind,
                "description": cap.description,
            }
        )
    return out


def planner_user_payload(
    *,
    job: dict[str, Any],
    inputs: PlannerInput,
    constraints: Sequence[str],
    capabilities: list[dict[str, Any]],
    risk_policy: dict[str, Any],
    test_command: str | None,
    budget: PromptBudget,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the budgeted planner input document (keys in the order of Bauplan §15)."""
    stats: dict[str, Any] = {}
    job_doc = DEFAULT_REDACTOR.obj(dict(job))
    goal_text, dropped = truncate_lines(str(job_doc.get("goal", "")), budget.job_chars)
    job_doc["goal"] = goal_text
    stats["job_goal_truncated_lines"] = dropped

    inventory = DEFAULT_REDACTOR.obj(dict(inputs.repository_inventory))
    if test_command and "test_command" not in inventory:
        inventory["test_command"] = test_command
    inventory, inv_trunc = shrink_json(inventory, budget.inventory_chars)
    stats["inventory_truncated"] = inv_trunc

    context, ctx_stats = context_section(inputs.retrieved_context, budget)
    stats["context"] = ctx_stats

    research, res_trunc = shrink_json(DEFAULT_REDACTOR.obj(dict(inputs.research_summary)), budget.research_chars)
    stats["research_truncated"] = res_trunc

    cons, cons_omitted = budget_strings(constraints, budget.constraints_chars)
    stats["constraints_omitted"] = cons_omitted
    tests, tests_omitted = budget_strings(inputs.existing_tests, budget.tests_chars)
    stats["existing_tests_omitted"] = tests_omitted

    caps, _ = shrink_json(capabilities, budget.misc_chars)
    risk, _ = shrink_json(DEFAULT_REDACTOR.obj(risk_policy), budget.misc_chars)
    payload = {
        "job": job_doc,
        "repository_inventory": inventory,
        "retrieved_context": context,
        "research_summary": research,
        "capabilities": caps,
        "constraints": cons,
        "existing_tests": tests,
        "risk_policy": risk,
    }
    return payload, stats


REPLAN_SHARED_SHARE = 0.55
REPLAN_SECTION_SHARES: dict[str, float] = {
    "current_plan": 0.28,
    "completed_steps": 0.15,
    "failed_step": 0.17,
    "deterministic_evidence": 0.30,
    "open_steps": 0.05,
    "research_evidence": 0.05,
}


def replan_user_payload(
    *,
    job: dict[str, Any],
    inputs: PlannerInput,
    constraints: Sequence[str],
    capabilities: list[dict[str, Any]],
    risk_policy: dict[str, Any],
    test_command: str | None,
    package: dict[str, Any],
    budget: PromptBudget,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replanner input: original goal, trigger, current plan, completed/failed/open steps, deterministic evidence,
    current repository facts and research evidence (Bauplan §16), each section within its share of the budget."""
    section_total = budget.section_total or budget.total_chars
    shared_budget = PromptBudget.from_total(max(int(section_total * REPLAN_SHARED_SHARE), 1))
    base, stats = planner_user_payload(
        job=job,
        inputs=inputs,
        constraints=constraints,
        capabilities=capabilities,
        risk_policy=risk_policy,
        test_command=test_command,
        budget=shared_budget,
    )
    job_doc = dict(base["job"])
    original_goal = job_doc.pop("goal", "")
    rest = section_total - shared_budget.total_chars
    sections: dict[str, Any] = {}
    truncated: list[str] = []
    for key, share in REPLAN_SECTION_SHARES.items():
        value, was_truncated = shrink_json(package.get(key), max(int(rest * share), 200))
        sections[key] = value
        if was_truncated:
            truncated.append(key)
    stats["replan_sections_truncated"] = truncated
    payload = {
        "original_goal": original_goal,
        "job": job_doc,
        "trigger": package.get("trigger", {}),
        "current_plan": sections["current_plan"],
        "completed_steps": sections["completed_steps"],
        "failed_step": sections["failed_step"],
        "deterministic_evidence": sections["deterministic_evidence"],
        "open_steps": sections["open_steps"],
        "repository_inventory": base["repository_inventory"],
        "retrieved_context": base["retrieved_context"],
        "research_summary": base["research_summary"],
        "research_evidence": sections["research_evidence"],
        "capabilities": base["capabilities"],
        "constraints": base["constraints"],
        "existing_tests": base["existing_tests"],
        "risk_policy": base["risk_policy"],
    }
    return payload, stats


FIT_FACTORS = (1.0, 0.8, 0.6, 0.45, 0.3, 0.2)


def fit_to_budget(
    build: Callable[[PromptBudget], tuple[dict[str, Any], dict[str, Any]]], budget: PromptBudget
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the payload with progressively smaller section budgets until the compact JSON fits ``total_chars``.

    Deterministic: the same input always ends at the same factor. If even the smallest split does not fit (only
    possible with pathological inputs), the smallest payload is returned and ``over_budget`` is set.
    """
    payload: dict[str, Any] = {}
    stats: dict[str, Any] = {}
    for factor in FIT_FACTORS:
        payload, stats = build(budget if factor == 1.0 else budget.scaled(factor))
        size = len(compact_json(payload))
        stats = {**stats, "budget_factor": factor, "budget_total_chars": budget.total_chars, "payload_chars": size}
        if size <= budget.total_chars:
            return payload, stats
    stats["over_budget"] = True
    return payload, stats


# --------------------------------------------------------------------------------------------- system prompts
def _evidence_examples() -> str:
    return "\n".join(
        [
            '  {"type":"presence","path_glob":"src/**/*.py","pattern":"def handler\\\\("}',
            '  {"type":"absence","path_glob":"legacy/old_module.py"}  (path must not exist; with "pattern": text must not occur)',
            '  {"type":"command","command":"<read-only check command>","expect_exit_code":0}',
            '  {"type":"test","command":"<repository test command>","framework":"pytest|unittest|npm|phpunit|go|cargo|generic"}',
            '  {"type":"diff","must_change":["path/or/glob"],"must_not_change":[],"allow_empty":false}',
            '  {"type":"scope"}  (all changes stay inside the runtime scope)',
            '  {"type":"schema","path":"config/app.yaml","format":"json|yaml|toml"}',
            '  {"type":"security","secret_scan":true,"conflict_markers":true}',
            '  {"type":"artifact","kind":"image|video|report|...","name_glob":"*.png"}',
        ]
    )


def _base_rules(kind_capability: dict[str, str], capability_names: Sequence[str], max_steps: int) -> list[str]:
    mapping = ", ".join(f"{k}->{c}" for k, c in kind_capability.items())
    return [
        "You never write code, patches, file contents or shell scripts. Workers execute the steps later; "
        "you only decide WHAT has to be done, in which order, and how success is checked.",
        f"Allowed step kinds and their required capability (kind->capability): {mapping}. "
        f"Use no other kinds. Allowed capabilities: {', '.join(capability_names)}. Use no other capability.",
        "Step ids are S001, S002, S003, ... (unique). depends_on lists ids of steps that must finish first. "
        "The dependency graph must be acyclic and must not contain duplicate work.",
        f"At most {max_steps} steps. One step per coherent change; prefer few, well-scoped steps.",
        f"Every mutating step (kinds: {MUTATING_KINDS_TEXT}) MUST have machine-checkable acceptance criteria. "
        f"Use only these evidence types: {', '.join(EVIDENCE_TYPES)}. Examples:\n{_evidence_examples()}",
        "Test evidence uses the repository's own test command as given in repository_inventory/existing_tests. "
        "Never invent test commands or test files that do not exist.",
        "repo_hints are the existing repository-relative files, directories, globs or symbol names a step works on. "
        "Use ONLY paths and symbols that appear in repository_inventory, retrieved_context or existing_tests. "
        "Never invent paths and never use absolute paths or '..'. Files a step creates go into allowed_new_paths "
        "(repository-relative). Host paths of remote systems belong in goal/constraints, not in repo_hints.",
        "If information is missing and must be researched first, add an entry to research_needed "
        "(question + reason); the runtime schedules research before the dependent work. A research step "
        "(kind research) must be a dependency of the steps that need its result.",
        "Review steps (kind review) and verify steps (kind verify) must depend on the steps they check.",
        "Network is off by default. Set network true (on a step or on command evidence) only for steps whose "
        "capability has network=true in capabilities.",
        "risk is low|medium|high. Deployments, SSH/server administration and database changes are at least medium.",
        "Respect every entry of constraints. Never plan git commits, pushes, merges or branch operations; "
        "the runtime performs all git mutations.",
        "Answer with exactly ONE JSON object that conforms to the provided JSON schema. No markdown, no prose, "
        "no comments, no explanation of your reasoning.",
    ]


def planner_system_prompt(kind_capability: dict[str, str], capability_names: Sequence[str], max_steps: int) -> str:
    rules = _base_rules(kind_capability, capability_names, max_steps)
    numbered = "\n".join(f"{i}. {r}" for i, r in enumerate(rules, start=1))
    return (
        "You are Gemma, the PLANNER of Hermclaw Next, a local multi-agent system for software and operations work.\n"
        "You receive one JSON document (job, repository_inventory, retrieved_context, research_summary, capabilities, "
        "constraints, existing_tests, risk_policy) and produce a structured execution plan (PlanContract: goal, "
        "summary, assumptions, risks, research_needed, steps).\n"
        f"Rules:\n{numbered}\n"
    )


def replanner_system_prompt(kind_capability: dict[str, str], capability_names: Sequence[str], max_steps: int) -> str:
    rules = _base_rules(kind_capability, capability_names, max_steps)
    rules += [
        "This is a REPLAN. completed_steps are already done and stay done: do NOT include them in steps. "
        "New steps may depend on completed step ids.",
        "Only if a completed step truly must run again, include it with the SAME id and a non-empty rerun_reason "
        "explaining why (e.g. the repository changed underneath it). Without rerun_reason a completed step is never re-run.",
        "New steps must use ids that are not used by completed steps. Ids of failed or not-yet-run steps may be reused; "
        "those old steps are superseded by your new plan.",
        "Address the failure evidence (failed_step, evidence) directly: change the approach instead of repeating "
        "the failed step unchanged.",
    ]
    numbered = "\n".join(f"{i}. {r}" for i, r in enumerate(rules, start=1))
    return (
        "You are Gemma, the REPLANNER of Hermclaw Next. A running plan hit a problem. You receive the original goal, "
        "the current plan, completed steps, the failed step with deterministic evidence, current repository facts and "
        "research evidence, and produce the remaining work as a structured plan (ReplanContract).\n"
        f"Rules:\n{numbered}\n"
    )


# --------------------------------------------------------------------------------------------- repair turn
def repair_message(errors: Sequence[str], remaining_after: int) -> str:
    listed = "\n".join(f"- {e}" for e in errors)
    return (
        "Your previous answer is not a valid plan. Fix ALL of the following validation errors and answer again "
        "with the complete corrected JSON object only (no markdown, no prose):\n"
        f"{listed}\n"
        f"Repair attempts remaining after this one: {remaining_after}."
    )


def build_messages(system: str, payload: dict[str, Any], stats: dict[str, Any]) -> PromptBuild:
    user = compact_json(payload)
    stats = dict(stats)
    stats["system_chars"] = len(system)
    stats["user_chars"] = len(user)
    stats["input_sha256"] = hashlib.sha256((system + "\n" + user).encode("utf-8")).hexdigest()
    return PromptBuild(messages=[ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)], stats=stats)
