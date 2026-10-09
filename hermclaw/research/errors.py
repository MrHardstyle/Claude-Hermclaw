"""Research-engine errors with stable machine-readable codes (Bauplan §23, P12)."""

from __future__ import annotations

from hermclaw.core.errors import HermclawError


class ResearchError(HermclawError):
    code = "RESEARCH_FAILED"
    http_status = 502


# ----------------------------------------------------------------------------------------------- search (12.2)
class SearchError(ResearchError):
    code = "SEARCH_FAILED"


class SearchJsonDisabled(SearchError):
    """SearXNG answers 403/HTML when ``search.formats`` does not contain ``json``."""

    code = "SEARCH_JSON_DISABLED"


class SearchUnavailable(SearchError):
    code = "SEARCH_UNAVAILABLE"
    http_status = 503


class SearchDisabled(SearchError):
    code = "SEARCH_DISABLED"
    http_status = 503


# ----------------------------------------------------------------------------------------------- fetch (12.3)
class FetchError(ResearchError):
    code = "FETCH_FAILED"


class FetchBlocked(FetchError):
    """SSRF guard: scheme, credentials or target address are not allowed."""

    code = "FETCH_BLOCKED"
    http_status = 403


class FetchTooLarge(FetchError):
    code = "FETCH_TOO_LARGE"


class FetchUnsupportedContent(FetchError):
    code = "FETCH_UNSUPPORTED_CONTENT"


class FetchTimeout(FetchError):
    code = "FETCH_TIMEOUT"


class FetchHttpError(FetchError):
    code = "FETCH_HTTP_ERROR"


class FetchTooManyRedirects(FetchError):
    code = "FETCH_TOO_MANY_REDIRECTS"


# ----------------------------------------------------------------------------------------------- synthesis (12.11/12.12)
class SynthesisRejected(ResearchError):
    """The model synthesis contained uncited statements or unknown claim references."""

    code = "SYNTHESIS_UNCITED"
