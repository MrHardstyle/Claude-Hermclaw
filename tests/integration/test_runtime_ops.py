"""P26 integration into the runtime: the composition root builds the operations-tool provider (SSH), the scheduler
process runs the idle-sleep hook over the SSH runner (BUG-071), and operators approve mutating SSH commands through
the API (admin scope, single use)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.api.app import create_app
from hermclaw.api.auth import reset_auth_cache
from hermclaw.contracts.events import EventType
from hermclaw.contracts.tools import SSH_TOOLS, ToolName
from hermclaw.core.config import get_config
from hermclaw.core.settings import Settings, reset_settings_cache
from hermclaw.events.store import append_event
from hermclaw.persistence.models import ApiToken, Event, Job
from hermclaw.runtime.main import SchedulerProcess
from hermclaw.runtime.services import ServiceOverrides, build_services
from hermclaw.security.tokens import generate_token, hash_token
from hermclaw.sshtools.client import SshRunner
from hermclaw.sshtools.policy import approval_token
from tests.integration.runtime_stack import StackModels

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]
ADMIN_TOKEN = "test-admin-token-ssh-approvals-1234"


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {"env": "test", "data_dir": tmp_path / "data", "execution_mode": "local", "wake_on_lan": False}
    base.update(kw)
    return Settings(**base)


async def test_composition_root_builds_the_ssh_operations_provider(sessionmaker: Any, tmp_path: Path) -> None:
    services = build_services(get_config(), _settings(tmp_path), sessionmaker, overrides=ServiceOverrides(chat=StackModels()))
    try:
        assert services.ops is not None and services.ops.names[0] == "ssh"
        assert isinstance(services.ops.ssh_runner, SshRunner)
        provider = services.ops.provider
        assert provider is not None and all(provider.provides(t) for t in SSH_TOOLS)
        implement = next(h for h in services.handlers if "ssh" in h.kinds)
        assert implement.deps.ops is provider, "ssh/deploy/database/docker steps get the operations tools"  # type: ignore[attr-defined]
        assert not provider.provides(ToolName.write_file)
    finally:
        await services.aclose()


async def test_scheduler_process_runs_idle_sleep_over_the_ssh_runner(sessionmaker: Any, tmp_path: Path) -> None:
    config = get_config()
    assert any(h.idle_sleep_command for h in config.hosts.hosts), "the shipped hosts example has idle sleep hooks"
    off = SchedulerProcess(config, _settings(tmp_path), overrides=ServiceOverrides(chat=StackModels()))
    services = build_services(config, _settings(tmp_path), sessionmaker, overrides=ServiceOverrides(chat=StackModels()))
    try:
        assert off._idle_sleep_task(services) is None, "Wake-on-LAN disabled: no idle sleep"
        on = SchedulerProcess(config, _settings(tmp_path, wake_on_lan=True), overrides=ServiceOverrides(chat=StackModels()))
        task = on._idle_sleep_task(services)
        assert task is not None and on.idle_sleep is not None
        assert on.idle_sleep._runner is services.ops.ssh_runner  # type: ignore[union-attr]
        on._stop.set()
        await asyncio.wait_for(task, timeout=30)
        await on.engine.dispose()
    finally:
        await services.aclose()
        await off.engine.dispose()


# ------------------------------------------------------------------------------------------------ approvals API


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


async def test_operator_approves_and_revokes_a_pending_ssh_command(app: FastAPI, sessionmaker: Any) -> None:
    step_id = uuid.uuid4()
    token = approval_token("webui-223", "sudo /usr/bin/systemctl reload nginx", step_id)
    async with sessionmaker() as s:
        job = Job(title="reload nginx", prompt="reload nginx on the web host")
        s.add(job)
        await s.flush()
        job_id = job.id
        await append_event(
            s,
            EventType.SSH_COMMAND,
            source_type="sshtools",
            job_id=job_id,
            step_id=step_id,
            payload={
                "tool": "ssh_command",
                "host": "webui-223",
                "outcome": "approval_required",
                "command": "sudo /usr/bin/systemctl reload nginx",
                "classification": "mutate",
                "approval": token,
            },
        )
        await s.commit()
    read, control, admin = await _token(sessionmaker, "read"), await _token(sessionmaker, "control"), await _token(sessionmaker, "admin")
    url = f"/api/jobs/{job_id}/ssh-approvals"
    async with _client(app, read) as c:
        listed = (await c.get(url)).json()
        assert [p["token"] for p in listed["pending"]] == [token] and listed["pending"][0]["host"] == "webui-223"
        assert (await c.post(url, json={"token": token})).status_code == 403
    async with _client(app, control) as c:
        assert (await c.post(url, json={"token": token})).status_code == 403, "approving a host change needs admin"
    async with _client(app, admin) as c:
        r = await c.post(url, json={"token": token, "note": "reload after config check"})
        assert r.status_code == 201 and r.json()["state"] == "approved"
        assert (await c.post(url, json={"token": token})).status_code == 409, "already approved and unused"
        listed = (await c.get(url)).json()
        assert listed["pending"] == [] and listed["approved"][0]["token"] == token
        assert listed["approved"][0]["note"] == "reload after config check"
        assert (await c.post(url, json={"token": "not-a-token"})).status_code in (400, 422)
        assert (await c.delete(f"{url}/{token}")).json()["state"] == "revoked"
        assert (await c.delete(f"{url}/{token}")).status_code == 404
        assert (await c.get(f"/api/jobs/{uuid.uuid4()}/ssh-approvals")).status_code == 404
    async with sessionmaker() as s:
        kinds = list((await s.execute(select(Event.event_type).where(Event.job_id == job_id).order_by(Event.sequence))).scalars())
    assert kinds == [EventType.SSH_COMMAND, EventType.SSH_APPROVAL_GRANTED, EventType.SSH_APPROVAL_REVOKED]


async def test_missing_optional_provider_is_skipped_with_info_logging(
    sessionmaker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: the skip log used the reserved LogRecord key 'module' and crashed the scheduler at INFO level."""
    import logging

    from hermclaw.runtime import ops

    monkeypatch.setattr(ops, "OPTIONAL_PROVIDERS", ("hermclaw.not_installed_provider_pkg.provider",))
    caplog.set_level(logging.INFO, logger="hermclaw.runtime.ops")
    bundle = ops.build_ops(get_config(), sessionmaker, _settings(tmp_path))
    assert bundle.names == ["ssh"]
    assert any("not installed" in r.getMessage() for r in caplog.records)
