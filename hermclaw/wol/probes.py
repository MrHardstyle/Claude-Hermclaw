"""Network probes of the readiness pipeline (P10 10.3–10.6).

Every probe is bounded by its own timeout and never raises for an unreachable target – it returns a
:class:`ProbeOutcome`. Targets come from the host configuration only (never from worker-reported data),
and addresses are validated before they reach a subprocess (``ping``) or a URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import ipaddress
import json
import math
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

import httpx

from hermclaw.core.logging import get_logger

log = get_logger(__name__)

_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
MAX_BODY_BYTES = 64 * 1024
_SSH_BANNER_MAX = 255
# connect() errors that prove the host itself answered at IP level (a closed port answers with RST)
_HOST_ALIVE_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ECONNRESET})


class InvalidTarget(ValueError):
    """A probe target (address, port, path) failed validation."""


def validate_address(address: str) -> str:
    """IPv4/IPv6 literal or a DNS hostname. Rejects anything that could be read as a command-line option."""
    value = (address or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if not _HOSTNAME.match(value):
        raise InvalidTarget(f"invalid host address {address!r}")
    return value


def validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise InvalidTarget(f"invalid port {port!r}")
    return port


def build_http_url(address: str, port: int, path: str | None, *, scheme: str = "http") -> str:
    """Build ``scheme://address:port/path`` from validated parts. The path must be absolute and cannot
    switch the authority (``//host``) or carry a scheme."""
    p = path or "/"
    if not p.startswith("/") or p.startswith("//") or "://" in p or any(ch in p for ch in ("\r", "\n", "\x00", " ", "\\")):
        raise InvalidTarget(f"invalid HTTP probe path {path!r}")
    if scheme not in ("http", "https"):
        raise InvalidTarget(f"invalid scheme {scheme!r}")
    return str(httpx.URL(scheme=scheme, host=validate_address(address), port=validate_port(port), raw_path=p.encode("ascii")))


def validate_base_url(url: str) -> str:
    """A worker API base URL from configuration: ``http(s)://host[:port][/prefix]`` without credentials,
    query or fragment."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as exc:
        raise InvalidTarget(f"invalid URL {url!r}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise InvalidTarget(f"URL {url!r} must be http(s) with a host")
    if parsed.userinfo or parsed.query or parsed.fragment:
        raise InvalidTarget(f"URL {url!r} must not contain credentials, query or fragment")
    validate_address(parsed.host)
    return str(parsed).rstrip("/")


@dataclass(frozen=True)
class ProbeOutcome:
    """Result of one probe. ``fatal`` stops waiting at once (waiting cannot change the outcome)."""

    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)
    fatal: bool = False


class Probes:
    """Real probes: ICMP ``ping`` (if the binary exists) with TCP fallback, TCP connect, SSH banner, HTTP GET.

    ``http_client`` is used for HTTP probes if given (the caller owns it); otherwise one client is created
    lazily and closed by :meth:`aclose`. Redirects are never followed.
    """

    def __init__(self, http_client: httpx.AsyncClient | None = None, *, ping_binary: str | None = "auto") -> None:
        self._external_client = http_client
        self._client: httpx.AsyncClient | None = None
        self._ping = shutil.which("ping") if ping_binary == "auto" else ping_binary

    @property
    def ping_binary(self) -> str | None:
        return self._ping

    def _http(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------------------------------ TCP
    async def tcp_connect(self, address: str, port: int, *, timeout_seconds: float) -> ProbeOutcome:
        """``ok`` iff a TCP connection to ``address:port`` is accepted. ``data.host_alive`` is also true when
        the host actively refused (closed port) – it proves the machine is up."""
        try:
            host, prt = validate_address(address), validate_port(port)
        except InvalidTarget as exc:
            return ProbeOutcome(False, str(exc), {"host_alive": False}, fatal=True)
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, prt), timeout=max(timeout_seconds, 0.01))
            return ProbeOutcome(True, f"tcp {host}:{prt} open", {"host_alive": True})
        except TimeoutError:
            return ProbeOutcome(False, f"tcp {host}:{prt} timeout", {"host_alive": False})
        except OSError as exc:
            alive = exc.errno in _HOST_ALIVE_ERRNOS or isinstance(exc, ConnectionRefusedError)
            return ProbeOutcome(False, f"tcp {host}:{prt} {type(exc).__name__}", {"host_alive": alive, "errno": exc.errno})
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), 1.0)

    # ------------------------------------------------------------------------------------------ ping
    async def ping(self, address: str, *, timeout_seconds: float, fallback_port: int) -> ProbeOutcome:
        """Host reachable at IP level? ``ping -c 1 -W <s>`` when available (needs ICMP permission), else – or
        when ICMP gets no reply – a TCP connect to ``fallback_port`` where a refusal also proves the host is up."""
        try:
            host = validate_address(address)
        except InvalidTarget as exc:
            return ProbeOutcome(False, str(exc), {"method": "none"}, fatal=True)
        icmp_detail = "ping binary not available"
        loop = asyncio.get_running_loop()
        end = loop.time() + max(timeout_seconds, 0.05)
        if self._ping:
            wait = max(1, math.ceil(timeout_seconds))
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    self._ping,
                    "-n",
                    "-c",
                    "1",
                    "-W",
                    str(wait),
                    host,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                rc = await asyncio.wait_for(proc.wait(), timeout=wait + 1.0)
                if rc == 0:
                    return ProbeOutcome(True, f"icmp echo reply from {host}", {"method": "icmp"})
                icmp_detail = f"ping exit {rc}"
            except TimeoutError:
                icmp_detail = "ping timed out"
            except OSError as exc:
                icmp_detail = f"ping failed: {type(exc).__name__}"
            finally:
                if proc is not None and proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), 1.0)
        # the TCP fallback gets what is left of this probe's budget (at least a LAN round trip)
        tcp = await self.tcp_connect(host, fallback_port, timeout_seconds=max(end - loop.time(), 0.05))
        if tcp.ok or tcp.data.get("host_alive"):
            return ProbeOutcome(True, f"host {host} answers on tcp/{fallback_port}", {"method": "tcp", "icmp": icmp_detail})
        return ProbeOutcome(False, f"{icmp_detail}; {tcp.detail}", {"method": "tcp" if not self._ping else "icmp+tcp"}, fatal=tcp.fatal)

    # ------------------------------------------------------------------------------------------ SSH
    async def ssh_banner(self, address: str, port: int, *, timeout_seconds: float) -> ProbeOutcome:
        """The SSH daemon is up when it sends its identification line ``SSH-2.0-…`` (RFC 4253 §4.2)."""
        try:
            host, prt = validate_address(address), validate_port(port)
        except InvalidTarget as exc:
            return ProbeOutcome(False, str(exc), fatal=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_seconds, 0.01)
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, prt), timeout=max(timeout_seconds, 0.01))
            buf = b""
            # servers may send other lines before the identification string (RFC 4253 §4.2)
            while len(buf) < 4 * _SSH_BANNER_MAX:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                chunk = await asyncio.wait_for(reader.read(_SSH_BANNER_MAX), timeout=remaining)
                if not chunk:
                    break
                buf += chunk
                complete_lines = buf.split(b"\n")[:-1]  # the last element is an unterminated remainder
                for line in complete_lines:
                    if line.startswith(b"SSH-"):
                        banner = line.rstrip(b"\r")[:_SSH_BANNER_MAX].decode("ascii", "replace")
                        return ProbeOutcome(True, f"ssh {host}:{prt} up", {"banner": banner})
            return ProbeOutcome(False, f"ssh {host}:{prt} sent no SSH identification")
        except TimeoutError:
            return ProbeOutcome(False, f"ssh {host}:{prt} timeout")
        except OSError as exc:
            return ProbeOutcome(False, f"ssh {host}:{prt} {type(exc).__name__}")
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), 1.0)

    # ------------------------------------------------------------------------------------------ HTTP
    async def http_get(self, url: str, *, timeout_seconds: float, ok_statuses: range = range(200, 300)) -> ProbeOutcome:
        """``GET url`` without redirects; reads at most 64 KiB. ``data``: ``status_code`` and ``json`` (if the
        body is a JSON document)."""
        try:
            async with self._http().stream("GET", url, timeout=max(timeout_seconds, 0.01), follow_redirects=False) as resp:
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BODY_BYTES:
                        body = body[:MAX_BODY_BYTES]
                        break
                status = resp.status_code
        except httpx.TimeoutException:
            return ProbeOutcome(False, f"GET {url} timeout")
        except (httpx.TransportError, httpx.InvalidURL) as exc:
            return ProbeOutcome(False, f"GET {url} {type(exc).__name__}")
        data: dict[str, Any] = {"status_code": status}
        if body:
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                data["json"] = json.loads(body)
        if status in ok_statuses:
            return ProbeOutcome(True, f"GET {url} -> {status}", data)
        return ProbeOutcome(False, f"GET {url} -> HTTP {status}", data)
