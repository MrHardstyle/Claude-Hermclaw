"""VerifierAdapter: the real deterministic verifier behind the implement-handler port and the regression rerun."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update

from hermclaw.contracts.scope import ScopeContract
from hermclaw.contracts.step import StepContract
from hermclaw.core.config import get_config
from hermclaw.core.interfaces import WorkspaceHandle
from hermclaw.persistence.models import ScopeContractRow, Step
from hermclaw.runtime.adapters import VerifierAdapter
from hermclaw.tools.executors import SubprocessExecutor
from tests.integration.test_gitops_support import World, make_world, write
from tests.integration.test_tools_support import dev_settings

pytestmark = pytest.mark.asyncio(loop_scope="session")
SCOPE = ScopeContract(target_paths=["src/module.py"])
ACCEPT = [{"type": "presence", "path_glob": "src/module.py", "pattern": "VALUE = 2"}]


@pytest.fixture
async def world(sessionmaker: Any, tmp_path: Path) -> World:
    return await make_world(sessionmaker, tmp_path)


def _adapter(world: World) -> VerifierAdapter:
    cfg = get_config()

    async def executor_for(ws: WorkspaceHandle, step: StepContract | None) -> SubprocessExecutor:
        return SubprocessExecutor(cfg.policies.sandbox, settings=dev_settings())

    return VerifierAdapter(world.sessionmaker, cfg, world.engine.reader(), executor_for)


async def _step(world: World, job_id: Any) -> tuple[Any, StepContract]:
    async with world.sessionmaker() as s:
        st = Step(
            job_id=job_id,
            step_key="S001",
            title="set value",
            kind="implement",
            capability="coding",
            goal="set VALUE to 2",
            acceptance=ACCEPT,
        )
        s.add(st)
        await s.flush()
        s.add(ScopeContractRow(job_id=job_id, step_id=st.id, version=1, status="active", contract=SCOPE.model_dump(mode="json")))
        await s.commit()
    contract = StepContract.model_validate(
        {
            "id": st.id,
            "job_id": job_id,
            "step_key": "S001",
            "title": "set value",
            "kind": "implement",
            "capability": "coding",
            "goal": "g",
            "status": "running",
            "acceptance": ACCEPT,
            "scope": SCOPE,
        }
    )
    return st.id, contract


async def test_verify_pass_produces_committable_run_and_fail_reports_evidence(world: World) -> None:
    adapter = _adapter(world)
    job_id = await world.job("verify adapter")
    step_id, contract = await _step(world, job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    handle = await world.engine.handle(ws)
    # not yet changed -> presence evidence fails with a concrete message
    bad = await adapter.verify(contract, handle, job_id=job_id, step_id=step_id, attempt_id=None)  # type: ignore[arg-type]
    assert not bad.report.passed and any(c.check_type == "presence" for c in bad.report.failures)
    write(Path(ws.path), "src/module.py", "VALUE = 2\n")
    good = await adapter.verify(contract, handle, job_id=job_id, step_id=step_id, attempt_id=None)  # type: ignore[arg-type]
    assert good.report.passed, good.report.summary
    await world.engine.stage_allowed(ws, SCOPE, step_id=step_id)
    commit = await world.engine.commit_verified(ws, "S001: set value", good.run_id, step_id=step_id, scope=SCOPE)
    assert commit.files == ["src/module.py"]


async def test_regression_rerun_checks_completed_code_steps(world: World) -> None:
    adapter = _adapter(world)
    job_id = await world.job("regression")
    step_id, _contract = await _step(world, job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    handle = await world.engine.handle(ws)
    async with world.sessionmaker() as s:
        await s.execute(update(Step).where(Step.id == step_id).values(status="completed"))
        await s.commit()
    ok, info = await adapter.rerun(job_id, handle)
    assert not ok and info["checked_steps"] == 1 and info["failed"][0]["step_key"] == "S001"
    write(Path(ws.path), "src/module.py", "VALUE = 2\n")
    ok, info = await adapter.rerun(job_id, handle)
    assert ok and info == {"checked_steps": 1, "failed": []}, info
