"""ASGI middleware: verify orchestrator-signed requests on a worker daemon (DECISIONS D-006).

The orchestrator signs every request to a daemon with *that worker's* credential
(:class:`hermclaw.workers.auth.WorkerRequestSigner`, ``X-Hermclaw-Worker`` = the daemon's own id).
The middleware

0. refuses every websocket handshake (fail closed; the scheme signs HTTP requests only),
1. lets exempt paths (``/health``) through unauthenticated,
2. runs the header-only checks before reading the body (missing headers, wrong worker, clock skew,
   bearer token) so unauthenticated uploads are rejected without buffering them,
3. streams the body (bounded by ``max_body_bytes``) into a spooled temp file while hashing it,
4. verifies the HMAC signature and the replay cache,
5. replays the body to the application and exposes the result as ``scope["state"]["worker_auth"]``.

Failures answer ``401 {"error": {"code": "WORKER_AUTH_*", ...}}`` (``413`` for oversized bodies).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Collection, Sequence
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hermclaw.core.logging import get_logger
from hermclaw.workers.auth import MAX_CLOCK_SKEW_SECONDS, ReplayCache, precheck_signed_request, verify_prechecked
from hermclaw.workers.errors import WorkerAuthError

log = get_logger(__name__)

SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
REPLAY_CHUNK_BYTES = 1024 * 1024


async def send_json_error(send: Send, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
    body = json.dumps({"error": {"code": code, "message": message, "details": details or {}}}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SignedRequestMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        worker_id: str,
        tokens: Callable[[], Sequence[str]],
        exempt_paths: Collection[str] = ("/health",),
        max_skew_seconds: float = MAX_CLOCK_SKEW_SECONDS,
        max_body_bytes: int = 1024 * 1024 * 1024,
        replay_cache: ReplayCache | None = None,
    ) -> None:
        self.app = app
        self.worker_id = worker_id
        self._tokens = tokens
        self.exempt_paths = frozenset(exempt_paths)
        self.max_skew_seconds = max_skew_seconds
        self.max_body_bytes = max_body_bytes
        self.replay_cache = replay_cache if replay_cache is not None else ReplayCache(window_seconds=2 * max_skew_seconds)

    def _tokens_for(self, worker_id: str) -> Sequence[str]:
        if worker_id != self.worker_id:
            return []
        try:
            return self._tokens()
        except WorkerAuthError as exc:
            log.error("worker token unavailable", extra={"code": exc.code})
            return []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # fail closed: the signing scheme covers plain HTTP requests only, so no websocket route (e.g.
            # one added through an extension router) can ever be reached unauthenticated
            log.warning("rejected websocket connection", extra={"path": scope.get("path")})
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http" or scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client = scope.get("client")
        remote = client[0] if client else None
        try:
            pre = precheck_signed_request(
                headers=headers,
                tokens_for=self._tokens_for,
                expected_worker_id=self.worker_id,
                max_skew_seconds=self.max_skew_seconds,
            )
        except WorkerAuthError as exc:
            log.warning("rejected unauthenticated request", extra={"code": exc.code, "path": scope["path"], "remote": remote})
            await send_json_error(send, 401, exc.code, "worker authentication failed", exc.details)
            return
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_body_bytes:
            await send_json_error(send, 413, "PAYLOAD_TOO_LARGE", f"request body exceeds {self.max_body_bytes} bytes")
            return

        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES)  # noqa: SIM115 - closed in finally
        try:
            digest = hashlib.sha256()
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk: bytes = message.get("body", b"")
                if chunk:
                    size += len(chunk)
                    if size > self.max_body_bytes:
                        await send_json_error(send, 413, "PAYLOAD_TOO_LARGE", f"request body exceeds {self.max_body_bytes} bytes")
                        return
                    digest.update(chunk)
                    spool.write(chunk)
                if not message.get("more_body", False):
                    break
            try:
                verified = verify_prechecked(
                    pre,
                    method=scope["method"],
                    path=scope["path"],
                    query=scope.get("query_string", b""),
                    body_digest=digest.hexdigest(),
                    replay_cache=self.replay_cache,
                )
            except WorkerAuthError as exc:
                log.warning("rejected request with invalid signature", extra={"code": exc.code, "path": scope["path"], "remote": remote})
                await send_json_error(send, 401, exc.code, "worker authentication failed", exc.details)
                return
            if verified.token_index > 0:
                log.info("request signed with a previous worker token (rotation in progress)")
            scope.setdefault("state", {})["worker_auth"] = verified
            spool.seek(0)
            body_done = False

            async def replay_receive() -> Message:
                nonlocal body_done
                if body_done:
                    return await receive()
                data = spool.read(REPLAY_CHUNK_BYTES)
                more = spool.tell() < size
                if not more:
                    body_done = True
                return {"type": "http.request", "body": data, "more_body": more}

            await self.app(scope, replay_receive, send)
        finally:
            spool.close()
