"""Research engine (Bauplan §23, P12): web search → fetch → extract → sources → claims → synthesis.

Public entry points:

* :class:`hermclaw.research.engine.ResearchEngine` – ``run(question, job_id=None, step_id=None, deep=False)``
* :func:`hermclaw.research.engine.build_research_engine` – production wiring from ``policies.research``
* :class:`hermclaw.research.search.SearxngSearchProvider` / ``StaticSearchProvider`` – search providers
* :class:`hermclaw.research.fetch.HttpFetcher` – SSRF-guarded HTTP fetcher
* :class:`hermclaw.research.browser.RenderingFetcher` – browser (JavaScript) fetch fallback behind the guard proxy
"""

from hermclaw.research.browser import BrowserRenderer, RenderingFetcher
from hermclaw.research.engine import (
    RESEARCH_DECISION_LINKED,
    ResearchEngine,
    ResearchModels,
    ResearchOutcome,
    ResearchSettings,
    build_research_engine,
    format_for_worker,
    research_callback,
)
from hermclaw.research.fetch import FetchResult, HttpFetcher
from hermclaw.research.search import SearchProvider, SearchResult, SearxngSearchProvider, StaticSearchProvider

__all__ = [
    "RESEARCH_DECISION_LINKED",
    "BrowserRenderer",
    "FetchResult",
    "HttpFetcher",
    "RenderingFetcher",
    "ResearchEngine",
    "ResearchModels",
    "ResearchOutcome",
    "ResearchSettings",
    "SearchProvider",
    "SearchResult",
    "SearxngSearchProvider",
    "StaticSearchProvider",
    "build_research_engine",
    "format_for_worker",
    "research_callback",
]
