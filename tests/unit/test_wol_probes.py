"""P10 10.3–10.6 probes against real local TCP/HTTP servers and a fake ``ping`` executable."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from aiohttp import web

from hermclaw.contracts.common import WorkerState
from hermclaw.wol.probes import InvalidTarget, Probes, build_http_url, validate_address, validate_base_url
from hermclaw.wol.status import WorkerUiStatus, ui_status_for

UNROUTABLE = "240.0.0.1"  # reserved (class E): connect never succeeds


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def tcp_server(handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]) -> AsyncIterator[int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


@contextlib.asynccontextmanager
async def http_app(app: web.Application) -> AsyncIterator[int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield port
    finally:
        await runner.cleanup()


async def _banner(lines: bytes) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    async def handler(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.write(lines)
        await w.drain()
        await asyncio.sleep(0.2)
        w.close()

    return handler


# ---------------------------------------------------------------------------------------------- validation
@pytest.mark.parametrize("addr", ["127.0.0.1", "::1", "[::1]", "exec-222.lan", "localhost"])
def test_valid_addresses(addr: str) -> None:
    assert validate_address(addr)


@pytest.mark.parametrize("addr", ["", "-c1", "--help", "a b", "host;rm", "x" * 300, "exa_mple.com", "http://x"])
def test_invalid_addresses(addr: str) -> None:
    with pytest.raises(InvalidTarget):
        validate_address(addr)


def test_build_http_url_rejects_authority_switch() -> None:
    assert build_http_url("192.168.178.224", 11434, "/api/version") == "http://192.168.178.224:11434/api/version"
    assert build_http_url("::1", 8080, "/") == "http://[::1]:8080/"
    for bad in ("//evil.example/x", "api/version", "/x\r\nHost: evil", "http://evil/", "/a b"):
        with pytest.raises(InvalidTarget):
            build_http_url("127.0.0.1", 80, bad)


def test_validate_base_url() -> None:
    assert validate_base_url("http://192.168.178.222:8787/") == "http://192.168.178.222:8787"
    for bad in ("ftp://x:1", "http://user:pw@host:1", "http://host:1/?q=1", "notaurl", "http://-bad:1"):
        with pytest.raises(InvalidTarget):
            validate_base_url(bad)


# ---------------------------------------------------------------------------------------------- TCP / ping
async def test_tcp_connect_open_closed_and_unroutable() -> None:
    p = Probes()

    async def idle(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.close()

    async with tcp_server(idle) as port:
        out = await p.tcp_connect("127.0.0.1", port, timeout_seconds=1)
        assert out.ok and out.data["host_alive"]
    closed = await p.tcp_connect("127.0.0.1", free_port(), timeout_seconds=1)
    assert not closed.ok and closed.data["host_alive"] is True  # RST proves the host is up
    down = await p.tcp_connect(UNROUTABLE, 22, timeout_seconds=0.3)
    assert not down.ok and down.data["host_alive"] is False
    bad = await p.tcp_connect("-oProxyCommand=x", 22, timeout_seconds=0.3)
    assert not bad.ok and bad.fatal


async def test_ping_without_binary_uses_tcp_fallback() -> None:
    p = Probes(ping_binary=None)
    up = await p.ping("127.0.0.1", timeout_seconds=1, fallback_port=free_port())
    assert up.ok and up.data["method"] == "tcp"
    down = await p.ping(UNROUTABLE, timeout_seconds=0.3, fallback_port=22)
    assert not down.ok and not down.fatal


def _fake_ping(tmp_path: Path, exit_code: int) -> Path:
    script = tmp_path / f"ping{exit_code}"
    log = tmp_path / "ping.log"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {log}\nexit {exit_code}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def test_ping_uses_icmp_binary_with_safe_arguments(tmp_path: Path) -> None:
    p = Probes(ping_binary=str(_fake_ping(tmp_path, 0)))
    out = await p.ping(UNROUTABLE, timeout_seconds=2.5, fallback_port=22)
    assert out.ok and out.data["method"] == "icmp"
    assert (tmp_path / "ping.log").read_text().split() == ["-n", "-c", "1", "-W", "3", UNROUTABLE]


async def test_ping_no_reply_falls_back_to_tcp(tmp_path: Path) -> None:
    p = Probes(ping_binary=str(_fake_ping(tmp_path, 1)))
    up = await p.ping("127.0.0.1", timeout_seconds=1, fallback_port=free_port())
    assert up.ok and up.data["method"] == "tcp" and up.data["icmp"] == "ping exit 1"
    down = await p.ping(UNROUTABLE, timeout_seconds=0.3, fallback_port=22)
    assert not down.ok


async def test_ping_rejects_option_injection(tmp_path: Path) -> None:
    p = Probes(ping_binary=str(_fake_ping(tmp_path, 0)))
    out = await p.ping("-f", timeout_seconds=1, fallback_port=22)
    assert not out.ok and out.fatal
    assert not (tmp_path / "ping.log").exists()


# ---------------------------------------------------------------------------------------------- SSH
async def test_ssh_banner_detected_even_after_pre_banner_lines() -> None:
    p = Probes()
    async with tcp_server(await _banner(b"Welcome\r\nSSH-2.0-OpenSSH_9.6p1 Ubuntu\r\n")) as port:
        out = await p.ssh_banner("127.0.0.1", port, timeout_seconds=1)
    assert out.ok and out.data["banner"] == "SSH-2.0-OpenSSH_9.6p1 Ubuntu"


async def test_ssh_banner_wrong_protocol_and_silent_server() -> None:
    p = Probes()
    async with tcp_server(await _banner(b"HTTP/1.1 400 Bad Request\r\n\r\n")) as port:
        out = await p.ssh_banner("127.0.0.1", port, timeout_seconds=1)
    assert not out.ok

    async def silent(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            await asyncio.sleep(1)
        finally:
            w.close()  # a stream server's wait_closed() waits for every connection to be closed

    async with tcp_server(silent) as port:
        out = await p.ssh_banner("127.0.0.1", port, timeout_seconds=0.3)
    assert not out.ok and "timeout" in out.detail
    closed = await p.ssh_banner("127.0.0.1", free_port(), timeout_seconds=0.3)
    assert not closed.ok


# ---------------------------------------------------------------------------------------------- HTTP
async def test_http_get_status_json_redirect_and_body_cap() -> None:
    async def health(_req: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "worker_id": "w1"})

    async def broken(_req: web.Request) -> web.Response:
        return web.Response(status=503, text="starting")

    async def redirect(_req: web.Request) -> web.Response:
        raise web.HTTPFound("http://169.254.169.254/latest/meta-data")

    async def huge(_req: web.Request) -> web.Response:
        return web.Response(body=b"x" * 500_000)

    async def slow(_req: web.Request) -> web.Response:
        await asyncio.sleep(2)
        return web.Response(text="late")

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/broken", broken)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/huge", huge)
    app.router.add_get("/slow", slow)
    p = Probes()
    try:
        async with http_app(app) as port:
            base = f"http://127.0.0.1:{port}"
            ok = await p.http_get(f"{base}/health", timeout_seconds=1)
            assert ok.ok and ok.data == {"status_code": 200, "json": {"status": "ok", "worker_id": "w1"}}
            bad = await p.http_get(f"{base}/broken", timeout_seconds=1)
            assert not bad.ok and bad.data["status_code"] == 503
            red = await p.http_get(f"{base}/redirect", timeout_seconds=1)
            assert not red.ok and red.data["status_code"] == 302  # never followed (SSRF)
            red_ok = await p.http_get(f"{base}/redirect", timeout_seconds=1, ok_statuses=range(200, 400))
            assert red_ok.ok
            big = await p.http_get(f"{base}/huge", timeout_seconds=2)
            assert big.ok and "json" not in big.data
            late = await p.http_get(f"{base}/slow", timeout_seconds=0.3)
            assert not late.ok and "timeout" in late.detail
        refused = await p.http_get(f"http://127.0.0.1:{free_port()}/health", timeout_seconds=0.5)
        assert not refused.ok
    finally:
        await p.aclose()


async def test_http_get_uses_injected_client_and_leaves_it_open() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = Probes(client)
    out = await p.http_get("http://worker.lan:8787/health", timeout_seconds=1)
    await p.aclose()
    assert out.ok and calls == ["http://worker.lan:8787/health"]
    assert not client.is_closed
    await client.aclose()


# ---------------------------------------------------------------------------------------------- UI status (10.8)
@pytest.mark.parametrize(
    ("state", "wakeable", "ui"),
    [
        (WorkerState.starting, True, WorkerUiStatus.STARTING),
        (WorkerState.waking, True, WorkerUiStatus.WAKING),
        (WorkerState.ready, True, WorkerUiStatus.READY),
        (WorkerState.busy, True, WorkerUiStatus.BUSY),
        (WorkerState.draining, True, WorkerUiStatus.BUSY),
        (WorkerState.error, True, WorkerUiStatus.ERROR),
        (WorkerState.sleeping, True, WorkerUiStatus.SLEEPING),
        (WorkerState.offline, True, WorkerUiStatus.SLEEPING),
        (WorkerState.offline, False, WorkerUiStatus.ERROR),
        (None, True, WorkerUiStatus.SLEEPING),
        ("bogus", True, WorkerUiStatus.ERROR),
    ],
)
def test_ui_status_mapping(state: WorkerState | str | None, wakeable: bool, ui: WorkerUiStatus) -> None:
    assert ui_status_for(state, wakeable=wakeable) == ui


def test_ui_status_values_are_the_six_fixed_ones() -> None:
    assert {s.value for s in WorkerUiStatus} == {"STARTING", "WAKING", "READY", "BUSY", "ERROR", "SLEEPING"}
