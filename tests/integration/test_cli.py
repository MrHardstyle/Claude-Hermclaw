"""Operator CLI against the real test database, git and config (installation smoke path)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from hermclaw.cli import main
from hermclaw.persistence.models import ApiToken, Job, JobInput
from hermclaw.security.tokens import hash_token
from tests.integration.test_gitops_support import make_upstream

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _json(capsys: Any) -> Any:
    return json.loads(capsys.readouterr().out)


async def test_config_check_and_schema_export(capsys: Any, tmp_path: Path) -> None:
    assert main(["config-check"]) == 0
    out = _json(capsys)
    assert out["coder_max_turns"] == 20 and "coder-main" in out["model_profiles"] and out["hosts"]
    assert main(["schemas", str(tmp_path / "schemas")]) == 0
    assert len(list((tmp_path / "schemas").glob("*.json"))) >= 10


async def test_litellm_config_generation(capsys: Any, tmp_path: Path) -> None:
    out = tmp_path / "litellm.yaml"
    assert main(["litellm-config", str(out)]) == 0
    text = out.read_text()
    for alias in ("fast-router", "planner-gemma", "coder-main", "heavy-review", "embedding"):
        assert alias in text


async def test_token_repo_and_job_commands(engine: Any, sessionmaker: Any, capsys: Any, tmp_path: Path) -> None:
    import asyncio

    loop_cli = lambda argv: asyncio.get_running_loop().run_in_executor(None, main, argv)  # noqa: E731 - CLI uses asyncio.run
    name = f"ops-{tmp_path.name[-8:]}"
    assert await loop_cli(["token-create", name, "--scopes", "read,control"]) == 0
    token = capsys.readouterr().out.strip().splitlines()[-1]
    async with sessionmaker() as s:
        row = (await s.execute(select(ApiToken).where(ApiToken.name == name))).scalar_one()
    assert row.token_hash == hash_token(token) and row.scopes == ["read", "control"]
    assert await loop_cli(["token-create", "bad", "--scopes", "root"]) == 2
    upstream = make_upstream(tmp_path / "up")
    repo_name = f"grp/cli-{tmp_path.name[-6:]}"
    assert await loop_cli(["repo-add", repo_name, upstream.url, "--provider", "generic"]) == 0
    assert _json(capsys)["name"] == repo_name
    assert await loop_cli(["repo-list"]) == 0
    assert repo_name in [r["name"] for r in _json(capsys)]
    assert await loop_cli(["job-submit", "Rename VALUE to LIMIT", "--repo", repo_name, "--constraint", "keep API stable"]) == 0
    job_id = _json(capsys)["job_id"]
    async with sessionmaker() as s:
        job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
        kinds = sorted(i.kind for i in (await s.execute(select(JobInput).where(JobInput.job_id == job.id))).scalars())
        job.status = "cancelled"  # keep other scheduler tests unaffected
        await s.commit()
    assert job.repository_id is not None and kinds == ["constraint", "prompt"]
    assert await loop_cli(["token-revoke", name]) == 0
