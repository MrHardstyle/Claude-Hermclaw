"""R-026: jobs without a repository get a runtime-created scratch repository + workspace.

* :class:`ScratchWorkspaces` against PostgreSQL and the real git CLI (bare repo, registration, idempotence);
* a full scheduler run of a repository-less operations job: an ``ssh`` step (read-only deny-all-write scope, the
  operations tool answered by a fake provider) followed by a ``documentation`` step that writes a report; finalize
  pushes the job branch into the local scratch repository and never creates a merge request."""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update

from hermclaw.contracts.tools import ToolName
from hermclaw.core.config import get_config
from hermclaw.core.settings import Settings
from hermclaw.models.protocols import CallContext, ChatMessage, StructuredResult
from hermclaw.persistence.models import Artifact, Job, Repository, ScopeContractRow, Step
from hermclaw.runtime.main import SchedulerProcess
from hermclaw.runtime.scratch import ScratchWorkspaces, scratch_repository_name
from hermclaw.runtime.services import ServiceOverrides
from hermclaw.scheduler.scheduler import SchedulerSettings
from hermclaw.tools.ops import OpsCall, OpsResult
from tests.integration.runtime_stack import StackModels, act
from tests.integration.test_gitops_support import World, make_world
from tests.unit.test_planner_support import plan, step

pytestmark = pytest.mark.asyncio(loop_scope="session")
TERMINAL = ("succeeded", "failed", "cancelled")
T = TypeVar("T", bound=BaseModel)
MARK = "FAKE-OPS: nginx.service active (running) since boot"


@pytest.fixture
async def world(sessionmaker: Any, tmp_path: Path) -> World:
    async with sessionmaker() as s:  # the DB is shared per test session: neutralise other tests' open jobs
        await s.execute(update(Job).where(Job.status.not_in(TERMINAL)).values(status="cancelled"))
        await s.commit()
    return await make_world(sessionmaker, tmp_path)


async def _repo_less_job(world: World, title: str = "Check nginx on the web host") -> uuid.UUID:
    async with world.sessionmaker() as s:
        job = Job(title=title, prompt="Check whether nginx runs on web-223 and write a short report.", repository_id=None)
        s.add(job)
        await s.commit()
        return job.id


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


# ------------------------------------------------------------------------------------------------ ScratchWorkspaces


async def test_scratch_workspace_is_created_once_and_only_for_repository_less_jobs(world: World) -> None:
    scratch = ScratchWorkspaces(world.sessionmaker, world.engine, world.root / "scratch")
    jid = await _repo_less_job(world)
    assert await scratch.existing(jid) is None
    ws = await scratch.ensure(jid)
    assert ws is not None and ws.status == "active" and ws.repository_name == scratch_repository_name(jid)
    bare = world.root / "scratch" / f"{jid}.git"
    assert _git(bare, "rev-parse", "--is-bare-repository") == "true"
    assert _git(bare, "show", "main:README.md").startswith("# Hermclaw scratch repository")
    assert Path(ws.path, "README.md").is_file() and _git(Path(ws.path), "branch", "--show-current") == ws.branch
    async with world.sessionmaker() as s:
        repo = (await s.execute(select(Repository).where(Repository.id == ws.repository_id))).scalar_one()
        job = (await s.execute(select(Job).where(Job.id == jid))).scalar_one()
    assert repo.provider == "generic" and repo.metadata_["scratch"] is True and repo.url == bare.as_uri()
    assert job.repository_id is None, "the job itself stays repository-less"
    again = await scratch.ensure(jid)
    assert again is not None and again.id == ws.id
    assert (await scratch.existing(jid)) == again
    # a job with a repository never gets a scratch workspace
    assert await scratch.ensure(await world.job()) is None


# ------------------------------------------------------------------------------------------------ full scheduler run


class FakeSsh:
    def __init__(self) -> None:
        self.calls: list[OpsCall] = []

    def provides(self, tool: ToolName) -> bool:
        return tool == ToolName.ssh_service_status

    async def call(self, call: OpsCall) -> OpsResult:
        self.calls.append(call)
        return OpsResult(True, MARK, data={"unit": "nginx.service", "active": True})


class OpsModels(StackModels):
    """Coder: the ssh step queries the service, the documentation step writes the report from the tool result."""

    async def structured(  # type: ignore[override]
        self, alias: str, messages: list[ChatMessage], schema: type[T], *, ctx: CallContext, **kw: Any
    ) -> StructuredResult[T]:
        if alias != "coder-main":
            return await super().structured(alias, messages, schema, ctx=ctx, **kw)
        self.aliases.append(alias)
        self.coder_calls += 1
        prompt = "\n".join(m.content for m in messages)
        if "Write the findings" in prompt:
            if "+# nginx on web-223" in prompt:
                data = act("complete_step", summary="report written", changed_files=["ops/report.md"], tests_run=[])
            else:
                data = act("write_file", path="ops/report.md", content="# nginx on web-223\n\nnginx.service is active (running).\n")
        elif MARK in prompt:
            data = act("complete_step", summary="nginx.service is active on web-223", changed_files=[], tests_run=[])
        else:
            data = act("ssh_service_status", host="web-223", unit="nginx.service")
        return self._ok(schema, data, alias, "qwen3-coder:30b")


def ops_plan() -> dict[str, Any]:
    return plan(
        "Check nginx on web-223 and document the result",
        [
            step(
                "S001",
                "ssh",
                "ssh",
                "Check the nginx service status on web-223 (read-only, no changes).",
                acceptance=[{"type": "artifact", "kind": "ops_output", "name_glob": "ssh_service_status*"}],
            ),
            step(
                "S002",
                "documentation",
                "documentation",
                "Write the findings of S001 to ops/report.md.",
                depends_on=["S001"],
                repo_hints=["ops/report.md"],
                acceptance=[{"type": "presence", "path_glob": "ops/report.md", "pattern": "nginx.service is active"}],
            ),
        ],
    )


async def test_repository_less_ops_job_runs_in_a_scratch_workspace(world: World) -> None:
    models, ssh = OpsModels([ops_plan()]), FakeSsh()
    settings = Settings(
        env="test",
        data_dir=world.settings.data_dir,
        instance_id=f"test-{uuid.uuid4().hex[:8]}",
        execution_mode="local",
        wake_on_lan=False,
        scheduler_poll_seconds=0.05,
    )
    proc = SchedulerProcess(
        get_config(),
        settings,
        overrides=ServiceOverrides(chat=models, research_enabled=False, gitlab=None, lease_keeper_interval=0.2, ops=ssh),
        scheduler_settings=SchedulerSettings(poll_seconds=0.05, retry_backoff_seconds=(0,)),
    )
    jid = await _repo_less_job(world)
    task = asyncio.create_task(proc.run())
    try:
        async with asyncio.timeout(120):
            while True:
                async with world.sessionmaker() as s:
                    job = (await s.execute(select(Job).where(Job.id == jid))).scalar_one()
                if job.status in TERMINAL:
                    break
                await asyncio.sleep(0.1)
    finally:
        proc.stop()
        assert await asyncio.wait_for(task, timeout=60) == 0
    async with world.sessionmaker() as s:
        states = [
            (r.step_key, r.kind, r.status, r.error_code, (r.error_message or "")[:300])
            for r in (await s.execute(select(Step).where(Step.job_id == jid))).scalars()
        ]
    assert job.status == "succeeded", (job.error_code, job.error_message, states)
    assert [c.capability for c in ssh.calls] == ["ssh"] and ssh.calls[0].tool == ToolName.ssh_service_status

    async with world.sessionmaker() as s:
        steps = {r.step_key: r for r in (await s.execute(select(Step).where(Step.job_id == jid))).scalars()}
        scopes = {
            r.step_id: r
            for r in (await s.execute(select(ScopeContractRow).where(ScopeContractRow.job_id == jid, ScopeContractRow.status == "active")))
            .scalars()
            .all()
        }
    assert steps["S001"].status == "completed" and steps["S002"].status == "completed"
    async with world.sessionmaker() as s:
        arts = (await s.execute(select(Artifact).where(Artifact.step_id == steps["S001"].id))).scalars().all()
    assert [(a.kind, a.name) for a in arts] == [("ops_output", "ssh_service_status-t1.txt")], "ops output is the ssh step's evidence"
    assert MARK in Path(arts[0].path).read_text()
    ssh_scope = scopes[steps["S001"].id].contract
    assert ssh_scope["target_paths"] == [] and ssh_scope["allowed_new_paths"] == [], "ssh steps get the deny-all-write scope"
    assert "ops/report.md" in scopes[steps["S002"].id].contract["allowed_new_paths"]

    bare = Path(settings.data_dir) / "scratch" / f"{jid}.git"
    branches = _git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    job_branch = next(b for b in branches.splitlines() if b != "main")
    assert "nginx.service is active" in _git(bare, "show", f"{job_branch}:ops/report.md")
    assert "MR:" not in (job.result_summary or ""), "scratch results never get a merge request"
