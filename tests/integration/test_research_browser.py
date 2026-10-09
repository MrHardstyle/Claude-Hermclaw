"""P12 12.3 browser fetch: headless Chromium behind the SSRF guard proxy (real browser, local fixture servers)."""

from __future__ import annotations

import asyncio
import os
import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.persistence.models import Event
from hermclaw.research.browser import CHROMIUM_ENV, BrowserRenderer, GuardProxy, RenderingFetcher, find_chromium
from hermclaw.research.engine import ResearchEngine, ResearchSettings
from hermclaw.research.errors import FetchBlocked, FetchError, FetchTimeout
from hermclaw.research.fetch import HttpFetcher
from hermclaw.research.search import StaticSearchProvider
from tests.integration.test_research_engine import MODELS, QUESTION, happy_chat, policy
from tests.integration.test_research_support import FixtureServer, Route, ScriptedChat, resolver_for

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(find_chromium() is None, reason="no Chromium/headless shell available"),
]

JS_PAGE = """<!doctype html><html lang="en"><head><title>Toolkit docs (SPA)</title></head><body>
<div id="app">Loading…</div>
<script>
  const text = 'Toolkit 4.2 requires Python 3.10 or newer. The toolkit configuration file is named toolkit.toml. ';
  document.getElementById('app').innerHTML = '<main><h1>Install</h1><p>' + text.repeat(6) + '</p></main>';
  fetch('/data.json').then(r => r.json()).then(d => {
    document.querySelector('main').insertAdjacentHTML('beforeend', '<p>' + d.msg + '</p>');
  });
</script></body></html>"""
STATIC_PAGE = "<html><body><main><p>" + "Toolkit 4.2 requires Python 3.10 or newer. " * 30 + "</p></main></body></html>"
HOSTS = {"docs.spa.test": "127.0.0.1", "tls.spa.test": "127.0.0.1", "intranet.spa.test": "10.0.0.9"}


class InternalServer:
    """A 'private' HTTP service on 127.0.0.2 that the browser must never reach."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        outer = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return

            def do_GET(self) -> None:
                outer.hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"secret")

            do_POST = do_GET

        self.httpd = ThreadingHTTPServer(("127.0.0.2", 0), _H)
        self.port = int(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[FixtureServer]:
    with FixtureServer() as srv:
        srv.add("/spa", Route(JS_PAGE), host="docs.spa.test")
        srv.add("/data.json", Route('{"msg": "Loaded through fetch: the runner uses asyncio."}', content_type="application/json"))
        srv.add("/static", Route(STATIC_PAGE), host="docs.spa.test")
        yield srv


def guard(networks: list[str] | None = None) -> HttpFetcher:
    return HttpFetcher(timeout_seconds=5, allowed_networks=networks or ["127.0.0.1/32"], resolver=resolver_for(HOSTS))


def renderer(g: HttpFetcher, **kw: Any) -> BrowserRenderer:
    kw.setdefault("timeout_seconds", 30)
    kw.setdefault("virtual_time_budget_ms", 3000)
    return BrowserRenderer(g, **kw)


def test_find_chromium_explicit_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert find_chromium("/nonexistent/chrome") is None
    exe = find_chromium()
    assert exe is not None and os.access(exe, os.X_OK)
    monkeypatch.setenv(CHROMIUM_ENV, str(tmp_path / "missing"))
    assert find_chromium() is None  # an explicit but broken setting is not silently replaced
    monkeypatch.setenv(CHROMIUM_ENV, exe)
    assert find_chromium() == exe


async def test_renders_javascript_page_and_guard_blocks_private_targets_and_writes(server: FixtureServer) -> None:
    internal = InternalServer()
    page = JS_PAGE.replace(
        "</script>",
        f"""fetch('http://127.0.0.2:{internal.port}/literal-ip').catch(() => {{}});
  fetch('http://intranet.spa.test/dns-private').catch(() => {{}});
  fetch('http://169.254.169.254/latest/meta-data/').catch(() => {{}});
  fetch('/collect', {{method: 'POST', body: 'x'}}).catch(() => {{}});
  new WebSocket('ws://127.0.0.2:{internal.port}/ws');
</script>""",
    )
    server.add("/spa-hostile", Route(page), host="docs.spa.test")
    try:
        result = await renderer(guard()).render(f"http://docs.spa.test:{server.port}/spa-hostile")
    finally:
        internal.close()
    assert "Toolkit 4.2 requires Python 3.10" in result.html and "Loaded through fetch" in result.html
    assert internal.hits == []  # neither the literal IP nor the WebSocket reached the private service
    reasons = " | ".join(f"{t} -> {r}" for t, r in result.stats.blocked)
    assert "literal-ip" in reasons and "dns-private" in reasons and "169.254.169.254" in reasons
    assert "method POST not allowed" in reasons
    assert not any(r["path"] == "/collect" for r in server.requests)
    assert all(r["ua"] == "HermclawResearch/0.1 (+internal)" for r in server.requests)


async def test_rendering_fetcher_only_renders_thin_html(server: FixtureServer) -> None:
    g = guard()
    fetcher = RenderingFetcher(g, renderer(g), min_text_chars=300)
    try:
        static = await fetcher.fetch(f"http://docs.spa.test:{server.port}/static")
        assert not static.rendered and fetcher.renders == 0
        spa = await fetcher.fetch(f"http://docs.spa.test:{server.port}/spa")
    finally:
        await fetcher.aclose()
    assert spa.rendered and fetcher.renders == 1 and spa.status_code == 200 and spa.content_type == "text/html"
    assert "Toolkit 4.2 requires Python 3.10" in spa.text and spa.bytes_read == len(spa.text.encode())


async def test_render_failure_keeps_static_result(server: FixtureServer) -> None:
    g = guard()
    fetcher = RenderingFetcher(g, BrowserRenderer(g, executable="/nonexistent/chrome"))
    try:
        res = await fetcher.fetch(f"http://docs.spa.test:{server.port}/spa")
    finally:
        await fetcher.aclose()
    assert not res.rendered and "Loading" in res.text and fetcher.render_failures == 1
    with pytest.raises(FetchError):
        await BrowserRenderer(g, executable="/nonexistent/chrome").render(f"http://docs.spa.test:{server.port}/spa")


async def test_blocked_page_never_starts_a_browser() -> None:
    r = renderer(guard())
    with pytest.raises(FetchBlocked):
        await r.render("http://intranet.spa.test/")
    with pytest.raises(FetchBlocked):
        await r.render("file:///etc/passwd")


async def test_busy_page_is_killed_on_timeout(server: FixtureServer) -> None:
    server.add("/busy", Route("<html><body><script>while (true) {}</script></body></html>"), host="docs.spa.test")
    r = renderer(guard(), timeout_seconds=4, virtual_time_budget_ms=60_000)
    with pytest.raises(FetchTimeout):
        await r.render(f"http://docs.spa.test:{server.port}/busy")
    await asyncio.sleep(0.5)
    leftover = subprocess.run(["pgrep", "-f", "hermclaw-render-"], capture_output=True, text=True, check=False)
    assert leftover.stdout.strip() == "", "browser processes must be killed with their session"


async def test_request_budget_is_enforced(server: FixtureServer) -> None:
    many = "".join(f"<script src='/s{i}.js'></script>" for i in range(6))
    server.add("/many", Route(f"<html><body><p>x</p>{many}</body></html>"), host="docs.spa.test")
    for i in range(6):
        server.add(f"/s{i}.js", Route("void 0;", content_type="application/javascript"), host="docs.spa.test")
    result = await renderer(guard(), max_requests=2).render(f"http://docs.spa.test:{server.port}/many")
    assert result.stats.requests == 2 and any(r == "render budget exhausted" for _, r in result.stats.blocked)


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[int]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=tls.spa.test", "-addext", "subjectAltName=DNS:tls.spa.test"],
        check=True, capture_output=True,
    )  # fmt: skip

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            return

        def do_GET(self) -> None:
            body = JS_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield int(httpd.server_address[1])
    httpd.shutdown()
    httpd.server_close()


async def test_https_page_is_tunnelled_through_validated_connect(tls_server: int) -> None:
    # the self-signed test certificate needs --ignore-certificate-errors; TLS stays end-to-end browser <-> origin
    r = renderer(guard(), extra_args=["--ignore-certificate-errors"])
    result = await r.render(f"https://tls.spa.test:{tls_server}/spa")
    assert "Toolkit 4.2 requires Python 3.10" in result.html
    assert f"tls.spa.test:{tls_server}" in result.stats.allowed


async def test_guard_proxy_rejects_garbage_and_non_http_targets() -> None:
    async with GuardProxy(guard()) as proxy:
        for raw in (
            b"GET /relative HTTP/1.1\r\nHost: x\r\n\r\n",
            b"CONNECT nonsense HTTP/1.1\r\n\r\n",
            b"DELETE http://x/ HTTP/1.1\r\n\r\n",
        ):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(raw)
            await writer.drain()
            answer = await asyncio.wait_for(reader.read(), 5)
            writer.close()
            assert answer.split(b" ", 2)[1] in {b"400", b"405"}
    assert proxy.stats.allowed == [] and len(proxy.stats.blocked) == 3


async def test_engine_reads_javascript_rendered_source(sessionmaker: async_sessionmaker[AsyncSession], server: FixtureServer) -> None:
    g = guard()
    chat: ScriptedChat = happy_chat()
    chat.handlers["research_claims"] = lambda a, m, n: {"claims": ["Toolkit 4.2 requires Python 3.10 or newer."]}
    engine = ResearchEngine(
        sessionmaker,
        chat,
        StaticSearchProvider(default=[f"http://docs.spa.test:{server.port}/spa"]),
        RenderingFetcher(g, renderer(g)),
        policy(primary_domains=["docs.spa.test"]),
        models=MODELS,
        settings=ResearchSettings(),
    )
    try:
        outcome = await engine.run_detailed(QUESTION)
    finally:
        await engine.aclose()
    assert outcome.contract.status == "completed" and outcome.contract.claims[0].claim.startswith("Toolkit 4.2 requires Python 3.10")
    async with sessionmaker() as s:
        ev = (
            await s.execute(select(Event).where(Event.source_id == str(outcome.run_id), Event.event_type == EventType.RESEARCH_SOURCE_READ))
        ).scalar_one()
    assert ev.payload["rendered"] is True and ev.payload["status"] == "read"
