"""P07 7.5 worker auth: tokens, HMAC-SHA256 signing, skew, replay, tamper, rotation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.workers.auth import (
    HEADER_AUTHORIZATION,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    HEADER_WORKER,
    MAX_CLOCK_SKEW_SECONDS,
    RefTokenStore,
    ReplayCache,
    StaticTokenStore,
    TokenFile,
    WorkerRequestSigner,
    canonical_string,
    generate_worker_token,
    load_token_file,
    parse_token_lines,
    precheck_signed_request,
    resolve_token_ref,
    sign_headers,
    verify_signed_request,
)
from hermclaw.workers.errors import WorkerAuthError

TOKEN = "t" * 40 + "current-token-0123456789"
OLD_TOKEN = "o" * 40 + "previous-token-012345678"
NOW = 1_800_000_000.0


def _sign(method: str = "POST", path: str = "/api/workers/heartbeat", body: bytes = b'{"a":1}', **kw: object) -> dict[str, str]:
    return sign_headers(worker_id="exec-1", token=kw.pop("token", TOKEN), method=method, path=path, body=body, now=NOW, **kw)  # type: ignore[arg-type]


def _verify(headers: dict[str, str], *, method: str = "POST", path: str = "/api/workers/heartbeat", body: bytes = b'{"a":1}', **kw: object):  # type: ignore[no-untyped-def]
    tokens = kw.pop("tokens", {"exec-1": [TOKEN]})
    return verify_signed_request(
        method=method,
        path=path,
        query=kw.pop("query", ""),  # type: ignore[arg-type]
        headers=headers,
        body=body,
        tokens_for=lambda wid: tokens.get(wid, []),  # type: ignore[union-attr]
        now=kw.pop("now", NOW),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def _code(exc: pytest.ExceptionInfo[WorkerAuthError]) -> str:
    return exc.value.code


def test_sign_and_verify_roundtrip() -> None:
    headers = _sign()
    assert set(headers) == {HEADER_WORKER, HEADER_TIMESTAMP, HEADER_NONCE, HEADER_SIGNATURE, HEADER_AUTHORIZATION}
    assert headers[HEADER_SIGNATURE].startswith("v1=")
    v = _verify(headers)
    assert v.worker_id == "exec-1" and v.used_bearer and v.token_index == 0 and v.timestamp == int(NOW)


def test_canonical_string_covers_method_path_timestamp_nonce_body_hash() -> None:
    digest = hashlib.sha256(b"x").hexdigest()
    s = canonical_string("post", "/p?q=1", "123", "n" * 32, digest)
    assert s == f"hermclaw-worker-v1|POST|/p?q=1|123|{'n' * 32}|{digest}"


@pytest.mark.parametrize(
    "mutation",
    [
        {"body": b'{"a":2}'},
        {"path": "/api/workers/other"},
        {"method": "PUT"},
        {"query": "x=1"},
    ],
)
def test_tampered_request_is_rejected(mutation: dict[str, object]) -> None:
    headers = _sign()
    with pytest.raises(WorkerAuthError) as exc:
        _verify(headers, **mutation)
    assert _code(exc) == "WORKER_AUTH_BAD_SIGNATURE"


def test_tampered_headers_are_rejected() -> None:
    headers = _sign()
    headers[HEADER_TIMESTAMP] = str(int(NOW) + 1)
    with pytest.raises(WorkerAuthError) as exc:
        _verify(headers)
    assert _code(exc) == "WORKER_AUTH_BAD_SIGNATURE"
    headers = _sign()
    headers[HEADER_NONCE] = "0" * 32
    with pytest.raises(WorkerAuthError) as exc:
        _verify(headers)
    assert _code(exc) == "WORKER_AUTH_BAD_SIGNATURE"
    headers = _sign()
    sig = headers[HEADER_SIGNATURE]
    headers[HEADER_SIGNATURE] = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    with pytest.raises(WorkerAuthError) as exc:
        _verify(headers)
    assert _code(exc) == "WORKER_AUTH_BAD_SIGNATURE"


def test_wrong_token_signature_rejected_even_with_valid_format() -> None:
    headers = _sign(token="w" * 64, include_bearer=False)
    with pytest.raises(WorkerAuthError) as exc:
        _verify(headers)
    assert _code(exc) == "WORKER_AUTH_BAD_SIGNATURE"


@pytest.mark.parametrize("offset", [MAX_CLOCK_SKEW_SECONDS + 1, -(MAX_CLOCK_SKEW_SECONDS + 1), 3600])
def test_clock_skew_outside_window_rejected(offset: float) -> None:
    headers = _sign()
    with pytest.raises(WorkerAuthError) as exc:
        _verify(headers, now=NOW + offset)
    assert _code(exc) == "WORKER_AUTH_SKEW"
    assert exc.value.details["skew_seconds"] >= MAX_CLOCK_SKEW_SECONDS


@pytest.mark.parametrize("offset", [MAX_CLOCK_SKEW_SECONDS - 1, -(MAX_CLOCK_SKEW_SECONDS - 1), 0])
def test_clock_skew_inside_window_accepted(offset: float) -> None:
    assert _verify(_sign(), now=NOW + offset).worker_id == "exec-1"


def test_replay_is_rejected_but_fresh_nonce_accepted() -> None:
    cache = ReplayCache()
    headers = _sign()
    _verify(headers, replay_cache=cache)
    with pytest.raises(WorkerAuthError) as exc:
        _verify(headers, replay_cache=cache)
    assert _code(exc) == "WORKER_AUTH_REPLAY"
    # identical request content in the same second but a fresh nonce is a legitimate new request
    _verify(_sign(), replay_cache=cache)
    assert len(cache) == 2


def test_invalid_signature_does_not_poison_replay_cache() -> None:
    cache = ReplayCache()
    good = _sign()
    forged = dict(good)
    forged[HEADER_SIGNATURE] = "v1=" + "0" * 64
    with pytest.raises(WorkerAuthError):
        _verify(forged, replay_cache=cache)
    assert len(cache) == 0
    _verify(good, replay_cache=cache)


def test_replay_cache_expiry_and_bound() -> None:
    cache = ReplayCache(window_seconds=10, max_entries=3)
    assert cache.check_and_store("w", "a", now=0)
    assert not cache.check_and_store("w", "a", now=5)
    assert cache.check_and_store("w", "a", now=11)  # expired -> accepted again (skew check guards this)
    for i in range(5):
        cache.check_and_store("w", f"n{i}", now=12)
    assert len(cache) <= 3


def test_missing_and_malformed_headers() -> None:
    for drop in (HEADER_WORKER, HEADER_TIMESTAMP, HEADER_NONCE, HEADER_SIGNATURE):
        headers = _sign()
        headers.pop(drop)
        with pytest.raises(WorkerAuthError) as exc:
            _verify(headers)
        assert _code(exc) == "WORKER_AUTH_MISSING"
    headers = _sign()
    headers[HEADER_WORKER] = "../etc"
    with pytest.raises(WorkerAuthError) as exc:
        _verify(headers)
    assert _code(exc) == "WORKER_AUTH_MISSING"
    headers = _sign()
    headers[HEADER_SIGNATURE] = "sha1=abc"
    with pytest.raises(WorkerAuthError) as exc:
        _verify(headers)
    assert _code(exc) == "WORKER_AUTH_MISSING"


def test_header_names_are_case_insensitive() -> None:
    headers = {k.lower(): v for k, v in _sign().items()}
    assert _verify(headers).worker_id == "exec-1"


def test_unknown_and_wrong_worker() -> None:
    with pytest.raises(WorkerAuthError) as exc:
        _verify(_sign(), tokens={})
    assert _code(exc) == "WORKER_AUTH_UNKNOWN"
    with pytest.raises(WorkerAuthError) as exc:
        _verify(_sign(), expected_worker_id="model-1")
    assert _code(exc) == "WORKER_AUTH_WRONG_WORKER"


def test_bearer_token_checks() -> None:
    headers = _sign()
    headers[HEADER_AUTHORIZATION] = "Bearer " + "x" * 64
    with pytest.raises(WorkerAuthError) as exc:
        _verify(headers)
    assert _code(exc) == "WORKER_AUTH_BAD_TOKEN"
    headers = _sign()
    headers[HEADER_AUTHORIZATION] = f"Basic {TOKEN}"
    with pytest.raises(WorkerAuthError) as exc:
        _verify(headers)
    assert _code(exc) == "WORKER_AUTH_BAD_TOKEN"
    no_bearer = _sign(include_bearer=False)
    assert not _verify(no_bearer).used_bearer
    with pytest.raises(WorkerAuthError) as exc:
        _verify(no_bearer, require_bearer=True)
    assert _code(exc) == "WORKER_AUTH_BAD_TOKEN"


def test_token_rotation_previous_token_still_accepted() -> None:
    old_signed = _sign(token=OLD_TOKEN)
    v = _verify(old_signed, tokens={"exec-1": [TOKEN, OLD_TOKEN]})
    assert v.token_index == 1
    with pytest.raises(WorkerAuthError):
        _verify(_sign(token=OLD_TOKEN), tokens={"exec-1": [TOKEN]})


def test_precheck_rejects_before_body_and_digest_variant_matches() -> None:
    headers = _sign(body=b"payload")
    pre = precheck_signed_request(headers=headers, tokens_for=lambda _w: [TOKEN], now=NOW)
    assert pre.worker_id == "exec-1" and pre.used_bearer
    digest = hashlib.sha256(b"payload").hexdigest()
    v = verify_signed_request(
        method="POST",
        path="/api/workers/heartbeat",
        query="",
        headers=headers,
        tokens_for=lambda _w: [TOKEN],
        body_digest=digest,
        now=NOW,
    )
    assert v.worker_id == "exec-1"
    with pytest.raises(WorkerAuthError):
        precheck_signed_request(headers=headers, tokens_for=lambda _w: [TOKEN], now=NOW + 500)


# ------------------------------------------------------------------------------------------- tokens
def test_generate_token_length_and_uniqueness() -> None:
    a, b = generate_worker_token(), generate_worker_token()
    assert a != b and len(a) >= 64


def test_parse_token_lines_and_file(tmp_path: Path) -> None:
    assert parse_token_lines("# c\n\n a \nb\na\n") == ["a", "b"]
    path = tmp_path / "tok"
    path.write_text(f"# rotated 2026-10-08\n{TOKEN}\n{OLD_TOKEN}\n", encoding="utf-8")
    assert load_token_file(path) == [TOKEN, OLD_TOKEN]
    # loaded tokens are registered with the redactor (never in logs/events)
    assert TOKEN not in DEFAULT_REDACTOR.text(f"Authorization: Bearer {TOKEN}")


def test_token_file_errors(tmp_path: Path) -> None:
    with pytest.raises(WorkerAuthError) as exc:
        load_token_file(tmp_path / "missing")
    assert exc.value.code == "WORKER_TOKEN_MISSING"
    short = tmp_path / "short"
    short.write_text("abc\n", encoding="utf-8")
    with pytest.raises(WorkerAuthError) as exc:
        load_token_file(short)
    assert exc.value.code == "WORKER_TOKEN_TOO_SHORT"
    empty = tmp_path / "empty"
    empty.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(WorkerAuthError) as exc:
        load_token_file(empty)
    assert exc.value.code == "WORKER_TOKEN_MISSING"


def test_resolve_token_refs(tmp_path: Path) -> None:
    (tmp_path / "exec-token").write_text(TOKEN + "\n", encoding="utf-8")
    assert resolve_token_ref(f"file:{tmp_path / 'exec-token'}") == [TOKEN]
    assert resolve_token_ref("cred:exec-token", environ={"CREDENTIALS_DIRECTORY": str(tmp_path)}) == [TOKEN]
    assert resolve_token_ref("env:WT", environ={"WT": f"{TOKEN},{OLD_TOKEN}"}) == [TOKEN, OLD_TOKEN]
    for bad in ("nope", "s3:x", "cred:../x"):
        with pytest.raises(WorkerAuthError) as exc:
            resolve_token_ref(bad, environ={})
        assert exc.value.code == "WORKER_TOKEN_REF_INVALID"
    with pytest.raises(WorkerAuthError):
        resolve_token_ref("cred:absent-credential-name", environ={"CREDENTIALS_DIRECTORY": str(tmp_path)})


def test_ref_token_store_caches_rotates_and_survives_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "t"
    path.write_text(TOKEN, encoding="utf-8")
    clock = [0.0]
    store = RefTokenStore({"exec-1": f"file:{path}"}, ttl_seconds=10, clock=lambda: clock[0])
    assert store.tokens_for("exec-1") == [TOKEN]
    assert store.tokens_for("other") == []
    path.write_text(f"{OLD_TOKEN}\n{TOKEN}\n", encoding="utf-8")
    assert store.tokens_for("exec-1") == [TOKEN]  # cached
    clock[0] = 11
    assert store.tokens_for("exec-1") == [OLD_TOKEN, TOKEN]  # re-read after TTL
    path.unlink()
    clock[0] = 30
    assert store.tokens_for("exec-1") == [OLD_TOKEN, TOKEN]  # last good tokens kept
    fresh = RefTokenStore({"exec-1": f"file:{path}"})
    assert fresh.tokens_for("exec-1") == []


def test_static_store_and_token_file_rotation(tmp_path: Path) -> None:
    store = StaticTokenStore({"a": TOKEN, "b": [TOKEN, OLD_TOKEN]})
    assert store.tokens_for("b") == [TOKEN, OLD_TOKEN] and store.tokens_for("zz") == []
    with pytest.raises(WorkerAuthError):
        StaticTokenStore({"a": "short"})
    path = tmp_path / "tok"
    path.write_text(TOKEN, encoding="utf-8")
    clock = [0.0]
    tf = TokenFile(path, ttl_seconds=5, clock=lambda: clock[0])
    assert tf.current() == TOKEN
    path.write_text(OLD_TOKEN, encoding="utf-8")
    clock[0] = 6
    assert tf.current() == OLD_TOKEN
    path.unlink()
    clock[0] = 20
    assert tf.current() == OLD_TOKEN  # keeps previous token while the file is temporarily gone
    with pytest.raises(WorkerAuthError):
        TokenFile(tmp_path / "never").tokens()


async def test_request_signer_signs_each_request_and_server_verifies() -> None:
    seen: list[httpx.Request] = []
    cache = ReplayCache()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        verify_signed_request(
            method=request.method,
            path=request.url.path,
            query=request.url.query,
            headers=dict(request.headers),
            body=request.content,
            tokens_for=lambda _w: [TOKEN],
            replay_cache=cache,
            expected_worker_id="exec-1",
        )
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://w", auth=WorkerRequestSigner("exec-1", lambda: TOKEN)
    ) as client:
        r1 = await client.post("/v1/commands", content=json.dumps({"x": 1}).encode(), params={"a": "b c"})
        r2 = await client.get("/v1/models")
    assert r1.status_code == r2.status_code == 200
    assert seen[0].headers[HEADER_NONCE] != seen[1].headers[HEADER_NONCE]
