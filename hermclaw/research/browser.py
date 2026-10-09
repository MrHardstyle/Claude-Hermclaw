"""Browser fetch (P12 12.3): JavaScript rendering fallback for research sources, SSRF-guarded.

Some documentation sites render their content client-side; the plain :class:`~hermclaw.research.fetch.HttpFetcher`
then sees an empty shell. :class:`RenderingFetcher` fetches with HTTP first (status, content type, size and SSRF
checks) and only when the extracted HTML text is too short it re-renders the page in headless Chromium
(``--dump-dom``) via :class:`BrowserRenderer`.

Security model – the browser never talks to the network directly:

* every browser request goes through :class:`GuardProxy`, a loopback HTTP proxy that applies the fetcher's SSRF
  guard (``HttpFetcher.check_url``: public addresses only unless explicitly allowed, DNS pinned) to each request,
  including subresources, XHR/fetch, redirects and IP literals; ``--proxy-bypass-list=<-loopback>`` disables
  Chromium's implicit loopback bypass, ``--host-resolver-rules`` makes local DNS resolution fail;
* only ``GET``/``HEAD`` (plain HTTP) and ``CONNECT`` (TLS, end-to-end verified by Chromium) are forwarded – research
  is read-only, a page script cannot ``POST`` anywhere;
* WebRTC is restricted to proxied TCP (``--force-webrtc-ip-handling-policy=disable_non_proxied_udp``), images are
  not loaded, requests and bytes per render are capped, the process runs in its own session with a throw-away
  profile, a minimal environment (no inherited secrets/proxy variables) and is killed on timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

from hermclaw.core.logging import get_logger
from hermclaw.research.errors import FetchBlocked, FetchError, FetchTimeout
from hermclaw.research.extract import extract_html
from hermclaw.research.fetch import FetchResult, HttpFetcher

log = get_logger(__name__)

CHROMIUM_ENV = "HERMCLAW_CHROMIUM"
HTML_TYPES: frozenset[str] = frozenset({"text/html", "application/xhtml+xml"})
_CANDIDATE_GLOBS: tuple[str, ...] = (
    "/opt/pw-browsers/chromium_headless_shell-*/chrome-linux/headless_shell",
    "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
    "~/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
)
_PATH_NAMES: tuple[str, ...] = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome-headless-shell")
_HOP_BY_HOP: frozenset[str] = frozenset(
    {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate", "te", "trailer", "upgrade"}
)
_MAX_HEAD = 64 * 1024


def find_chromium(explicit: str | None = None) -> str | None:
    """Chromium/headless-shell executable: explicit path, ``$HERMCLAW_CHROMIUM``, Playwright caches, ``$PATH``."""
    for cand in (explicit, os.environ.get(CHROMIUM_ENV)):
        if cand:
            return cand if os.access(cand, os.X_OK) else None
    for pattern in _CANDIDATE_GLOBS:
        for path in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            if os.access(path, os.X_OK):
                return path
    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


# ----------------------------------------------------------------------------------------------- guard proxy
@dataclass
class ProxyStats:
    allowed: list[str] = field(default_factory=list)  # "host:port" of forwarded requests
    blocked: list[tuple[str, str]] = field(default_factory=list)  # (target, reason)
    requests: int = 0
    bytes: int = 0


class GuardProxy:
    """Loopback HTTP/CONNECT proxy enforcing ``guard.check_url`` for every request of one browser session."""

    def __init__(self, guard: HttpFetcher, *, max_requests: int = 150, max_bytes: int = 20_000_000, io_timeout: float = 15.0) -> None:
        self.guard = guard
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.io_timeout = io_timeout
        self.stats = ProxyStats()
        self.port = 0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> GuardProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=_MAX_HEAD)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    def reached(self, url: str) -> bool:
        """True when the proxy forwarded at least one request to the host:port of ``url``."""
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return f"{(parts.hostname or '').lower()}:{port}" in self.stats.allowed

    # ------------------------------------------------------------------------------------------- connection handling
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)  # type: ignore[arg-type]
        try:
            await self._serve(reader, writer)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, TimeoutError, OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            if task is not None:
                self._tasks.discard(task)  # type: ignore[arg-type]

    async def _deny(self, writer: asyncio.StreamWriter, status: int, reason: str, target: str) -> None:
        self.stats.blocked.append((target[:300], reason[:200]))
        body = reason.encode("utf-8", "replace")[:500]
        writer.write(
            f"HTTP/1.1 {status} Blocked\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.io_timeout)
        lines = head.decode("latin-1").split("\r\n")
        method, target, _version = lines[0].split(" ", 2)
        method = method.upper()
        if self.stats.requests >= self.max_requests or self.stats.bytes >= self.max_bytes:
            await self._deny(writer, 429, "render budget exhausted", target)
            return
        self.stats.requests += 1
        if method == "CONNECT":
            host, _, port_text = target.rpartition(":")
            host = host.strip("[]")
            if not host or not port_text.isdigit():
                await self._deny(writer, 400, "bad CONNECT target", target)
                return
            check_url = f"https://[{host}]:{port_text}/" if ":" in host else f"https://{host}:{port_text}/"
        elif method in {"GET", "HEAD"}:
            check_url = target
            if urlsplit(target).scheme.lower() != "http":
                await self._deny(writer, 400, "absolute http URL required", target)
                return
        else:
            await self._deny(writer, 405, f"method {method} not allowed (research is read-only)", target)
            return
        try:
            _scheme, host, port, address = await self.guard.check_url(check_url)
        except FetchBlocked as exc:
            await self._deny(writer, 403, exc.message, target)
            return
        except FetchError as exc:
            await self._deny(writer, 502, exc.message, target)
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(address, port, limit=_MAX_HEAD), self.io_timeout)
        except (OSError, TimeoutError) as exc:
            await self._deny(writer, 502, f"upstream connect failed: {type(exc).__name__}", target)
            return
        self.stats.allowed.append(f"{host.lower()}:{port}")
        try:
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await self._pipe_both(reader, writer, up_reader, up_writer)
            else:
                await self._forward_http(
                    method, target, header_lines=lines[1:], reader=reader, writer=writer, up_reader=up_reader, up_writer=up_writer
                )
        finally:
            with contextlib.suppress(Exception):
                up_writer.close()

    async def _forward_http(
        self,
        method: str,
        target: str,
        *,
        header_lines: Sequence[str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
    ) -> None:
        parts = urlsplit(target)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        headers = [h for h in header_lines if h and h.split(":", 1)[0].strip().lower() not in _HOP_BY_HOP]
        up_writer.write(f"{method} {path} HTTP/1.1\r\n".encode("latin-1") + "".join(f"{h}\r\n" for h in headers).encode("latin-1"))
        up_writer.write(b"Connection: close\r\n\r\n")
        await up_writer.drain()
        resp_head = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), self.io_timeout)
        resp_lines = resp_head.decode("latin-1").split("\r\n")
        kept = [h for h in resp_lines[1:] if h and h.split(":", 1)[0].strip().lower() not in _HOP_BY_HOP]
        out = resp_lines[0] + "\r\n" + "".join(f"{h}\r\n" for h in kept) + "Connection: close\r\nProxy-Connection: close\r\n\r\n"
        writer.write(out.encode("latin-1"))
        self.stats.bytes += len(resp_head)
        await writer.drain()

        async def drain_client() -> None:  # the browser closes its side when the response is complete
            while await reader.read(65536):
                pass

        await self._race(self._copy(up_reader, writer), drain_client())

    async def _copy(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        while True:
            chunk = await asyncio.wait_for(src.read(65536), self.io_timeout)
            if not chunk:
                return
            self.stats.bytes += len(chunk)
            if self.stats.bytes > self.max_bytes:
                self.stats.blocked.append(("*", "render byte budget exceeded"))
                return
            dst.write(chunk)
            await dst.drain()

    async def _pipe_both(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, up_reader: asyncio.StreamReader, up_writer: asyncio.StreamWriter
    ) -> None:
        await self._race(self._copy(reader, up_writer), self._copy(up_reader, writer))

    @staticmethod
    async def _race(*coros: object) -> None:
        tasks = [asyncio.ensure_future(c) for c in coros]  # type: ignore[call-overload]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ----------------------------------------------------------------------------------------------- renderer
@dataclass
class RenderResult:
    html: str
    truncated: bool
    elapsed_ms: int
    stats: ProxyStats


@dataclass
class BrowserRenderer:
    """Renders one URL in headless Chromium (``--dump-dom``) behind a :class:`GuardProxy`."""

    guard: HttpFetcher
    executable: str | None = None
    virtual_time_budget_ms: int = 5000
    timeout_seconds: float = 30.0
    max_output_bytes: int = 2_000_000
    max_requests: int = 150
    max_proxy_bytes: int = 20_000_000
    concurrency: int = 2
    extra_args: Sequence[str] = ()
    _sem: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(max(1, self.concurrency))

    @property
    def available(self) -> bool:
        return find_chromium(self.executable) is not None

    def command(self, url: str, *, proxy_port: int, profile_dir: str) -> list[str]:
        exe = find_chromium(self.executable)
        if exe is None:
            raise FetchError("no Chromium executable found (set HERMCLAW_CHROMIUM)", details={"env": CHROMIUM_ENV})
        args = [
            exe,
            "--headless",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--disable-default-apps",
            "--disable-breakpad",
            "--mute-audio",
            "--hide-scrollbars",
            "--incognito",
            "--blink-settings=imagesEnabled=false",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--dns-prefetch-disable",
            f"--user-data-dir={profile_dir}",
            f"--proxy-server=http://127.0.0.1:{proxy_port}",
            "--proxy-bypass-list=<-loopback>",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",  # only the guard proxy is reachable
            f"--user-agent={self.guard.user_agent}",
            f"--virtual-time-budget={int(self.virtual_time_budget_ms)}",
            "--dump-dom",
        ]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")  # Chromium refuses its sandbox as root (containers); the proxy still guards
        args.extend(self.extra_args)
        args.append(url)
        return args

    async def render(self, url: str) -> RenderResult:
        await self.guard.check_url(url)  # fail fast before starting a browser
        async with self._sem:
            started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="hermclaw-render-") as profile:
                async with GuardProxy(self.guard, max_requests=self.max_requests, max_bytes=self.max_proxy_bytes) as proxy:
                    html, truncated = await self._run(self.command(url, proxy_port=proxy.port, profile_dir=profile), profile)
                    stats = proxy.stats
                    reached = proxy.reached(url)
            if not reached:
                reason = next((r for t, r in stats.blocked if urlsplit(t).hostname == urlsplit(url).hostname), "page was not loaded")
                raise FetchBlocked(f"browser could not load the page: {reason}", details={"url": url})
            return RenderResult(html, truncated, int((time.monotonic() - started) * 1000), stats)

    async def _run(self, args: list[str], profile: str) -> tuple[str, bool]:
        env = {"HOME": profile, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "TMPDIR": profile}
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        assert proc.stdout is not None
        chunks: list[bytes] = []
        size = 0
        truncated = False
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    remaining = self.max_output_bytes - size
                    if len(chunk) >= remaining:
                        chunks.append(chunk[:remaining])
                        size += remaining
                        truncated = True
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                if truncated:
                    _kill(proc)
                await proc.wait()
        except TimeoutError as exc:
            _kill(proc)
            await proc.wait()
            raise FetchTimeout(f"browser render exceeded {self.timeout_seconds:.0f}s") from exc
        except BaseException:
            _kill(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
            raise
        if proc.returncode not in (0, None) and not truncated and not chunks:
            raise FetchError(f"browser exited with code {proc.returncode}")
        return b"".join(chunks).decode("utf-8", errors="replace"), truncated


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


# ----------------------------------------------------------------------------------------------- fetcher
@dataclass
class RenderingFetcher:
    """``Fetcher`` that re-renders HTML pages with too little static text in the browser (best effort)."""

    http: HttpFetcher
    renderer: BrowserRenderer
    min_text_chars: int = 400
    renders: int = 0
    render_failures: int = 0

    async def fetch(self, url: str) -> FetchResult:
        result = await self.http.fetch(url)
        if result.content_type not in HTML_TYPES or result.truncated:
            return result
        static_len = len(extract_html(result.text, url=result.final_url).text)
        if static_len >= self.min_text_chars:
            return result
        try:
            rendered = await self.renderer.render(result.final_url)
        except Exception as exc:  # rendering is an optional improvement – keep the HTTP result
            self.render_failures += 1
            log.warning("browser render failed; using static HTML", extra={"error_code": getattr(exc, "code", type(exc).__name__)})
            return result
        self.renders += 1
        if len(extract_html(rendered.html, url=result.final_url).text) <= static_len:
            return result
        return replace(
            result,
            text=rendered.html,
            bytes_read=len(rendered.html.encode("utf-8")),
            truncated=rendered.truncated,
            elapsed_ms=result.elapsed_ms + rendered.elapsed_ms,
            rendered=True,
        )

    async def aclose(self) -> None:
        await self.http.aclose()


__all__ = ["CHROMIUM_ENV", "BrowserRenderer", "GuardProxy", "ProxyStats", "RenderResult", "RenderingFetcher", "find_chromium"]
