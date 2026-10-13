"""Registering a *local* repository through ``POST /api/jobs`` (``file://`` URL or absolute path) needs the
``admin`` scope: such a URL lets the runtime clone anything the orchestrator user can read. ``control``
principals may register remote URLs and use repositories that are already registered; ``read`` principals cannot
create jobs at all."""

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
from hermclaw.core.settings import reset_settings_cache
from hermclaw.persistence.models import ApiToken, Event, Job, Repository
from hermclaw.security.tokens import generate_token, hash_token

pytestmark = pytest.mark.integration
ADMIN_TOKEN = "test-admin-token-repo-reg-123456"


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


def _job(url: str, **repo: Any) -> dict[str, Any]:
    return {"prompt": "Add a health endpoint", "repository": {"url": url, "base_branch": "main", **repo}}


async def _repo_rows(sm: async_sessionmaker[AsyncSession], url: str) -> int:
    async with sm() as s:
        return int((await s.execute(select(func.count()).select_from(Repository).where(Repository.url == url))).scalar_one())


async def _jobs_for(sm: async_sessionmaker[AsyncSession], prompt_marker: str) -> int:
    async with sm() as s:
        return int((await s.execute(select(func.count()).select_from(Job).where(Job.prompt.contains(prompt_marker)))).scalar_one())


def _unique_local(kind: str) -> str:
    path = f"/srv/git/reg-{uuid.uuid4().hex[:10]}.git"
    return f"file://{path}" if kind == "file" else path


@pytest.mark.parametrize("kind", ["file", "path"])
async def test_control_principal_cannot_register_local_repository(
    kind: str, control_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    url = _unique_local(kind)
    marker = uuid.uuid4().hex
    for candidate in (url, f"  {url}  "):  # surrounding whitespace is stripped before the check, not a bypass
        r = await control_client.post("/api/jobs", json={**_job(candidate), "prompt": f"Add a health endpoint {marker}"})
        assert r.status_code == 403, r.text
        err = r.json()["error"]
        assert err["code"] == "FORBIDDEN" and "admin" in err["message"]
    assert await _repo_rows(sessionmaker, url) == 0, "nothing is registered"
    assert await _jobs_for(sessionmaker, marker) == 0, "no job is created"
    async with sessionmaker() as s:
        registered = (await s.execute(select(Event).where(Event.event_type == "repository.registered"))).scalars().all()
    assert not [e for e in registered if e.payload.get("url") == url]


async def test_control_principal_with_unknown_name_and_local_url_is_forbidden(
    control_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    url = _unique_local("file")
    r = await control_client.post("/api/jobs", json=_job(url, name=f"named-{uuid.uuid4().hex[:8]}"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", r.text
    assert await _repo_rows(sessionmaker, url) == 0


async def test_read_principal_is_forbidden(app: FastAPI, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    url = _unique_local("file")
    async with _client(app, await _token(sessionmaker, "read")) as c:
        r = await c.post("/api/jobs", json=_job(url))
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", r.text
    assert await _repo_rows(sessionmaker, url) == 0


async def test_control_principal_may_register_remote_repository(
    control_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    url = f"https://git.example.invalid/team/remote-{uuid.uuid4().hex[:8]}.git"
    r = await control_client.post("/api/jobs", json=_job(url))
    assert r.status_code == 201, r.text
    assert await _repo_rows(sessionmaker, url) == 1
    async with sessionmaker() as s:
        ev = (await s.execute(select(Event).where(Event.event_type == "repository.registered"))).scalars().all()
    mine = [e for e in ev if e.payload.get("url") == url]
    assert len(mine) == 1 and mine[0].payload["local"] is False and (mine[0].source_id or "").startswith("control-")


@pytest.mark.parametrize("kind", ["file", "path"])
async def test_admin_registers_local_repository_and_control_may_use_it(
    kind: str, app: FastAPI, control_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    url = _unique_local(kind)
    async with _client(app, ADMIN_TOKEN) as admin:
        r = await admin.post("/api/jobs", json=_job(url))
    assert r.status_code == 201, r.text
    assert await _repo_rows(sessionmaker, url) == 1
    async with sessionmaker() as s:
        repo = (await s.execute(select(Repository).where(Repository.url == url))).scalar_one()
        ev = (await s.execute(select(Event).where(Event.event_type == "repository.registered"))).scalars().all()
    assert [e.payload["local"] for e in ev if e.payload.get("url") == url] == [True]
    # an already registered local repository is usable by every control principal (by url or by name)
    r = await control_client.post("/api/jobs", json=_job(url))
    assert r.status_code == 201, r.text
    assert r.json()["repository_id"] == str(repo.id)
    r = await control_client.post("/api/jobs", json={"prompt": "Use it by name", "repository": {"name": repo.name}})
    assert r.status_code == 201, r.text
    assert r.json()["repository_id"] == str(repo.id)
    assert await _repo_rows(sessionmaker, url) == 1, "no second registration"


async def test_admin_scoped_db_token_may_register_local_repository(app: FastAPI, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    url = _unique_local("file")
    async with _client(app, await _token(sessionmaker, "admin")) as c:
        r = await c.post("/api/jobs", json=_job(url))
    assert r.status_code == 201, r.text
    assert await _repo_rows(sessionmaker, url) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://oauth2:glpat-secret-token@git.example.invalid/team/creds.git",  # token smuggled into the URL
        "https://glpat-secret-token@git.example.invalid/team/creds.git",
        "ssh://git:hunter2@git.example.invalid/team/creds.git",
    ],
)
async def test_remote_urls_with_embedded_credentials_are_rejected(
    url: str, control_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    r = await control_client.post("/api/jobs", json=_job(url))
    assert r.status_code == 422, r.text
    assert "credentials" in r.json()["error"]["message"]
    assert await _repo_rows(sessionmaker, url) == 0
    async with sessionmaker() as s:
        ev = (await s.execute(select(Event).where(Event.event_type == "repository.registered"))).scalars().all()
    assert not [e for e in ev if "secret" in str(e.payload) or "hunter2" in str(e.payload)], "the secret never reaches an event"


async def test_remotes_with_the_same_tail_get_distinct_names(
    control_client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    tail = f"same-{uuid.uuid4().hex[:8]}"
    a, b = f"https://git.example.invalid/a/{tail}.git", f"https://git.example.invalid/b/{tail}.git"
    for url in (a, b):
        r = await control_client.post("/api/jobs", json=_job(url))
        assert r.status_code == 201, r.text
    async with sessionmaker() as s:
        names = {r.url: r.name for r in (await s.execute(select(Repository).where(Repository.url.in_([a, b])))).scalars()}
    assert set(names) == {a, b} and len(set(names.values())) == 2
    assert names[a] == tail and names[b].startswith(f"{tail}-")
