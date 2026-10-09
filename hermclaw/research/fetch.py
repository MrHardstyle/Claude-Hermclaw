"""HTTP fetch for research sources (P12 12.3) with SSRF protection.

Guarantees of :class:`HttpFetcher`:

* only ``http``/``https`` URLs without embedded credentials;
* every hop (initial URL and each redirect target, max ``max_redirects``) is resolved and *all* resolved
  addresses must be public (``is_global``; IPv4-mapped, NAT64 and 6to4 addresses are unwrapped first) unless the
  address lies in an explicitly allowed network or ``allow_private`` is set (tests / trusted internal mirrors);
* the connection goes to the validated address (DNS pinning – a second, rebinding DNS answer is never used);
  TLS still verifies the certificate against the original host name via SNI, and keep-alive pooling is disabled
  so a TLS connection is never reused for another host name;
* content-type allowlist, ``Content-Length`` pre-check, streamed body capped at ``max_bytes`` (truncated flag),
  per-phase timeouts plus a total wall-clock budget, policy user agent; no proxy/env configuration is inherited.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import re
import socket
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from hermclaw.core.config import ResearchPolicy
from hermclaw.research.errors import (
    FetchBlocked,
    FetchError,
    FetchHttpError,
    FetchTimeout,
    FetchTooLarge,
    FetchTooManyRedirects,
    FetchUnsupportedContent,
)

ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset({"text/html", "text/plain", "application/json", "application/xhtml+xml"})
REDIRECT_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Awaitable[list[str]]]


@dataclass(frozen=True)
class FetchResult:
    url: str  # requested URL
    final_url: str  # after redirects
    status_code: int
    content_type: str  # MIME type without parameters
    charset: str | None
    text: str
    bytes_read: int
    truncated: bool
    elapsed_ms: int
    last_modified: datetime | None = None
    redirects: tuple[str, ...] = ()


@runtime_checkable
class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in out:
            out.append(addr)
    return out


def embedded_ipv4(ip: IPAddress) -> ipaddress.IPv4Address | None:
    """IPv4 address hidden in an IPv6 address (mapped, NAT64, 6to4), else ``None``."""
    if isinstance(ip, ipaddress.IPv4Address):
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if any(ip in net for net in _NAT64):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_address(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses (loopback, private, link-local, CGNAT … are not)."""
    inner = embedded_ipv4(ip)
    if inner is not None:
        return is_public_address(inner)
    if isinstance(ip, ipaddress.IPv6Address) and ip.teredo is not None:
        return False
    return bool(ip.is_global) and not ip.is_multicast and not ip.is_unspecified and not ip.is_reserved


def _parse_networks(networks: Iterable[str]) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(n, strict=False) for n in networks)


@dataclass
class HttpFetcher:
    """Fetches one document per call; reusable and safe for concurrent use."""

    timeout_seconds: float = 20.0
    max_bytes: int = 2_000_000
    user_agent: str = "HermclawResearch/0.1 (+internal)"
    max_redirects: int = 5
    allow_private: bool = False
    allowed_networks: Sequence[str] = ()
    allowed_content_types: frozenset[str] = ALLOWED_CONTENT_TYPES
    resolver: Resolver | None = None
    pin_dns: bool = True
    verify: bool | str = True
    total_timeout_seconds: float | None = None
    transport: httpx.AsyncBaseTransport | None = None
    _networks: tuple[IPNetwork, ...] = field(init=False, repr=False, default=())
    _client: httpx.AsyncClient | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        self._networks = _parse_networks(self.allowed_networks)
        if self.max_bytes <= 0 or self.timeout_seconds <= 0:
            raise ValueError("max_bytes and timeout_seconds must be positive")

    @classmethod
    def from_policy(cls, policy: ResearchPolicy, **overrides: object) -> HttpFetcher:
        kwargs: dict[str, object] = {
            "timeout_seconds": float(policy.fetch_timeout_seconds),
            "max_bytes": policy.max_fetch_bytes,
            "user_agent": policy.user_agent,
        }
        kwargs.update(overrides)
        return cls(**kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------------------------------------------- lifecycle
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=min(10.0, self.timeout_seconds)),
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,application/json;q=0.8,*/*;q=0.1",
                    "Accept-Language": "en-US,en;q=0.8,de;q=0.6",
                },
                follow_redirects=False,
                trust_env=False,
                verify=self.verify,
                transport=self.transport,
                # pinned connections must never be reused for another host name (see module doc)
                limits=httpx.Limits(max_keepalive_connections=0 if self.pin_dns else 10, max_connections=20),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> HttpFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------------------------------- SSRF guard
    def _address_allowed(self, ip: IPAddress) -> bool:
        if self.allow_private:
            return True
        if any(ip in net for net in self._networks):
            return True
        inner = embedded_ipv4(ip)
        if inner is not None and any(inner in net for net in self._networks):
            return True
        return is_public_address(ip)

    async def check_url(self, url: str) -> tuple[str, str, int, str]:
        """Validate ``url``; returns (scheme, host, port, pinned address). Raises ``FetchBlocked``."""
        try:
            parts = urlsplit(url)
            host = parts.hostname
            port = parts.port
        except ValueError as exc:
            raise FetchBlocked(f"invalid URL: {exc}") from exc
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            raise FetchBlocked(f"scheme '{scheme}' is not allowed", details={"url": url})
        if not host:
            raise FetchBlocked("URL has no host", details={"url": url})
        if parts.username is not None or parts.password is not None:
            raise FetchBlocked("URLs with embedded credentials are not fetched")
        port = port or (443 if scheme == "https" else 80)
        try:
            literal: IPAddress | None = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            addresses = [str(literal)]
        else:
            resolve = self.resolver or system_resolver
            try:
                addresses = await resolve(host, port)
            except (OSError, UnicodeError) as exc:
                raise FetchError(f"DNS resolution failed for {host}", details={"host": host}) from exc
            if not addresses:
                raise FetchError(f"DNS returned no address for {host}", details={"host": host})
        parsed: list[IPAddress] = []
        for addr in addresses:
            try:
                parsed.append(ipaddress.ip_address(addr.split("%", 1)[0]))
            except ValueError as exc:
                raise FetchBlocked(f"resolver returned invalid address '{addr}'") from exc
        blocked = [str(ip) for ip in parsed if not self._address_allowed(ip)]
        if blocked:
            # one non-public answer is enough to refuse: an attacker controls which answer a client picks
            raise FetchBlocked(
                f"refusing to fetch {host}: resolves to non-public address {blocked[0]}",
                details={"host": host, "addresses": blocked[:5]},
            )
        # prefer IPv4 – IPv6 is often resolvable but not routable on internal hosts
        parsed.sort(key=lambda ip: ip.version)
        return scheme, host, port, str(parsed[0])

    # ------------------------------------------------------------------------------------------- fetch
    async def fetch(self, url: str) -> FetchResult:
        budget = self.total_timeout_seconds or self.timeout_seconds * 2
        started = time.monotonic()
        try:
            async with asyncio.timeout(budget):
                return await self._fetch(url, started)
        except TimeoutError as exc:
            raise FetchTimeout(f"fetch exceeded {budget:.0f}s", details={"url": url}) from exc

    async def _fetch(self, url: str, started: float) -> FetchResult:
        current = url
        redirects: list[str] = []
        client = self._http()
        for _hop in range(self.max_redirects + 1):
            scheme, host, port, address = await self.check_url(current)
            request = self._build_request(client, current, scheme=scheme, host=host, port=port, address=address)
            try:
                resp = await client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise FetchTimeout(f"timeout fetching {host}: {type(exc).__name__}", details={"url": current}) from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"fetching {host} failed: {type(exc).__name__}", details={"url": current}) from exc
            try:
                if resp.status_code in REDIRECT_CODES:
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchHttpError(f"redirect {resp.status_code} without Location", details={"url": current})
                    current = urljoin(current, location.strip())
                    redirects.append(current)
                    continue
                return await self._read(resp, url, current, started, tuple(redirects))
            finally:
                await resp.aclose()
        raise FetchTooManyRedirects(f"more than {self.max_redirects} redirects", details={"url": url, "chain": redirects[:10]})

    def _build_request(self, client: httpx.AsyncClient, url: str, *, scheme: str, host: str, port: int, address: str) -> httpx.Request:
        if not self.pin_dns or self.transport is not None:
            return client.build_request("GET", url)
        parts = urlsplit(url)
        ip_host = f"[{address}]" if ":" in address else address
        default_port = 443 if scheme == "https" else 80
        netloc = ip_host if port == default_port else f"{ip_host}:{port}"
        host_header = host if port == default_port else f"{host}:{port}"
        if ":" in host:  # IPv6 literal host
            host_header = f"[{host}]" if port == default_port else f"[{host}]:{port}"
        pinned = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
        extensions = {"sni_hostname": host} if scheme == "https" else {}
        return client.build_request("GET", pinned, headers={"Host": host_header}, extensions=extensions)

    async def _read(self, resp: httpx.Response, url: str, final_url: str, started: float, redirects: tuple[str, ...]) -> FetchResult:
        if resp.status_code >= 400:
            raise FetchHttpError(f"HTTP {resp.status_code}", details={"url": final_url, "status": resp.status_code})
        raw_ctype = resp.headers.get("content-type", "")
        mime = raw_ctype.split(";", 1)[0].strip().lower()
        if mime not in self.allowed_content_types:
            raise FetchUnsupportedContent(
                f"content type '{mime or 'missing'}' is not allowed", details={"url": final_url, "content_type": mime}
            )
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            raise FetchTooLarge(
                f"document is {declared} bytes (limit {self.max_bytes})", details={"url": final_url, "bytes": int(declared)}
            )
        chunks: list[bytes] = []
        size = 0
        truncated = False
        async for chunk in resp.aiter_bytes():
            remaining = self.max_bytes - size
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                size += remaining
                truncated = True
                break
            chunks.append(chunk)
            size += len(chunk)
        body = b"".join(chunks)
        charset = _charset(raw_ctype, body, mime)
        try:
            text = body.decode(charset or "utf-8", errors="replace")
        except LookupError:
            charset = "utf-8"
            text = body.decode("utf-8", errors="replace")
        return FetchResult(
            url=url,
            final_url=final_url,
            status_code=resp.status_code,
            content_type=mime,
            charset=charset,
            text=text,
            bytes_read=size,
            truncated=truncated,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            last_modified=_http_date(resp.headers.get("last-modified")),
            redirects=redirects,
        )


def _charset(content_type: str, body: bytes, mime: str) -> str | None:
    for param in content_type.split(";")[1:]:
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            return _valid_codec(value.strip().strip("\"'"))
    if mime in {"text/html", "application/xhtml+xml"}:
        m = _META_CHARSET_RE.search(body[:4096])
        if m:
            return _valid_codec(m.group(1).decode("ascii", "ignore"))
    return None


def _valid_codec(name: str) -> str | None:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def _http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return dt if dt.tzinfo is not None else None
