"""P07 worker client: signing, typed errors, timeouts, retries only for idempotent GETs."""

from __future__ import annotations

import httpx
import pytest

from hermclaw.contracts.common import WorkerKind, WorkerState
from hermclaw.contracts.worker import CommandRequest
from hermclaw.workers.auth import HEADER_SIGNATURE, StaticTokenStore, verify_signed_request
from hermclaw.workers.client import ExecutionWorkerClient, ModelWorkerClient, WorkerClient, probe_health
from hermclaw.workers.errors import (
    WorkerAuthFailed,
    WorkerBusy,
    WorkerProtocolError,
    WorkerRemoteError,
    WorkerTimeout,
    WorkerUnreachable,
)
from hermclaw.workers.schemas import WorkerInfo

TOKEN = "c" * 48 + "-client-token"
HEALTH = {"status": "ok", "worker_id": "exec-1", "kind": "execution", "state": "ready", "worker_version": "0.1.0rc1", "extra": 1}


def _client(handler: httpx.MockTransport, cls: type[WorkerClient] = ExecutionWorkerClient, **kw: object) -> WorkerClient:
    return cls("http://w", worker_id="exec-1", token=TOKEN, transport=handler, retry_backoff_seconds=0, **kw)  # type: ignore[arg-type]


async def test_requests_are_signed_and_unknown_fields_ignored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        verify_signed_request(
            method=request.method,
            path=request.url.path,
            query=request.url.query,
            headers=dict(request.headers),
            body=request.content,
            tokens_for=lambda _w: [TOKEN],
            expected_worker_id="exec-1",
        )
        return httpx.Response(200, json=HEALTH)

    async with _client(httpx.MockTransport(handler)) as c:
        h = await c.health()
    assert h.state == WorkerState.ready and h.kind == WorkerKind.execution


async def test_get_retried_on_503_and_transport_errors_then_succeeds() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers[HEADER_SIGNATURE])
        if len(calls) == 1:
            raise httpx.ConnectError("refused")
        if len(calls) == 2:
            return httpx.Response(503, json={"error": {"code": "X", "message": "warming up"}})
        return httpx.Response(200, json=HEALTH)

    async with _client(httpx.MockTransport(handler), get_retries=2) as c:
        assert (await c.health()).worker_id == "exec-1"
    assert len(calls) == 3 and len(set(calls)) == 3  # every retry is freshly signed (new nonce)


async def test_post_is_never_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"code": "WORKER_BUSY", "message": "no slot"}})

    req = CommandRequest(request_id="r1", job_id="j", step_id="s", workspace="ws", command="true")
    async with _client(httpx.MockTransport(handler), get_retries=5) as c:
        assert isinstance(c, ExecutionWorkerClient)
        with pytest.raises(WorkerBusy) as exc:
            await c.run_command(req)
    assert calls == 1 and exc.value.remote_code == "WORKER_BUSY"


async def test_error_mapping() -> None:
    responses = {
        "/a": httpx.Response(401, json={"error": {"code": "WORKER_AUTH_BAD_SIGNATURE", "message": "no"}}),
        "/b": httpx.Response(404, json={"error": {"code": "WORKSPACE_NOT_FOUND", "message": "gone", "details": {"workspace": "x"}}}),
        "/c": httpx.Response(500, text="plain failure"),
        "/d": httpx.Response(422, json={"detail": "bad"}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return responses[request.url.path]

    async with _client(httpx.MockTransport(handler), get_retries=0) as c:
        with pytest.raises(WorkerAuthFailed) as auth:
            await c._request("GET", "/a")
        assert auth.value.details["remote_code"] == "WORKER_AUTH_BAD_SIGNATURE"
        with pytest.raises(WorkerRemoteError) as nf:
            await c._request("GET", "/b")
        assert nf.value.status_code == 404 and nf.value.remote_code == "WORKSPACE_NOT_FOUND"
        assert nf.value.details["remote_details"] == {"workspace": "x"}
        with pytest.raises(WorkerRemoteError) as err:
            await c._request("GET", "/c")
        assert err.value.status_code == 500 and err.value.remote_code is None
        with pytest.raises(WorkerRemoteError) as val:
            await c._request("GET", "/d")
        assert "bad" in val.value.message


async def test_timeouts_and_unreachable() -> None:
    def read_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def connect_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=request)

    async with _client(httpx.MockTransport(read_timeout), get_retries=1) as c:
        with pytest.raises(WorkerTimeout) as exc:
            await c.health()
        assert exc.value.details["attempt"] == 2
    async with _client(httpx.MockTransport(connect_timeout), get_retries=0) as c:
        with pytest.raises(WorkerUnreachable):
            await c.health()


async def test_protocol_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/commands":
            return httpx.Response(200, json={"request_id": "other", "exit_code": 0})
        if request.url.path.endswith("/archive"):
            return httpx.Response(200, json={"not": "a tar"})
        return httpx.Response(200, json={"status": "ok"})

    async with _client(httpx.MockTransport(handler)) as c:
        assert isinstance(c, ExecutionWorkerClient)
        with pytest.raises(WorkerProtocolError):
            await c.health()
        with pytest.raises(WorkerProtocolError):
            await c.run_command(CommandRequest(request_id="mine", job_id="j", step_id="s", workspace="ws", command="true"))
        with pytest.raises(WorkerProtocolError):
            await c.download_workspace("ws")
        with pytest.raises(ValueError):
            await c.upload_workspace("ws", b"x", mode="append")


async def test_model_client_parameters() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/models/load":
            return httpx.Response(200, json={"model": "qwen3:8b", "loaded": True, "context_length": 8192})
        return httpx.Response(200, json={"model": "qwen3:8b", "unloaded": True})

    from hermclaw.contracts.worker import ModelLoadRequest

    async with _client(httpx.MockTransport(handler), cls=ModelWorkerClient) as c:
        assert isinstance(c, ModelWorkerClient)
        res = await c.load_model(ModelLoadRequest(model="qwen3:8b", context_tokens=8192), exclusive=True, keep=["emb", "x"])
        await c.unload_model("qwen3:8b")
    assert res.context_length == 8192
    assert seen[0].url.params.get_list("keep") == ["emb", "x"] and seen[0].url.params["exclusive"] == "true"


def test_for_worker_requires_api_url_and_credential() -> None:
    info = WorkerInfo(id="exec-1", hostname="h", address="a", kind=WorkerKind.execution, state=WorkerState.ready, api_url=None)
    with pytest.raises(WorkerUnreachable):
        ExecutionWorkerClient.for_worker(info, StaticTokenStore({}))
    info2 = info.model_copy(update={"api_url": "http://192.168.178.222:8787"})
    with pytest.raises(WorkerAuthFailed):
        ExecutionWorkerClient.for_worker(info2, StaticTokenStore({}))
    c = ExecutionWorkerClient.for_worker(info2, StaticTokenStore({"exec-1": TOKEN}))
    assert c.base_url == "http://192.168.178.222:8787" and c.worker_id == "exec-1"


async def test_probe_health_errors() -> None:
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(WorkerRemoteError):
        await probe_health("http://w", transport=httpx.MockTransport(bad))

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(WorkerUnreachable):
        await probe_health("http://w", transport=httpx.MockTransport(refused))

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"x": 1})

    with pytest.raises(WorkerProtocolError):
        await probe_health("http://w", transport=httpx.MockTransport(garbage))


@pytest.mark.parametrize(
    ("health", "code"),
    [
        ({**HEALTH, "protocol_version": 999}, "WORKER_INCOMPATIBLE"),
        ({**HEALTH, "worker_id": "exec-other"}, "WORKER_IDENTITY_MISMATCH"),
    ],
)
async def test_ensure_compatible_rejects_wrong_protocol_or_identity(health: dict[str, object], code: str) -> None:
    async with _client(httpx.MockTransport(lambda _r: httpx.Response(200, json=health))) as c:
        with pytest.raises(WorkerProtocolError) as exc:
            await c.ensure_compatible()
    assert exc.value.code == code


async def test_ensure_compatible_accepts_matching_daemon() -> None:
    async with _client(httpx.MockTransport(lambda _r: httpx.Response(200, json=HEALTH))) as c:
        assert (await c.ensure_compatible()).worker_id == "exec-1"


@pytest.mark.parametrize("workspace", ["..", ".", "a/b", "../commands", "", "-x", "x" * 129])
async def test_client_rejects_unsafe_workspace_ids_without_sending(workspace: str) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    async with _client(httpx.MockTransport(handler)) as c:
        assert isinstance(c, ExecutionWorkerClient)
        for call in (
            c.upload_workspace(workspace, b"tar"),
            c.download_workspace(workspace),
            c.workspace_manifest(workspace),
            c.workspace_info(workspace),
            c.delete_paths(workspace, ["a"]),
            c.delete_workspace(workspace),
            c.run_command(CommandRequest(request_id="r1", job_id="j", step_id="s", workspace=workspace, command="true")),
        ):
            with pytest.raises(ValueError):
                await call
    assert sent == []
