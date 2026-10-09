"""P12 12.2/12.3: HttpFetcher against a local HTTP server (SSRF guard, limits, redirects, DNS pinning) and the
SearXNG provider against a fake SearXNG JSON server."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterator

import httpx
import pytest

from hermclaw.core.config import ResearchPolicy
from hermclaw.core.errors import ConfigError
from hermclaw.research.errors import (
    FetchBlocked,
    FetchError,
    FetchHttpError,
    FetchTimeout,
    FetchTooLarge,
    FetchTooManyRedirects,
    FetchUnsupportedContent,
    SearchError,
    SearchJsonDisabled,
    SearchUnavailable,
)
from hermclaw.research.fetch import HttpFetcher, embedded_ipv4, is_public_address
from hermclaw.research.search import (
    SearchResult,
    SearxngSearchProvider,
    StaticSearchProvider,
    canonical_url,
    dedupe_results,
    merge_results,
    parse_searxng_results,
)
from tests.integration.test_research_support import FixtureServer, Route, resolver_for

PAGE = "<html><head><title>Doc</title></head><body><main><p>Hello research world.</p></main></body></html>"


@pytest.fixture
def server() -> Iterator[FixtureServer]:
    with FixtureServer() as srv:
        yield srv


def local_fetcher(**kw: object) -> HttpFetcher:
    return HttpFetcher(timeout_seconds=5, allowed_networks=["127.0.0.0/8"], **kw)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------------------- SSRF guard
@pytest.mark.parametrize(
    "addr",
    ["127.0.0.1", "10.0.0.5", "172.16.1.1", "192.168.178.225", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fc00::1", "fe80::1",
     "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::", "224.0.0.1"],
)
def test_non_public_addresses_detected(addr: str) -> None:
    assert not is_public_address(ipaddress.ip_address(addr))


def test_public_addresses_and_embedded_ipv4() -> None:
    assert is_public_address(ipaddress.ip_address("8.8.8.8"))
    assert is_public_address(ipaddress.ip_address("2001:4860:4860::8888"))
    assert embedded_ipv4(ipaddress.ip_address("64:ff9b::a00:1")) == ipaddress.IPv4Address("10.0.0.1")


async def test_ssrf_blocks_loopback_private_and_bad_urls(server: FixtureServer) -> None:
    url = server.add("/doc", Route(PAGE))
    async with HttpFetcher(timeout_seconds=5) as fetcher:
        for bad in (url, "http://10.1.2.3/", "http://[::1]:8080/", "http://169.254.169.254/latest/meta-data/"):
            with pytest.raises(FetchBlocked):
                await fetcher.fetch(bad)
        for bad in ("file:///etc/passwd", "ftp://example.org/x", "gopher://x/", "http://user:pw@example.org/", "http:///nohost"):
            with pytest.raises(FetchBlocked):
                await fetcher.fetch(bad)
    assert server.requests == []  # nothing ever reached the server


async def test_dns_answer_with_any_private_address_is_refused() -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "127.0.0.1"]  # rebinding-style mixed answer

    async with HttpFetcher(resolver=resolve) as fetcher:
        with pytest.raises(FetchBlocked) as exc:
            await fetcher.fetch("http://evil.example/")
    assert exc.value.code == "FETCH_BLOCKED" and "127.0.0.1" in exc.value.message


async def test_allowed_network_fetch_with_dns_pinning_and_host_header(server: FixtureServer) -> None:
    server.add("/install", Route(PAGE), host="docs.toolkit.test")
    calls: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append(host)
        return ["127.0.0.1"]

    async with local_fetcher(resolver=resolve, user_agent="HermclawTest/1.0") as fetcher:
        res = await fetcher.fetch(f"http://docs.toolkit.test:{server.port}/install")
    assert res.status_code == 200 and res.content_type == "text/html" and res.charset == "utf-8"
    assert "Hello research world." in res.text and not res.truncated
    assert res.final_url == f"http://docs.toolkit.test:{server.port}/install"
    assert calls == ["docs.toolkit.test"]
    assert server.requests[-1]["host"] == "docs.toolkit.test"  # pinned IP, original Host header
    assert server.requests[-1]["ua"] == "HermclawTest/1.0"


async def test_redirect_into_blocked_network_is_refused(server: FixtureServer) -> None:
    url = server.add("/r", Route(b"", status=302, content_type=None, headers={"Location": "http://169.254.169.254/latest"}))
    async with local_fetcher() as fetcher:
        with pytest.raises(FetchBlocked):
            await fetcher.fetch(url)
    assert [r["path"] for r in server.requests] == ["/r"]


async def test_redirects_followed_and_limited(server: FixtureServer) -> None:
    server.add("/a", Route(b"", status=301, content_type=None, headers={"Location": "/b"}))
    server.add("/b", Route(b"", status=307, content_type=None, headers={"Location": "/doc"}))
    server.add("/doc", Route(PAGE))
    for i in range(10):
        server.add(f"/loop{i}", Route(b"", status=302, content_type=None, headers={"Location": f"/loop{i + 1}"}))
    async with local_fetcher(max_redirects=3) as fetcher:
        res = await fetcher.fetch(server.url("/a"))
        assert res.final_url == server.url("/doc") and res.redirects == (server.url("/b"), server.url("/doc"))
        with pytest.raises(FetchTooManyRedirects):
            await fetcher.fetch(server.url("/loop0"))


async def test_content_type_allowlist_and_http_errors(server: FixtureServer) -> None:
    server.add("/pdf", Route(b"%PDF-1.4", content_type="application/pdf"))
    server.add("/none", Route(b"data", content_type=None))
    server.add("/json", Route(json.dumps({"a": 1}), content_type="application/json"))
    server.add("/txt", Route("plain text", content_type="text/plain"))
    server.add("/gone", Route("gone", status=410, content_type="text/plain"))
    async with local_fetcher() as fetcher:
        with pytest.raises(FetchUnsupportedContent):
            await fetcher.fetch(server.url("/pdf"))
        with pytest.raises(FetchUnsupportedContent):
            await fetcher.fetch(server.url("/none"))
        assert (await fetcher.fetch(server.url("/json"))).content_type == "application/json"
        assert (await fetcher.fetch(server.url("/txt"))).text == "plain text"
        with pytest.raises(FetchHttpError) as exc:
            await fetcher.fetch(server.url("/missing"))
        assert exc.value.details["status"] == 404
        with pytest.raises(FetchHttpError):
            await fetcher.fetch(server.url("/gone"))


async def test_size_limits_declared_and_streamed(server: FixtureServer) -> None:
    big = "<html><body><p>" + "x" * 5000 + "</p></body></html>"
    server.add("/big", Route(big))
    server.add("/stream", Route(big, chunks=5))
    async with local_fetcher(max_bytes=1000) as fetcher:
        with pytest.raises(FetchTooLarge):
            await fetcher.fetch(server.url("/big"))
        res = await fetcher.fetch(server.url("/stream"))
    assert res.truncated and res.bytes_read == 1000 and len(res.text) == 1000


async def test_charset_from_meta_and_last_modified(server: FixtureServer) -> None:
    body = '<html><head><meta charset="iso-8859-1"></head><body><p>Gr\xfc\xdfe</p></body></html>'.encode("latin-1")
    server.add("/latin", Route(body, content_type="text/html", headers={"Last-Modified": "Tue, 05 Mar 2024 10:00:00 GMT"}))
    async with local_fetcher() as fetcher:
        res = await fetcher.fetch(server.url("/latin"))
    assert "Grüße" in res.text and res.charset == "iso8859-1"
    assert res.last_modified is not None and res.last_modified.year == 2024


async def test_timeout_and_dns_failure(server: FixtureServer) -> None:
    server.add("/slow", Route(PAGE, delay=1.5))
    async with HttpFetcher(timeout_seconds=0.5, allowed_networks=["127.0.0.0/8"]) as fetcher:
        with pytest.raises(FetchTimeout):
            await fetcher.fetch(server.url("/slow"))
    async with HttpFetcher(resolver=resolver_for({})) as fetcher:
        with pytest.raises(FetchError) as exc:
            await fetcher.fetch("http://unknown.invalid/")
    assert exc.value.code == "FETCH_FAILED"


def test_from_policy_uses_policy_values() -> None:
    policy = ResearchPolicy(fetch_timeout_seconds=7, max_fetch_bytes=1234, user_agent="UA/9")
    fetcher = HttpFetcher.from_policy(policy, allow_private=True)
    assert (fetcher.timeout_seconds, fetcher.max_bytes, fetcher.user_agent, fetcher.allow_private) == (7.0, 1234, "UA/9", True)


# ----------------------------------------------------------------------------------------------- search helpers
def test_canonical_url_and_dedupe() -> None:
    assert canonical_url("HTTPS://Docs.Example.org:443/a?utm_source=x&b=1#frag") == "https://docs.example.org/a?b=1"
    assert canonical_url("http://example.org") == "http://example.org/"
    assert canonical_url("javascript:alert(1)") is None
    assert canonical_url("https://user:pw@example.org/") is None
    results = [
        SearchResult("https://www.example.org/a/", rank=0),
        SearchResult("https://example.org/a", rank=1),
        SearchResult("mailto:x@y", rank=2),
        SearchResult("https://example.org/b", rank=3),
    ]
    assert [r.url for r in dedupe_results(results)] == ["https://www.example.org/a/", "https://example.org/b"]


def test_merge_results_keeps_best_rank_and_all_queries() -> None:
    merged = merge_results(
        [
            [SearchResult("https://a.org/x", title="A", rank=3, query="q1"), SearchResult("https://b.org/", rank=0, query="q1")],
            [SearchResult("https://a.org/x#top", snippet="better", rank=0, query="q2")],
        ]
    )
    assert [m.url for m in merged] == ["https://a.org/x", "https://b.org/"]
    assert merged[0].rank == 0 and merged[0].queries == ("q1", "q2") and merged[0].title == "A" and merged[0].snippet == "better"


async def test_static_provider() -> None:
    provider = StaticSearchProvider({"Toolkit": ["https://a.org/1", "https://a.org/1", "https://b.org/2"]}, default=["https://c.org/"])
    res = await provider.search("toolkit", limit=5)
    assert [r.url for r in res] == ["https://a.org/1", "https://b.org/2"] and res[0].query == "toolkit"
    assert [r.url for r in await provider.search("other", limit=5)] == ["https://c.org/"]
    assert provider.calls == ["toolkit", "other"]


# ----------------------------------------------------------------------------------------------- SearXNG
SEARX_JSON = {
    "query": "toolkit python",
    "number_of_results": 3,
    "results": [
        {"url": "https://docs.toolkit.dev/install", "title": "Install", "content": "Toolkit requires Python 3.10", "engine": "duckduckgo",
         "engines": ["duckduckgo", "brave"], "score": 2.0, "publishedDate": "2026-03-01T00:00:00"},
        {"url": "https://docs.toolkit.dev/install#section", "title": "Install again", "engines": ["google"]},
        {"url": "javascript:void(0)", "title": "bad"},
        "not-a-dict",
        {"url": "https://forum.toolkit.dev/t/1", "title": "Forum", "content": None, "publishedDate": None},
    ],
    "answers": [],
    "suggestions": [],
    "unresponsive_engines": [],
}


async def test_searxng_json_parsing_against_fake_server(server: FixtureServer) -> None:
    server.add("/search", Route(json.dumps(SEARX_JSON), content_type="application/json"))
    provider = SearxngSearchProvider(server.url(""), categories=["general", "it"], language="en", user_agent="UA-Searx")
    try:
        results = await provider.search("toolkit python", limit=10)
    finally:
        await provider.aclose()
    assert [r.url for r in results] == ["https://docs.toolkit.dev/install", "https://forum.toolkit.dev/t/1"]
    first = results[0]
    assert first.title == "Install" and first.snippet == "Toolkit requires Python 3.10" and first.engine == "duckduckgo"
    assert first.published_hint is not None and first.published_hint.year == 2026 and first.query == "toolkit python"
    req = server.requests[-1]
    assert req["path"].startswith("/search?") and "format=json" in req["path"] and "q=toolkit+python" in req["path"]
    assert "categories=general%2Cit" in req["path"] and "language=en" in req["path"] and req["ua"] == "UA-Searx"


async def test_searxng_json_disabled_and_errors(server: FixtureServer) -> None:
    provider = SearxngSearchProvider(server.url(""))
    try:
        server.add("/search", Route("<html>Forbidden</html>", status=403))
        with pytest.raises(SearchJsonDisabled) as exc:
            await provider.search("x", limit=3)
        assert exc.value.code == "SEARCH_JSON_DISABLED"
        server.add("/search", Route("<html>results page</html>", status=200))  # HTML answer although JSON requested
        with pytest.raises(SearchJsonDisabled):
            await provider.search("x", limit=3)
        server.add("/search", Route("busy", status=429, content_type="text/plain"))
        with pytest.raises(SearchUnavailable):
            await provider.search("x", limit=3)
        server.add("/search", Route("boom", status=500, content_type="text/plain"))
        with pytest.raises(SearchError) as err:
            await provider.search("x", limit=3)
        assert err.value.code == "SEARCH_FAILED"
        server.add("/search", Route("{broken", content_type="application/json"))
        with pytest.raises(SearchError):
            await provider.search("x", limit=3)
    finally:
        await provider.aclose()
    unreachable = SearxngSearchProvider("http://127.0.0.1:9", timeout_seconds=2)
    with pytest.raises(SearchUnavailable):
        await unreachable.search("x", limit=3)
    await unreachable.aclose()


def test_searxng_config_validation_and_parse_errors() -> None:
    with pytest.raises(ConfigError):
        SearxngSearchProvider("ftp://searx")
    with pytest.raises(SearchError):
        parse_searxng_results({"results": "nope"}, query="q", limit=3)
    assert parse_searxng_results({"results": []}, query="q", limit=3) == []


async def test_searxng_with_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [{"url": f"https://r.org/{i}"} for i in range(20)]})

    provider = SearxngSearchProvider("http://searx.internal:8888", transport=httpx.MockTransport(handler))
    assert len(await provider.search("q", limit=5)) == 5
    await provider.aclose()


@pytest.mark.live
async def test_live_searxng_on_target_host() -> None:
    """BLOCKER-001: needs the SearXNG container on the orchestrator host (.225)."""
    import os

    url = os.environ.get("HERMCLAW_LIVE_SEARXNG_URL", "http://192.168.178.225:8888")
    provider = SearxngSearchProvider(url, timeout_seconds=20)
    try:
        results = await provider.search("python asyncio documentation", limit=5)
    finally:
        await provider.aclose()
    assert results and all(r.url.startswith(("http://", "https://")) for r in results)
