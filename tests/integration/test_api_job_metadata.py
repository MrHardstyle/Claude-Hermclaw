"""``POST /api/jobs`` refuses client metadata that would plant runtime-reserved keys (BUG-140).

Before the fix ``create_job`` stored ``{**body.metadata, …}`` unfiltered: a principal with only the ``control`` scope
could plant ``ssh_approvals`` (operator approvals of SSH mutations – otherwise admin only), ``triage`` (skips the fast
router, steers the planner, a non-dict crashes the job), ``replan_requested`` (a falsy value wedged the job forever)
and the other keys the runtime writes and trusts. Each is now refused with 422 before anything is written.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.api.app import create_app
from hermclaw.api.auth import reset_auth_cache
from hermclaw.core import job_metadata as jm
from hermclaw.core.settings import reset_settings_cache
from hermclaw.persistence.models import ApiToken, Event, Job, JobInput, Repository
from hermclaw.security.tokens import generate_token, hash_token
from hermclaw.sshtools.approvals import approval_state, consume_approval
from hermclaw.sshtools.policy import approval_token

pytestmark = pytest.mark.integration
ADMIN_TOKEN = "test-admin-token-job-metadata-123456"

# the predictable approval token of an SSH call without a step (``step_id or '-'``): the worst case for planting
HOST, COMMAND = "prod-web-1", "systemctl restart nginx"
TOKEN = approval_token(HOST, COMMAND, None)

PLANTED: dict[str, Any] = {
    jm.SSH_APPROVALS: [TOKEN],
    jm.SSH_APPROVALS_CONSUMED: [{"token": TOKEN, "approved_by": "admin", "consumed_at": "2026-10-15T00:00:00+00:00"}],
    jm.TRIAGE: "x",
    jm.REPLAN_REQUESTED: False,
    jm.REPLAN_REASON: "planted",
    jm.RETRY_REQUESTED: True,
    jm.RECOVERED_PREPARE_FROM: "planning",
    jm.PLANNER_MODEL_ALIAS: "planner-gemma",
    jm.PLANNER_MODEL_FALLBACK: True,
    jm.PLANNER_FALLBACK_ALIAS: "planner-gemma-fallback",
    jm.LAST_REPLAN: {"version": 9, "reason_code": "operator_request"},
    jm.ALLOW_NETWORK_RESEARCH: True,
    jm.AUTO_COMMIT: True,
    jm.CREATE_MERGE_REQUEST: True,
}


@pytest.fixture
def app(engine: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FastAPI]:
    monkeypatch.setenv("HERMCLAW_API_TOKEN_REF", f"literal:{ADMIN_TOKEN}")
    monkeypatch.setenv("HERMCLAW_DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    reset_auth_cache()
    from hermclaw.api.state import AppState

    a = create_app(lifespan_hooks=False)
    a.state.hermclaw = AppState()
    yield a
    reset_settings_cache()
    reset_auth_cache()


async def _token(sm: async_sessionmaker[AsyncSession], *scopes: str) -> str:
    token = generate_token()
    async with sm() as s:
        s.add(ApiToken(name=f"{'-'.join(scopes)}-{uuid.uuid4().hex[:6]}", token_hash=hash_token(token), scopes=list(scopes)))
        await s.commit()
    return token


def _client(app: FastAPI, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
async def control_client(app: FastAPI, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(app, await _token(sessionmaker, "control")) as c:
        yield c


@pytest.fixture
async def admin_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(app, ADMIN_TOKEN) as c:
        yield c


async def _counts(sm: async_sessionmaker[AsyncSession]) -> tuple[int, ...]:
    async with sm() as s:
        return tuple([int((await s.execute(select(func.count()).select_from(t))).scalar_one()) for t in (Job, JobInput, Repository, Event)])


def _body(metadata: dict[str, Any], **extra: Any) -> dict[str, Any]:
    marker = uuid.uuid4().hex[:10]
    return {
        "prompt": f"Restart the web tier {marker}",
        # a new remote URL: a refused request must not persist it (the API session is rolled back on refusal)
        "repository": {"url": f"https://gitlab.example.com/ops/web-{marker}.git", "base_branch": "main"},
        "constraints": ["no downtime"],
        "metadata": metadata,
        **extra,
    }


def _assert_refused(r: httpx.Response, *keys: str) -> None:
    """422 naming every offending key. The contract strips surrounding whitespace from dict keys
    (``str_strip_whitespace``) – before the fix ``" ssh_approvals "`` therefore arrived as the trusted key itself."""
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == jm.RESERVED_KEY_ERROR
    received = sorted(k.strip() for k in keys)
    assert err["details"]["reserved_keys"] == received
    for key in received:
        assert repr(key) in err["message"]


@pytest.mark.parametrize("key", sorted(PLANTED))
async def test_each_reserved_key_is_refused_before_anything_is_written(
    key: str, control_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    before = await _counts(sessionmaker)
    r = await control_client.post("/api/jobs", json=_body({"ticket": "OPS-1", key: PLANTED[key]}))
    _assert_refused(r, key)
    assert await _counts(sessionmaker) == before, "no job, job input, repository or event may be written"


@pytest.mark.parametrize(
    "key",
    [" SSH_Approvals", "SSH_APPROVALS\t", "ssh_approvals\u200b", "Triage", "REPLAN_REQUESTED ", "_internal", "hermclaw.state"],
)
async def test_spelling_variants_and_reserved_prefixes_are_refused(
    key: str, control_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    before = await _counts(sessionmaker)
    _assert_refused(await control_client.post("/api/jobs", json=_body({key: [TOKEN]})), key)
    assert await _counts(sessionmaker) == before


async def test_reserved_key_is_checked_before_repository_resolution(
    control_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The metadata check runs first (api.md §1): a control token registering a local repository would otherwise get
    403 FORBIDDEN from _resolve_repository, and an unknown name would get VALIDATION_FAILED. The counts alone cannot
    show the order – a late refusal is rolled back as well."""
    before = await _counts(sessionmaker)
    marker = uuid.uuid4().hex[:8]
    for repository in ({"url": f"file:///srv/git/planted-{marker}.git"}, {"name": f"unregistered-{marker}"}):
        r = await control_client.post("/api/jobs", json=_body({jm.SSH_APPROVALS: [TOKEN]}, repository=repository))
        _assert_refused(r, jm.SSH_APPROVALS)
    assert await _counts(sessionmaker) == before


async def test_all_offending_keys_are_named(control_client: httpx.AsyncClient) -> None:
    r = await control_client.post("/api/jobs", json=_body({"ticket": 1, "triage": {"risk": "low"}, " SSH_Approvals": [TOKEN]}))
    _assert_refused(r, "triage", " SSH_Approvals")
    assert "ticket" not in r.json()["error"]["details"]["reserved_keys"]


async def test_admin_cannot_plant_reserved_keys_either(
    admin_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Approvals go through the audited ``/ssh-approvals`` endpoint (event, ``approved_by``), never job metadata."""
    before = await _counts(sessionmaker)
    _assert_refused(await admin_client.post("/api/jobs", json=_body({jm.SSH_APPROVALS: [TOKEN]})), jm.SSH_APPROVALS)
    assert await _counts(sessionmaker) == before


async def test_free_form_metadata_is_stored_and_the_typed_fields_win(
    control_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    free = {"ticket": "OPS-42", "sandbox_image": "node", "labels": ["web", "nginx"], "nested": {"triage": "is fine nested"}}
    body = _body(free, allow_network_research=False, auto_commit=False, create_merge_request=True)
    r = await control_client.post("/api/jobs", json=body)
    assert r.status_code == 201, r.text
    job = r.json()
    expected = {**free, jm.ALLOW_NETWORK_RESEARCH: False, jm.AUTO_COMMIT: False, jm.CREATE_MERGE_REQUEST: True}
    assert job["metadata"] == expected
    assert (await control_client.get(f"/api/jobs/{job['id']}")).json()["metadata"] == expected
    async with sessionmaker() as s:
        row = (await s.execute(select(Job).where(Job.id == uuid.UUID(job["id"])))).scalar_one()
        assert row.metadata_ == expected
        assert (await s.execute(select(Repository).where(Repository.url == body["repository"]["url"]))).scalar_one_or_none() is not None
    # runtime/API writers are not affected by the create filter
    assert (await control_client.post(f"/api/jobs/{job['id']}/replan", params={"reason": "new info"})).status_code == 200
    assert (await control_client.get(f"/api/jobs/{job['id']}")).json()["metadata"][jm.REPLAN_REQUESTED] == "new info"


async def test_defaults_without_metadata(control_client: httpx.AsyncClient) -> None:
    r = await control_client.post("/api/jobs", json={"prompt": "Write a short summary"})
    assert r.status_code == 201, r.text
    assert r.json()["metadata"] == {jm.ALLOW_NETWORK_RESEARCH: True, jm.AUTO_COMMIT: True, jm.CREATE_MERGE_REQUEST: None}


async def test_control_token_cannot_pre_approve_an_ssh_mutation(
    control_client: httpx.AsyncClient, admin_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Regression BUG-140: the control scope could plant ``ssh_approvals`` and skip the admin-only approval."""
    for planted in (
        {jm.SSH_APPROVALS: [TOKEN]},
        {jm.SSH_APPROVALS: [{"token": TOKEN, "approved_by": "admin", "approved_at": "2026-10-15T00:00:00+00:00"}]},
        {" Ssh_Approvals ": [TOKEN]},
        {jm.SSH_APPROVALS_CONSUMED: [{"token": TOKEN, "approved_by": "admin"}]},
    ):
        before = await _counts(sessionmaker)
        r = await control_client.post("/api/jobs", json=_body(planted))
        assert r.status_code == 422, r.text
        assert await _counts(sessionmaker) == before

    r = await control_client.post("/api/jobs", json=_body({"ticket": "OPS-7"}))
    assert r.status_code == 201, r.text
    job_id = uuid.UUID(r.json()["id"])
    listing = (await control_client.get(f"/api/jobs/{job_id}/ssh-approvals")).json()
    assert listing["approved"] == [] and listing["consumed"] == []
    async with sessionmaker() as s:
        assert await approval_state(s, job_id, TOKEN) == "missing"
        assert await consume_approval(s, job_id, TOKEN, host_id=HOST, step_id=None) is False
        await s.rollback()

    # approving stays admin-only …
    assert (await control_client.post(f"/api/jobs/{job_id}/ssh-approvals", json={"token": TOKEN})).status_code == 403
    assert (await admin_client.post(f"/api/jobs/{job_id}/ssh-approvals", json={"token": TOKEN})).status_code == 201
    # … and is what makes the command runnable (positive control of the consume path used above)
    async with sessionmaker() as s:
        assert await consume_approval(s, job_id, TOKEN, host_id=HOST, step_id=None) is True
        await s.commit()
    consumed = (await control_client.get(f"/api/jobs/{job_id}/ssh-approvals")).json()["consumed"]
    assert [c["token"] for c in consumed] == [TOKEN] and consumed[0]["approved_by"] == "admin"
