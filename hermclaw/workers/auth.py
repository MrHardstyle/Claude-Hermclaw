"""Worker authentication: per-worker tokens and HMAC-SHA256 request signing (Bauplan §35, DECISIONS D-006).

Both directions use the same per-worker shared secret ("worker token"):

- orchestrator -> worker daemon (``WorkerClient`` signs, ``worker.common.auth`` middleware verifies)
- worker daemon -> orchestrator (heartbeat sender signs, ``hermclaw.workers.api`` verifies)

Headers of a signed request::

    X-Hermclaw-Worker:    <worker id whose credential signs the request>
    X-Hermclaw-Timestamp: <unix seconds, integer>
    X-Hermclaw-Nonce:     <32 hex chars, random per request>
    X-Hermclaw-Signature: v1=<hex HMAC-SHA256(token, canonical)>
    Authorization:        Bearer <token>            (optional, see below)

Canonical string (``|`` separated, fixed field order)::

    hermclaw-worker-v1|METHOD|/path[?query]|timestamp|nonce|sha256_hex(body)

The nonce is an extension of the plain ``method|path|timestamp|sha256(body)`` scheme: it makes every
request unique so the replay cache can reject exact replays without false positives for two identical
requests in the same second. Verification checks, in order: required headers present, known worker,
clock skew <= ``MAX_CLOCK_SKEW_SECONDS`` (120 s), optional bearer token (constant-time) - these header-only
checks run before the body is read (:func:`precheck_signed_request`) - then the signature (constant-time
compare against every valid token - current and previous - to support rotation) and finally the replay
cache (only after the signature is valid, so attackers cannot poison it).

Token files hold one token per line; the first non-empty, non-comment line is the *current* token used
for signing, further lines are still accepted (rotation window). Tokens shorter than
``MIN_TOKEN_LENGTH`` are rejected. Every loaded token is registered with the global redactor so it can
never appear in logs or events.

This module must stay import-light (stdlib + httpx + hermclaw.core) because the worker daemons use it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.workers.errors import WorkerAuthError

log = get_logger(__name__)

HEADER_WORKER = "X-Hermclaw-Worker"
HEADER_TIMESTAMP = "X-Hermclaw-Timestamp"
HEADER_NONCE = "X-Hermclaw-Nonce"
HEADER_SIGNATURE = "X-Hermclaw-Signature"
HEADER_AUTHORIZATION = "Authorization"

SIGNATURE_SCHEME = "hermclaw-worker-v1"
SIGNATURE_PREFIX = "v1="
MAX_CLOCK_SKEW_SECONDS = 120
MIN_TOKEN_LENGTH = 32
EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()

_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_TS_RE = re.compile(r"^\d{1,12}$")
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SIG_RE = re.compile(r"^v1=[0-9a-f]{64}$")


# ----------------------------------------------------------------------------------------------- tokens
def generate_worker_token() -> str:
    """A fresh random worker token (64 url-safe characters, 384 bit)."""
    return secrets.token_urlsafe(48)


def parse_token_lines(text: str) -> list[str]:
    """Tokens from a token file: one per line, ``#`` comments and blank lines ignored, order preserved."""
    tokens: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line not in tokens:
            tokens.append(line)
    return tokens


def validate_tokens(tokens: Sequence[str], *, source: str) -> list[str]:
    out = list(tokens)
    if not out:
        raise WorkerAuthError(f"no worker token found in {source}", code="WORKER_TOKEN_MISSING")
    for tok in out:
        if len(tok) < MIN_TOKEN_LENGTH:
            raise WorkerAuthError(
                f"worker token from {source} is shorter than {MIN_TOKEN_LENGTH} characters",
                code="WORKER_TOKEN_TOO_SHORT",
            )
        DEFAULT_REDACTOR.add_literal(tok)
    return out


def load_token_file(path: Path) -> list[str]:
    """Read and validate a token file (see module docstring)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorkerAuthError(f"worker token file {path} not found", code="WORKER_TOKEN_MISSING") from exc
    except OSError as exc:
        raise WorkerAuthError(f"worker token file {path} unreadable: {exc.strerror}", code="WORKER_TOKEN_MISSING") from exc
    return validate_tokens(parse_token_lines(text), source=str(path))


def resolve_token_ref(ref: str, *, environ: Mapping[str, str] | None = None) -> list[str]:
    """Resolve a secret reference to the list of valid tokens.

    Supported references (same scheme as ``hermclaw.core.settings`` secret refs):

    - ``cred:<name>`` -> ``$CREDENTIALS_DIRECTORY/<name>`` (systemd ``LoadCredential``), fallback
      ``/etc/hermclaw/secrets/<name>``
    - ``file:<path>`` -> the file
    - ``env:<VAR>``   -> environment variable (development/test only; tokens separated by newlines or commas)
    """
    env = os.environ if environ is None else environ
    scheme, _, rest = ref.partition(":")
    if not rest:
        raise WorkerAuthError(f"invalid secret reference '{scheme}' (expected cred:/file:/env:)", code="WORKER_TOKEN_REF_INVALID")
    if scheme == "file":
        return load_token_file(Path(rest))
    if scheme == "cred":
        if "/" in rest or rest in {".", ".."}:
            raise WorkerAuthError("credential names must not contain '/'", code="WORKER_TOKEN_REF_INVALID")
        cred_dir = env.get("CREDENTIALS_DIRECTORY")
        candidates = [Path(cred_dir) / rest] if cred_dir else []
        candidates.append(Path("/etc/hermclaw/secrets") / rest)
        for cand in candidates:
            if cand.exists():
                return load_token_file(cand)
        raise WorkerAuthError(f"credential '{rest}' not found", code="WORKER_TOKEN_MISSING")
    if scheme == "env":
        value = env.get(rest, "")
        return validate_tokens(parse_token_lines(value.replace(",", "\n")), source=f"env:{rest}")
    raise WorkerAuthError(f"unsupported secret reference scheme '{scheme}'", code="WORKER_TOKEN_REF_INVALID")


class TokenStore(Protocol):
    """Looks up the valid tokens of a worker. The first token is the current (signing) token."""

    def tokens_for(self, worker_id: str) -> list[str]: ...


class StaticTokenStore:
    """In-memory token store (tests, tooling). Values may be a token or a list (current first)."""

    def __init__(self, tokens: Mapping[str, str | Sequence[str]]) -> None:
        self._tokens: dict[str, list[str]] = {}
        for worker_id, value in tokens.items():
            toks = [value] if isinstance(value, str) else list(value)
            self._tokens[worker_id] = validate_tokens(toks, source=f"static:{worker_id}")

    def tokens_for(self, worker_id: str) -> list[str]:
        return list(self._tokens.get(worker_id, []))


class RefTokenStore:
    """Token store backed by secret references (``Settings.worker_token_refs``).

    Files are re-read after ``ttl_seconds`` so tokens can be rotated without restarting the runtime.
    A worker without a reference has no valid token (requests are rejected).
    """

    def __init__(
        self,
        refs: Mapping[str, str],
        *,
        ttl_seconds: float = 30.0,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._refs = dict(refs)
        self._ttl = ttl_seconds
        self._environ = environ
        self._clock = clock
        self._cache: dict[str, tuple[float, list[str]]] = {}

    def tokens_for(self, worker_id: str) -> list[str]:
        ref = self._refs.get(worker_id)
        if not ref:
            return []
        now = self._clock()
        hit = self._cache.get(worker_id)
        if hit and now - hit[0] < self._ttl:
            return list(hit[1])
        try:
            tokens = resolve_token_ref(ref, environ=self._environ)
        except WorkerAuthError as exc:
            log.error("worker token unavailable", extra={"worker_id": worker_id, "code": exc.code, "error": exc.message})
            if hit:  # keep serving the last good tokens if the file vanished temporarily
                return list(hit[1])
            return []
        self._cache[worker_id] = (now, tokens)
        return list(tokens)


class TokenFile:
    """A worker daemon's own token file, re-read after ``ttl_seconds`` (rotation without restart)."""

    def __init__(self, path: Path, *, ttl_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self._ttl = ttl_seconds
        self._clock = clock
        self._loaded_at: float | None = None
        self._tokens: list[str] = []

    def tokens(self) -> list[str]:
        now = self._clock()
        if self._loaded_at is None or now - self._loaded_at >= self._ttl:
            try:
                self._tokens = load_token_file(self.path)
                self._loaded_at = now
            except WorkerAuthError:
                if not self._tokens:
                    raise
                log.error("worker token file unreadable, keeping previous tokens", extra={"path": str(self.path)})
        return list(self._tokens)

    def current(self) -> str:
        return self.tokens()[0]


# ----------------------------------------------------------------------------------------------- signing
def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest() if body else EMPTY_BODY_SHA256


def request_target(path: str, query: str | bytes = "") -> str:
    q = query.decode("latin-1") if isinstance(query, bytes) else query
    return f"{path}?{q}" if q else path


def canonical_string(method: str, target: str, timestamp: str, nonce: str, body_digest: str) -> str:
    return "|".join((SIGNATURE_SCHEME, method.upper(), target, timestamp, nonce, body_digest))


def compute_signature(token: str, *, method: str, target: str, timestamp: str, nonce: str, body: bytes) -> str:
    msg = canonical_string(method, target, timestamp, nonce, body_sha256(body)).encode("utf-8")
    return SIGNATURE_PREFIX + hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def sign_headers(
    *,
    worker_id: str,
    token: str,
    method: str,
    path: str,
    query: str | bytes = "",
    body: bytes = b"",
    now: float | None = None,
    nonce: str | None = None,
    include_bearer: bool = True,
) -> dict[str, str]:
    """Headers that authenticate one request (fresh timestamp + nonce every call)."""
    ts = str(int(time.time() if now is None else now))
    nn = nonce or secrets.token_hex(16)
    headers = {
        HEADER_WORKER: worker_id,
        HEADER_TIMESTAMP: ts,
        HEADER_NONCE: nn,
        HEADER_SIGNATURE: compute_signature(token, method=method, target=request_target(path, query), timestamp=ts, nonce=nn, body=body),
    }
    if include_bearer:
        headers[HEADER_AUTHORIZATION] = f"Bearer {token}"
    return headers


class ReplayCache:
    """Remembers ``(worker_id, nonce)`` pairs for the skew window; rejects exact replays.

    Bounded (oldest entries are evicted first). In-memory per process: a replay against a *restarted*
    process is still limited by the clock-skew window.
    """

    def __init__(self, *, window_seconds: float = 2 * MAX_CLOCK_SKEW_SECONDS, max_entries: int = 100_000) -> None:
        self._window = window_seconds
        self._max = max_entries
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()

    def __len__(self) -> int:
        return len(self._seen)

    def check_and_store(self, worker_id: str, nonce: str, *, now: float | None = None) -> bool:
        """``True`` if the pair is new (and stores it), ``False`` if it is a replay."""
        t = time.time() if now is None else now
        while self._seen:
            key, expires = next(iter(self._seen.items()))
            if expires > t and len(self._seen) < self._max:
                break
            self._seen.popitem(last=False)
        key = (worker_id, nonce)
        if key in self._seen:
            return False
        self._seen[key] = t + self._window
        return True


@dataclass(frozen=True)
class VerifiedRequest:
    worker_id: str
    timestamp: int
    nonce: str
    used_bearer: bool
    token_index: int  # 0 = current token, >0 = previous token (rotation in progress)


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


@dataclass(frozen=True)
class PrecheckedRequest:
    """Result of :func:`precheck_signed_request`: everything verified except signature and replay."""

    worker_id: str
    timestamp_raw: str
    timestamp: int
    nonce: str
    signature: str
    tokens: tuple[str, ...]
    used_bearer: bool
    checked_at: float


def precheck_signed_request(
    *,
    headers: Mapping[str, str],
    tokens_for: Callable[[str], Sequence[str]],
    expected_worker_id: str | None = None,
    max_skew_seconds: float = MAX_CLOCK_SKEW_SECONDS,
    require_bearer: bool = False,
    now: float | None = None,
) -> PrecheckedRequest:
    """Cheap header-only checks (presence/format, worker, clock skew, bearer token).

    Lets a server reject unauthenticated requests *before* reading a potentially large body. The
    signature itself needs the body digest and is checked by :func:`verify_prechecked`.
    """
    h = _lower_headers(headers)
    worker_id = h.get(HEADER_WORKER.lower(), "")
    ts_s = h.get(HEADER_TIMESTAMP.lower(), "")
    nonce = h.get(HEADER_NONCE.lower(), "")
    signature = h.get(HEADER_SIGNATURE.lower(), "")
    if not (_WORKER_ID_RE.match(worker_id) and _TS_RE.match(ts_s) and _NONCE_RE.match(nonce) and _SIG_RE.match(signature)):
        raise WorkerAuthError("missing or malformed authentication headers", code="WORKER_AUTH_MISSING")
    if expected_worker_id is not None and worker_id != expected_worker_id:
        raise WorkerAuthError("request is signed for a different worker", code="WORKER_AUTH_WRONG_WORKER")
    tokens = tuple(tokens_for(worker_id))
    if not tokens:
        raise WorkerAuthError("unknown worker or no credential configured", code="WORKER_AUTH_UNKNOWN")
    t = time.time() if now is None else now
    ts = int(ts_s)
    skew = abs(t - ts)
    if skew > max_skew_seconds:
        raise WorkerAuthError(
            f"request timestamp outside the allowed clock skew of {int(max_skew_seconds)} s",
            code="WORKER_AUTH_SKEW",
            details={"skew_seconds": int(skew)},
        )
    auth = h.get(HEADER_AUTHORIZATION.lower())
    used_bearer = False
    if auth is not None:
        scheme, _, presented = auth.partition(" ")
        presented_b = presented.strip().encode("utf-8")
        # evaluate every token (no short-circuit) so timing does not reveal which one matched
        hits = [hmac.compare_digest(presented_b, tok.encode("utf-8")) for tok in tokens]
        if scheme.lower() != "bearer" or not any(hits):
            raise WorkerAuthError("invalid bearer token", code="WORKER_AUTH_BAD_TOKEN")
        used_bearer = True
    elif require_bearer:
        raise WorkerAuthError("bearer token required", code="WORKER_AUTH_BAD_TOKEN")
    return PrecheckedRequest(
        worker_id=worker_id,
        timestamp_raw=ts_s,
        timestamp=ts,
        nonce=nonce,
        signature=signature,
        tokens=tokens,
        used_bearer=used_bearer,
        checked_at=t,
    )


def verify_prechecked(
    pre: PrecheckedRequest,
    *,
    method: str,
    path: str,
    query: str | bytes,
    body_digest: str,
    replay_cache: ReplayCache | None = None,
) -> VerifiedRequest:
    """Second stage: constant-time signature check against every valid token, then the replay cache
    (only after the signature is valid, so unauthenticated requests cannot poison the cache)."""
    target = request_target(path, query)
    msg = canonical_string(method, target, pre.timestamp_raw, pre.nonce, body_digest).encode("utf-8")
    presented = pre.signature.encode("ascii")
    matched: int | None = None
    for idx, tok in enumerate(pre.tokens):
        expected = (SIGNATURE_PREFIX + hmac.new(tok.encode("utf-8"), msg, hashlib.sha256).hexdigest()).encode("ascii")
        # compare every candidate (no early exit) so timing does not reveal which token matched
        if hmac.compare_digest(expected, presented) and matched is None:
            matched = idx
    if matched is None:
        raise WorkerAuthError("invalid request signature", code="WORKER_AUTH_BAD_SIGNATURE")
    if replay_cache is not None and not replay_cache.check_and_store(pre.worker_id, pre.nonce, now=pre.checked_at):
        raise WorkerAuthError("replayed request", code="WORKER_AUTH_REPLAY")
    return VerifiedRequest(
        worker_id=pre.worker_id,
        timestamp=pre.timestamp,
        nonce=pre.nonce,
        used_bearer=pre.used_bearer,
        token_index=matched,
    )


def verify_signed_request(
    *,
    method: str,
    path: str,
    query: str | bytes,
    headers: Mapping[str, str],
    body: bytes = b"",
    tokens_for: Callable[[str], Sequence[str]],
    body_digest: str | None = None,
    expected_worker_id: str | None = None,
    max_skew_seconds: float = MAX_CLOCK_SKEW_SECONDS,
    replay_cache: ReplayCache | None = None,
    require_bearer: bool = False,
    now: float | None = None,
) -> VerifiedRequest:
    """Verify a signed request; raises :class:`WorkerAuthError` with a specific ``code``.

    ``body_digest`` (hex SHA-256) may be passed instead of ``body`` when the body was hashed while
    streaming. Codes: ``WORKER_AUTH_MISSING`` (headers absent/malformed), ``WORKER_AUTH_WRONG_WORKER``,
    ``WORKER_AUTH_UNKNOWN``, ``WORKER_AUTH_SKEW``, ``WORKER_AUTH_BAD_TOKEN``,
    ``WORKER_AUTH_BAD_SIGNATURE``, ``WORKER_AUTH_REPLAY``.
    """
    pre = precheck_signed_request(
        headers=headers,
        tokens_for=tokens_for,
        expected_worker_id=expected_worker_id,
        max_skew_seconds=max_skew_seconds,
        require_bearer=require_bearer,
        now=now,
    )
    digest = body_digest if body_digest is not None else body_sha256(body)
    return verify_prechecked(pre, method=method, path=path, query=query, body_digest=digest, replay_cache=replay_cache)


class WorkerRequestSigner(httpx.Auth):
    """httpx auth flow that signs every outgoing request (incl. each retry) with a fresh nonce."""

    requires_request_body = True

    def __init__(
        self,
        worker_id: str,
        token: str | Callable[[], str],
        *,
        include_bearer: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.worker_id = worker_id
        self._token = token
        self.include_bearer = include_bearer
        self._clock = clock

    def _current_token(self) -> str:
        return self._token() if callable(self._token) else self._token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        headers = sign_headers(
            worker_id=self.worker_id,
            token=self._current_token(),
            method=request.method,
            path=request.url.path,
            query=request.url.query,
            body=request.content,
            now=self._clock(),
            include_bearer=self.include_bearer,
        )
        request.headers.update(headers)
        yield request
