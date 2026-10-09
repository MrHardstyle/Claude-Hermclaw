"""Search query planning (P12 12.1) with the fast model and a deterministic fallback.

The fast model (role ``fast``) gets the research question and returns a :class:`QueryPlan`. Queries are
sanitised (length, duplicates, must share at least one key term with the question) and capped at
``policies.research.max_queries``. If the model fails or returns nothing usable, the fallback uses the question
itself plus key-term variants – research never stalls because of the planner model.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from hermclaw.contracts.research import QueryPlan
from hermclaw.core.logging import get_logger
from hermclaw.models.protocols import CallContext, ChatMessage, ChatModel
from hermclaw.research.text import clip, is_numeric_token, key_terms, normalize_ws

log = get_logger(__name__)

MAX_QUERY_CHARS = 200
SYSTEM_PROMPT = (
    "You plan web search queries for a technical research question.\n"
    'Return JSON {{"queries": [...]}} with 1 to {max_queries} distinct, concise search-engine queries '
    "(at most 12 words each). Keep the exact product names, versions and error messages from the question. "
    "Prefer queries that surface primary sources: official documentation, specifications, release notes, "
    "changelogs and source repositories. Write queries in English unless the topic is language specific. "
    "No explanations, no numbering."
)


@dataclass(frozen=True)
class PlannedQueries:
    queries: list[str]
    source: Literal["model", "fallback"]
    alias: str | None = None
    error: str | None = None
    dropped: int = 0


def sanitize_queries(raw: Iterable[str], *, question: str, max_queries: int) -> tuple[list[str], int]:
    """Clean model queries; returns (queries, number dropped)."""
    q_terms = set(key_terms(question))
    out: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for item in raw:
        query = normalize_ws(str(item)).strip(" -•*\t")
        if query[:1].isdigit() and query[1:3] in {". ", ") "}:
            query = query[3:].strip()
        query = clip(query, MAX_QUERY_CHARS)
        terms = set(key_terms(query))
        key = " ".join(sorted(terms))
        if len(query) < 3 or not terms or key in seen or (q_terms and not terms & q_terms):
            dropped += 1
            continue
        seen.add(key)
        out.append(query)
    return out[:max_queries], dropped + max(len(out) - max_queries, 0)


def fallback_queries(question: str, *, max_queries: int) -> list[str]:
    """Question as query plus key-term variants (deduplicated, at most ``max_queries``)."""
    base = normalize_ws(question).rstrip("?!. ")
    terms = key_terms(question)
    candidates: list[str] = []
    if base:
        candidates.append(clip(base, MAX_QUERY_CHARS) if len(base) <= MAX_QUERY_CHARS else " ".join(terms[:12]))
    if terms:
        candidates.append(" ".join(terms[:8]))
        named = [t for t in terms if not is_numeric_token(t)][:6]
        versions = [t for t in terms if is_numeric_token(t)][:2]
        candidates.append(" ".join([*named, *versions, "documentation"]))
        candidates.append(" ".join([*terms[:5], "release notes"]) if versions else " ".join([*named[:5], "example"]))
    out: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        key = " ".join(sorted(set(key_terms(cand))))
        if cand and key not in seen:
            seen.add(key)
            out.append(cand)
    return out[: max(max_queries, 1)] or [clip(question, MAX_QUERY_CHARS)]


class QueryPlanner:
    def __init__(
        self,
        chat: ChatModel,
        alias: str,
        *,
        max_queries: int,
        max_tokens: int | None = 400,
        temperature: float | None = 0.2,
        timeout_seconds: float | None = None,
    ) -> None:
        self.chat = chat
        self.alias = alias
        self.max_queries = max(1, min(max_queries, 6))  # QueryPlan allows at most 6
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds

    def messages(self, question: str) -> list[ChatMessage]:
        return [
            ChatMessage("system", SYSTEM_PROMPT.format(max_queries=self.max_queries)),
            ChatMessage("user", f"Research question:\n{question}\n\nMaximum number of queries: {self.max_queries}"),
        ]

    async def plan(self, question: str, *, ctx: CallContext) -> PlannedQueries:
        try:
            result = await self.chat.structured(
                self.alias,
                self.messages(question),
                QueryPlan,
                ctx=ctx,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            log.warning("research query planning fell back", extra={"alias": self.alias, "error_code": code})
            return PlannedQueries(fallback_queries(question, max_queries=self.max_queries), "fallback", self.alias, str(code))
        queries, dropped = sanitize_queries(result.value.queries, question=question, max_queries=self.max_queries)
        if not queries:
            return PlannedQueries(
                fallback_queries(question, max_queries=self.max_queries), "fallback", self.alias, "NO_USABLE_QUERIES", dropped
            )
        return PlannedQueries(queries, "model", result.result.alias or self.alias, None, dropped)
