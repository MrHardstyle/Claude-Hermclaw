"""Test helpers for the research engine (test code only): scripted ChatModel and a local fixture HTTP server."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from hermclaw.core.errors import ModelOutputInvalid
from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult

T = TypeVar("T", bound=BaseModel)

# ----------------------------------------------------------------------------------------------- scripted model
Handler = Callable[[str, list[ChatMessage], int], Any]  # (alias, messages, call number for this purpose) -> answer


@dataclass
class Call:
    alias: str
    purpose: str
    schema: str
    messages: list[ChatMessage]


@dataclass
class ScriptedChat:
    """Answers per ``ctx.purpose`` (concurrency-safe: no global answer order).

    A handler returns a dict/str (validated against the schema), or raises / returns an exception instance.
    Purposes without handler raise ``ModelOutputInvalid`` (= model unusable).
    """

    handlers: dict[str, Handler] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    model_name: str = "scripted"

    def count(self, purpose: str) -> int:
        return sum(1 for c in self.calls if c.purpose == purpose)

    def aliases(self, purpose: str) -> list[str]:
        return [c.alias for c in self.calls if c.purpose == purpose]

    def prompts(self) -> str:
        return "\n".join(m.content for c in self.calls for m in c.messages)

    async def chat(
        self,
        alias: str,
        messages: list[ChatMessage],
        *,
        ctx: CallContext,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> ChatResult:
        raise AssertionError("research uses structured() only")

    async def structured(
        self,
        alias: str,
        messages: list[ChatMessage],
        schema: type[T],
        *,
        ctx: CallContext,
        max_repairs: int = 2,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
    ) -> StructuredResult[T]:
        number = self.count(ctx.purpose)
        self.calls.append(Call(alias, ctx.purpose, schema.__name__, list(messages)))
        await asyncio.sleep(0)
        handler = self.handlers.get(ctx.purpose)
        if handler is None:
            raise ModelOutputInvalid(f"no scripted answer for {ctx.purpose}", details={"alias": alias})
        answer = handler(alias, messages, number)
        if isinstance(answer, BaseException):
            raise answer
        content = answer if isinstance(answer, str) else json.dumps(answer)
        try:
            value = schema.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ModelOutputInvalid(f"invalid {schema.__name__}", details={"error": str(exc)[:200]}) from exc
        return StructuredResult(value=value, result=ChatResult(content=content, alias=alias, model=self.model_name))


# ----------------------------------------------------------------------------------------------- fixture server
@dataclass
class Route:
    body: bytes | str = b""
    status: int = 200
    content_type: str | None = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    chunks: int = 1  # stream body in N chunks (no Content-Length when > 1)


class FixtureServer:
    """Threaded HTTP server; routes keyed by path or by ``host/path`` (Host header without port)."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[dict[str, str]] = []
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                host = (self.headers.get("Host") or "").split(":")[0]
                server.requests.append({"host": host, "path": self.path, "ua": self.headers.get("User-Agent", "")})
                route = server.routes.get(f"{host}{self.path}") or server.routes.get(self.path)
                if route is None:
                    route = Route(b"not found", status=404, content_type="text/plain")
                if route.delay:
                    time.sleep(route.delay)
                body = route.body.encode() if isinstance(route.body, str) else route.body
                try:
                    self.send_response(route.status)
                    if route.content_type:
                        self.send_header("Content-Type", route.content_type)
                    for k, v in route.headers.items():
                        self.send_header(k, v)
                    if route.chunks > 1:
                        self.send_header("Transfer-Encoding", "chunked")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        size = max(1, len(body) // route.chunks)
                        for i in range(0, len(body), size):
                            part = body[i : i + size]
                            self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                        self.wfile.write(b"0\r\n\r\n")
                        return
                    if "Content-Length" not in route.headers:
                        self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> FixtureServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"

    def add(self, path: str, route: Route, host: str | None = None) -> str:
        self.routes[f"{host}{path}" if host else path] = route
        return self.url(path, host or "127.0.0.1")


def resolver_for(mapping: dict[str, str]) -> Callable[[str, int], Any]:
    async def resolve(host: str, port: int) -> list[str]:
        if host not in mapping:
            raise OSError(f"unknown host {host}")
        return [mapping[host]]

    return resolve
