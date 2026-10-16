"""Allow-listing HTTPS egress proxy for dependency installs (D-014).

Runs inside the ``hermclaw-egress-proxy`` container on the execution worker, the only container attached to both the
internal network of the install sandboxes (no route outside) and an outbound network. It forwards ``CONNECT`` tunnels
to allow-listed package registries only (``pypi.org``, ``files.pythonhosted.org``, ``registry.npmjs.org`` …) and
refuses everything else: plain HTTP, other hosts, other ports, and allow-listed names that resolve to a non-public
address (DNS rebinding into the LAN). Every decision is one JSON line on stdout (``podman logs``).

Standard library only: the container runs it with the system ``python3`` of the generic toolchain image.

    python3 egress_proxy.py --listen 0.0.0.0:3128 --allow pypi.org --allow files.pythonhosted.org [--port 443]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import socket
import sys
import time
from collections.abc import Iterable

MAX_HEAD = 16 * 1024
IO_TIMEOUT = 30.0
CONNECT_TIMEOUT = 15.0


def host_allowed(host: str, allowed: Iterable[str]) -> bool:
    """``host`` equals an allow-list entry, or is a subdomain of an entry written as ``.example.org``."""
    h = host.strip().lower().rstrip(".")
    if not h:
        return False
    for entry in allowed:
        e = entry.strip().lower().rstrip(".")
        if not e:
            continue
        if e.startswith("."):
            if h.endswith(e) and len(h) > len(e):
                return True
        elif h == e:
            return True
    return False


def public_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


def parse_connect(head: bytes) -> tuple[str, int] | None:
    """``(host, port)`` of a ``CONNECT host:port HTTP/1.x`` request line, else ``None``."""
    line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = line.split()
    if len(parts) != 3 or parts[0].upper() != "CONNECT" or not parts[2].upper().startswith("HTTP/1."):
        return None
    target = parts[1]
    if target.startswith("["):  # IPv6 literals are never allow-listed names
        return None
    host, sep, port = target.rpartition(":")
    if not sep or not host or not port.isdigit():
        return None
    return host, int(port)


class EgressProxy:
    def __init__(self, allowed: list[str], ports: set[int]) -> None:
        self.allowed, self.ports = allowed, ports

    def log(self, **fields: object) -> None:
        print(json.dumps({"ts": round(time.time(), 3), **fields}, sort_keys=True), flush=True)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        client = peer[0] if isinstance(peer, tuple) else "?"
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), IO_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError):
            writer.close()
            return
        target = parse_connect(head)
        if target is None:
            first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")[:200]
            await self.deny(writer, 405, "only CONNECT tunnels to allow-listed registries are permitted", client, first)
            return
        host, port = target
        if port not in self.ports or not host_allowed(host, self.allowed):
            await self.deny(writer, 403, f"{host}:{port} is not an allow-listed package registry", client, f"{host}:{port}")
            return
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            await self.deny(writer, 502, f"cannot resolve {host}: {exc}", client, f"{host}:{port}")
            return
        addrs = [str(i[4][0]) for i in infos if public_address(str(i[4][0]))]
        if not addrs:
            await self.deny(writer, 403, f"{host} does not resolve to a public address", client, f"{host}:{port}")
            return
        upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        last_error: Exception | None = None
        for addr in dict.fromkeys(addrs):  # every address in resolver order (IPv6 first may be unreachable)
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(addr, port), CONNECT_TIMEOUT)
                break
            except (OSError, TimeoutError) as exc:
                last_error = exc
        if upstream is None:
            await self.deny(writer, 502, f"cannot connect to {host}:{port}: {last_error}", client, f"{host}:{port}")
            return
        up_reader, up_writer = upstream
        self.log(decision="allowed", client=client, target=f"{host}:{port}", address=str(up_writer.get_extra_info("peername", ("?",))[0]))
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        sent = await asyncio.gather(self.pipe(reader, up_writer), self.pipe(up_reader, writer), return_exceptions=True)
        self.log(decision="closed", client=client, target=f"{host}:{port}", bytes=[s if isinstance(s, int) else 0 for s in sent])

    async def deny(self, writer: asyncio.StreamWriter, status: int, reason: str, client: str, target: str) -> None:
        self.log(decision="blocked", client=client, target=target, reason=reason)
        body = reason.encode("utf-8", "replace")[:500]
        writer.write(
            f"HTTP/1.1 {status} Blocked\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()

    @staticmethod
    async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> int:
        total = 0
        try:
            while True:
                chunk = await asyncio.wait_for(src.read(65536), IO_TIMEOUT * 4)
                if not chunk:
                    break
                total += len(chunk)
                dst.write(chunk)
                await dst.drain()
        except (ConnectionError, TimeoutError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                dst.close()
        return total


async def serve(listen: str, allowed: list[str], ports: set[int]) -> None:
    host, _, port = listen.rpartition(":")
    proxy = EgressProxy(allowed, ports)
    server = await asyncio.start_server(proxy.handle, host or "0.0.0.0", int(port), limit=MAX_HEAD)  # noqa: S104 - container-internal
    proxy.log(decision="listening", listen=listen, allowed=allowed, ports=sorted(ports))
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--listen", default="0.0.0.0:3128")
    ap.add_argument("--allow", action="append", default=[], help="allowed host (exact) or .domain (subdomains)")
    ap.add_argument("--port", action="append", type=int, default=[], help="allowed destination port (default 443)")
    args = ap.parse_args(argv)
    if not args.allow:
        print("egress_proxy: at least one --allow host is required", file=sys.stderr)
        return 2
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.listen, args.allow, set(args.port or [443])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
