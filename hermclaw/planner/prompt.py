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
            '  {"type":"command","command":"python tool.py report sample.csv --format json","stdout_pattern":"total"}  '
            "(runs the delivered program end to end like a user)",
            '  {"type":"test","command":"<repository test command>","framework":"pytest|unittest|npm|phpunit|go|cargo|generic"}',
            '  {"type":"diff","must_change":["path/or/glob"],"must_not_change":[],"allow_empty":false}',
            '  {"type":"scope"}  (all changes stay inside the runtime scope)',
            '  {"type":"schema","path":"config/app.yaml","format":"json|yaml|toml"}',
            '  {"type":"security","secret_scan":true,"conflict_markers":true}',
            '  {"type":"artifact","kind":"image|video|report|...","name_glob":"*.png"}',
            '  {"type":"artifact","kind":"ops_output","name_glob":"ssh_service_status*"}  (output of an operations tool call)',
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
        f"At most {max_steps} steps. One step per coherent change; prefer few, well-scoped steps. A change and the "
        "tests that check it are ONE step (its allowed_new_paths/repo_hints contain both, its acceptance runs the "
        "tests): never split code and its tests into separate steps. Add verify or review steps only when they check "
        "something the implementing step's own acceptance does not; a small job usually needs one implement step. "
        "A step creates at most about 8 new files (one worker attempt has a limited number of turns): split a larger "
        "job by component (e.g. backend, frontend, scripts, documentation), each step with the tests of its own code.",
        "Components built in different steps that talk to each other (an HTTP/JSON API and the page or client calling "
        "it, a file format and its reader) need one written interface: the step building the providing side also "
        "writes it to a small file (e.g. docs/api.md: every endpoint with method, path and an example request and "
        "JSON response, or every record with its fields) in its allowed_new_paths; the consuming step depends on that "
        "step and names the file in its goal and repo_hints. Both sides follow the file, never their own guess.",
        "Choose the simplest solution that fully meets the job. Use the language's standard library and the tools the "
        "repository or the sandbox already has (e.g. its test runner) unless the job explicitly asks for a framework or "
        "library, or the repository already uses one. Every external dependency is an extra failure source: a "
        "repository without a dependency manifest gets third-party packages only when the job names them (the runtime "
        "refuses to install others), so plan the standard-library solution unless the job asks for a package. A goal "
        'never names a third-party package the job does not ask for, not even as an option ("tomllib or tomli_w"): '
        "the coder tries it and is refused. Where the standard library only reads a format (Python's tomllib reads "
        "TOML), the goal asks for a small own writer for the subset the program writes, tested by reading it back.",
        "When a step delivers a runnable program (command-line tool or script), its acceptance also gets one command "
        "criterion that runs it end to end the way a user would – the documented command with the sample data or "
        "arguments the step creates – so its real output is checked, not only unit tests. Use stdout_pattern only for a "
        "value that must appear (a name, a number), never for exact formatting. Never start servers or watchers "
        "(they do not exit).",
        "Deliver complete work: when the job adds a program, command, command-line options, configuration or "
        "environment variables and the repository has a README, the step that implements it also documents their "
        "use there (README in its repo_hints, or in allowed_new_paths when the repository has none and the job "
        "creates a new program).",
        f"Every mutating step (kinds: {MUTATING_KINDS_TEXT}) MUST have machine-checkable acceptance criteria. "
        f"Use only these evidence types: {', '.join(EVIDENCE_TYPES)}. Examples:\n{_evidence_examples()}",
        "Test evidence uses the repository's own test command as given in repository_inventory/existing_tests. "
        "Never invent test commands or test files that do not exist.",
        "repo_hints are the existing repository-relative files, directories, globs or symbol names a step works on. "
        "Use ONLY paths and symbols that appear in repository_inventory, retrieved_context or existing_tests. "
        "Never invent paths and never use absolute paths or '..'. Files a step creates go into allowed_new_paths "
        "(repository-relative). Host paths of remote systems belong in goal/constraints, not in repo_hints.",
        "Steps of kind ssh or deploy act on registered remote hosts only through the runtime operations tools; they "
        "change no repository files. The runtime stores the output of every successful operations tool call as an "
        "artifact of kind ops_output named <tool>-t<turn>.txt (e.g. ssh_read-t1.txt); use that artifact evidence as "
        "their acceptance. A job without a repository runs in a runtime scratch workspace: files it writes "
        "(reports) go into allowed_new_paths like in any repository.",
        "If information is missing and must be researched first, add an entry to research_needed "
        "(question + reason); the runtime schedules research before the dependent work. A research step "
        "(kind research) must be a dependency of the steps that need its result. Research searches the web: use it "
        "only for external facts (documentation, APIs, versions of third-party software). Questions about this "
        "repository itself (language, framework, layout, tests) and questions about which technology to use are "
        "rejected: repository facts come from repository_inventory and retrieved_context; an empty or nearly empty "
        "repository has no stack yet, so choose the simplest fitting one yourself and state it as an assumption. "
        "Most jobs need no research at all.",
        "Review steps (kind review) and verify steps (kind verify) must depend on the steps they check. Steps of kind "
        "test, verify and review only run checks and never write files: the step that writes code also writes its "
        "tests (test files in its allowed_new_paths, test evidence in its acceptance).",
        "Network is off by default. Set network true (on a step or on command evidence) only for steps whose "
        "capability has network=true in capabilities.",
        "Code steps run their commands in a network-less sandbox with a fixed toolchain (language runtime and test "
        "tools such as pytest). Third-party dependencies must be declared in the dependency manifest "
        "(requirements.txt or package.json, listed in repo_hints or allowed_new_paths of the step that adds them); "
        "the coder installs them from the allow-listed package registries when the runtime allows dependency "
        "installs. Prefer the standard library and dependencies the repository already declares.",
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
        "open_steps are still part of the job: open steps you leave out are carried over unchanged, with their "
        "dependencies. Do not repeat an open step to keep it; list it only to change it (its id may be reused), and add "
        "new steps only for work no open step covers. A short answer (the changed and new steps) is complete.",
        "deterministic_evidence.runtime_refusals are runtime policy decisions (e.g. a dependency the job does not "
        "name, a forbidden command): they will be refused again. Plan the remaining work so it does not need the "
        "refused action – for a refused dependency, the standard-library solution without that package or manifest.",
        "Never plan around an explicit requirement of the job: when the job demands exactly a thing (a package, "
        "service, file, API, credential) that the evidence shows does not exist or cannot be obtained, or two of its "
        "requirements contradict each other, set user_question to one concrete question for the user in one or "
        "two short sentences (what is missing and which decision you need) and leave steps empty. The job then "
        "waits for the answer. Do not ask about anything the repository, research or a different approach can settle, "
        "and never ask the user to confirm a plan you can deliver: when you can plan the remaining work, return the "
        "steps and leave user_question empty.",
    ]
    numbered = "\n".join(f"{i}. {r}" for i, r in enumerate(rules, start=1))
    return (
        "You are Gemma, the REPLANNER of Hermclaw Next. A running plan hit a problem. You receive the original goal, "
        "the current plan, completed steps, the failed step with deterministic evidence, current repository facts and "
        "research evidence, and produce the remaining work as a structured plan (ReplanContract).\n"
        f"Rules:\n{numbered}\n"
    )


# --------------------------------------------------------------------------------------------- repair turn
def repair_message(errors: Sequence[str], remaining_after: int, earlier: Sequence[str] = ()) -> str:
    """The repair turn: the current errors, plus the errors of earlier answers the corrected plan must keep satisfying
    – each turn only sees the latest answer, and a fix for one rule re-broke the previous one (live G1: tests split
    off → merged → too many files → split off again, PLANNER_INVALID_OUTPUT, BUG-261)."""
    listed = "\n".join(f"- {e}" for e in errors)
    still = ""
    if earlier:
        rules = "\n".join(f"- {e}" for e in earlier)
        still = f"Earlier answers were rejected for the following; the corrected plan must still satisfy these too:\n{rules}\n"
    return (
        "Your previous answer is not a valid plan. Fix ALL of the following validation errors and answer again "
        "with the complete corrected JSON object only (no markdown, no prose):\n"
        f"{listed}\n"
        f"{still}"
        f"Repair attempts remaining after this one: {remaining_after}."
    )


def build_messages(system: str, payload: dict[str, Any], stats: dict[str, Any]) -> PromptBuild:
    user = compact_json(payload)
    stats = dict(stats)
    stats["system_chars"] = len(system)
    stats["user_chars"] = len(user)
    stats["input_sha256"] = hashlib.sha256((system + "\n" + user).encode("utf-8")).hexdigest()
    return PromptBuild(messages=[ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)], stats=stats)
