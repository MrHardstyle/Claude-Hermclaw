"""Web search interface (P12 12.2): provider protocol, SearXNG JSON provider, static provider, URL dedupe.

SearXNG (DECISIONS D-004, research 20261008-023) answers ``GET /search?q=…&format=json`` only when ``json`` is
listed in ``search.formats`` of its ``settings.yml``; otherwise it replies 403 (or an HTML page). That case gets
the dedicated error code ``SEARCH_JSON_DISABLED`` so operators know exactly what to change.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from hermclaw.core.errors import ConfigError
from hermclaw.research.errors import SearchDisabled, SearchError, SearchJsonDisabled, SearchUnavailable

#: query parameters that only track the visitor and never change the document
TRACKING_PARAMS: frozenset[str] = frozenset(
    {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid", "_hsenc", "_hsmi", "ref_src", "spm"}
)


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""
    engine: str = ""
    rank: int = 0  # 0-based position in the provider answer for ``query``
    query: str = ""
    published_hint: datetime | None = None
    queries: tuple[str, ...] = ()  # all queries that returned this URL (filled by ``merge_results``)


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        """Return at most ``limit`` results; raise ``SearchError`` (or a subclass) on failure."""
        ...


# ----------------------------------------------------------------------------------------------- URL helpers
def canonical_url(url: str) -> str | None:
    """http(s) URL without fragment, tracking parameters and default port; ``None`` for anything else."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not host:
        return None
    netloc = host.lower().rstrip(".")
    if ":" in netloc:  # IPv6 literal
        netloc = f"[{netloc}]"
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    if parts.username or parts.password:
        # credentials are never followed (SSRF / secret leak); keep them out of the canonical form as well
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)]
    return urlunsplit((scheme, netloc, parts.path or "/", urlencode(query, doseq=True), ""))


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k.startswith("utm_") or k in TRACKING_PARAMS


def dedupe_key(url: str) -> str | None:
    """Identity of a document for de-duplication: canonical URL, ``www.`` dropped, trailing slash ignored."""
    canon = canonical_url(url)
    if canon is None:
        return None
    parts = urlsplit(canon)
    host = parts.netloc.removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https" if parts.scheme in {"http", "https"} else parts.scheme, host, path, parts.query, ""))


def dedupe_results(results: Iterable[SearchResult]) -> list[SearchResult]:
    """Drop invalid and duplicate URLs, keeping the first occurrence (and its rank)."""
    seen: set[str] = set()
    out: list[SearchResult] = []
    for res in results:
        key = dedupe_key(res.url)
        canon = canonical_url(res.url)
        if key is None or canon is None or key in seen:
            continue
        seen.add(key)
        out.append(replace(res, url=canon))
    return out


def merge_results(per_query: Sequence[Sequence[SearchResult]]) -> list[SearchResult]:
    """Merge result lists of several queries: one entry per document, best rank kept, all queries recorded."""
    merged: dict[str, SearchResult] = {}
    order: list[str] = []
    for results in per_query:
        for res in results:
            key = dedupe_key(res.url)
            canon = canonical_url(res.url)
            if key is None or canon is None:
                continue
            prev = merged.get(key)
            if prev is None:
                merged[key] = replace(res, url=canon, queries=(res.query,) if res.query else ())
                order.append(key)
                continue
            queries = prev.queries + ((res.query,) if res.query and res.query not in prev.queries else ())
            best = res if res.rank < prev.rank else prev
            merged[key] = replace(
                prev,
                rank=min(prev.rank, res.rank),
                title=prev.title or res.title,
                snippet=best.snippet or prev.snippet or res.snippet,
                published_hint=prev.published_hint or res.published_hint,
                queries=queries,
            )
    return [merged[k] for k in order]


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    from hermclaw.research.extract import parse_date

    return parse_date(value)


# ----------------------------------------------------------------------------------------------- providers
@dataclass
class StaticSearchProvider:
    """Offline/test provider: fixed answers per query (exact match, case-insensitive) or a default list."""

    results_by_query: Mapping[str, Sequence[SearchResult | str]] = field(default_factory=dict)
    default: Sequence[SearchResult | str] = ()
    failures: Mapping[str, Exception] = field(default_factory=dict)
    name: str = "static"
    calls: list[str] = field(default_factory=list)

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        self.calls.append(query)
        lowered = {k.lower(): v for k, v in self.results_by_query.items()}
        failure = {k.lower(): v for k, v in self.failures.items()}.get(query.lower())
        if failure is not None:
            raise failure
        raw = lowered.get(query.lower(), self.default)
        out: list[SearchResult] = []
        for rank, item in enumerate(raw):
            res = SearchResult(url=item) if isinstance(item, str) else item
            out.append(replace(res, rank=rank, query=query))
        return dedupe_results(out)[:limit]


@dataclass
class DisabledSearchProvider:
    """``policies.research.search_provider: none`` – research runs fail fast with ``SEARCH_DISABLED``."""

    name: str = "none"

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        raise SearchDisabled("web search is disabled (policies.research.search_provider = none)")


class SearxngSearchProvider:
    """SearXNG JSON API client (``GET {base}/search?q=…&format=json``)."""

    name = "searxng"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15.0,
        user_agent: str = "HermclawResearch/0.1 (+internal)",
        categories: Sequence[str] = (),
        language: str | None = None,
        safesearch: int = 1,
        time_range: str | None = None,
        engines: Sequence[str] = (),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ConfigError(f"invalid SearXNG url '{base_url}' (http/https with host required)")
        self.base_url = base_url.rstrip("/")
        self.categories = list(categories)
        self.language = language
        self.safesearch = safesearch
        self.time_range = time_range
        self.engines = list(engines)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                # SearXNG's bot detection (limiter) rejects requests without Accept-Language
                "Accept-Language": "en-US,en;q=0.8,de;q=0.6",
            },
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _params(self, query: str) -> dict[str, str]:
        params = {"q": query, "format": "json", "pageno": "1", "safesearch": str(self.safesearch)}
        if self.categories:
            params["categories"] = ",".join(self.categories)
        if self.language:
            params["language"] = self.language
        if self.time_range:
            params["time_range"] = self.time_range
        if self.engines:
            params["engines"] = ",".join(self.engines)
        return params

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        try:
            resp = await self._client.get(f"{self.base_url}/search", params=self._params(query))
        except httpx.TimeoutException as exc:
            raise SearchUnavailable(f"SearXNG timed out: {type(exc).__name__}", details={"provider": self.name}) from exc
        except httpx.HTTPError as exc:
            raise SearchUnavailable(f"SearXNG unreachable: {type(exc).__name__}", details={"provider": self.name}) from exc
        ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if resp.status_code == 403 or (resp.status_code == 200 and ctype != "application/json"):
            raise SearchJsonDisabled(
                "SearXNG refused the JSON format – add 'json' to search.formats in settings.yml",
                details={"status": resp.status_code, "content_type": ctype},
            )
        if resp.status_code == 429:
            raise SearchUnavailable("SearXNG rate limit (limiter/bot detection) hit", details={"status": 429})
        if resp.status_code >= 400:
            raise SearchError(f"SearXNG answered HTTP {resp.status_code}", details={"status": resp.status_code})
        try:
            data = resp.json()
        except ValueError as exc:
            raise SearchError("SearXNG returned invalid JSON") from exc
        return parse_searxng_results(data, query=query, limit=limit)


def parse_searxng_results(data: Any, *, query: str, limit: int) -> list[SearchResult]:
    """Parse a SearXNG JSON answer; entries without an http(s) URL are skipped."""
    if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
        raise SearchError("SearXNG JSON has no 'results' list")
    out: list[SearchResult] = []
    for item in data.get("results", []):
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or canonical_url(url) is None:
            continue
        engines = item.get("engines")
        engine = item.get("engine") or (",".join(e for e in engines if isinstance(e, str)) if isinstance(engines, list) else "")
        out.append(
            SearchResult(
                url=url,
                title=str(item.get("title") or "")[:500],
                snippet=str(item.get("content") or "")[:1000],
                engine=str(engine)[:100],
                rank=len(out),
                query=query,
                published_hint=_parse_date(item.get("publishedDate")),
            )
        )
    return dedupe_results(out)[:limit]
