"""Which questions web research can answer (root-cause analysis 2026-10-16, U1).

Research searches the public web. It answers *external* facts – documentation, APIs, versions, error messages of
third-party software. It cannot answer facts about the job's own repository ("which language does this repository
use?") and it must not make technology decisions ("which framework should we use?"): in the live jobs such
questions returned other people's projects (Next.js, Go/iris, ASP.NET, "vanillajs") and popularity rankings, and
the plan followed them into frameworks and dependencies the job never needed.

The checks are deterministic text rules on the question only; they know nothing about a particular job.
"""

from __future__ import annotations

import re

_REPO_SELF = re.compile(
    r"\b(?:this|the|our|current|given|existing|target|that)\s+"
    r"(?:(?:git|code|software|source|test|empty|new)\s+)?"
    r"(?:repository|repo|project|codebase|code\s+base|workspace|application|app|environment|sandbox)\b"
    r"(?!\s+(?:template|generator|scaffold(?:ing)?|structure\s+conventions?))",
    re.IGNORECASE,
)
_CHOICE_WORD = re.compile(
    r"\b(?:which|preferred|prefer|best|most\s+popular|popular|suitable|appropriate|recommended|ideal|"
    r"choose|choice|select|pick|should\s+(?:i|we)\s+use|determine\s+(?:a|the)|decide)\b",
    re.IGNORECASE,
)
_TECH_WORD = re.compile(
    r"\b(?:frameworks?|(?:programming\s+)?languages?|(?:tech(?:nology)?\s+)?stacks?|technolog(?:y|ies)|"
    r"librar(?:y|ies)\s+to\s+use)\b",
    re.IGNORECASE,
)
_DERIVED_TECH = re.compile(
    r"\b(?:identified|detected|chosen|selected|used|existing|project's|repository's)\s+(?:web\s+)?"
    r"(?:frameworks?|languages?|stacks?)\b",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"\[(?:framework|language|library|stack|tool|name|project|repo(?:sitory)?)\]|<(?:framework|language)>", re.I)

REPO_FACT_HINT = (
    "take facts about the repository from repository_inventory / retrieved_context (or read the files); an empty "
    "repository has no stack yet"
)
CHOICE_HINT = (
    "a technology choice is a decision, not a web fact: choose the simplest option that fully meets the job "
    "(standard library and the tools the repository or sandbox already has) and record it as an assumption"
)


def unanswerable_reason(question: str) -> str | None:
    """Why web research cannot answer ``question`` (``None`` when it can)."""
    q = " ".join(question.split())
    if not q:
        return "the question is empty"
    if _PLACEHOLDER.search(q):
        return "the question contains an unresolved placeholder; it depends on a decision that has not been made: " + CHOICE_HINT
    if _REPO_SELF.search(q) or _DERIVED_TECH.search(q):
        return "it asks about the job's own repository, which the web does not know: " + REPO_FACT_HINT
    if _CHOICE_WORD.search(q) and _TECH_WORD.search(q):
        return "it asks which technology to use: " + CHOICE_HINT
    return None
