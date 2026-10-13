"""Failure cases of the exact verification binding (real git + PostgreSQL).

The content digest is computed with ``git hash-object``, which runs the clean filter a repository config names. A
filter driver planted in a workspace's ``.git/config`` (e.g. from the sandbox) must never run on the orchestrator:
the digest is refused for workspaces with tampered git metadata, the run carries no digest and can never be committed.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from hermclaw.contracts.acceptance import PresenceEvidence
from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import PoliciesConfig
from hermclaw.gitops.engine import worktree_content_digest
from hermclaw.gitops.errors import WorkspaceTamperedError
from hermclaw.persistence.models import VerificationRun
from hermclaw.verifier import VerificationStep, Verifier
from tests.integration.test_gitops_support import World, git, make_world, write
from tests.integration.test_verifier_support import WorkspaceShellExecutor

pytestmark = pytest.mark.integration

SCOPE = ScopeContract(target_paths=["src/**"], allowed_new_paths=["src/**"], allowed_operations=["create", "modify"])


@pytest.fixture
async def world(sessionmaker: Any, tmp_path: Path) -> World:
    return await make_world(sessionmaker, tmp_path, policies=PoliciesConfig())


def plant_filter(path: Path, marker: Path) -> None:
    with (path / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(f'[filter "evil"]\n\tclean = touch {marker}; cat\n')
    write(path, ".gitattributes", "*.py filter=evil\n")


async def test_digest_never_runs_a_planted_filter_driver(world: World, tmp_path: Path) -> None:
    job_id = await world.job()
    step_id = await world.step(job_id)
    ws = await world.engine.create_workspace(job_id, world.repo)
    path = Path(ws.path)
    write(path, "src/module.py", "VALUE = 2\n")
    marker = tmp_path / f"filter-ran-{uuid.uuid4().hex[:6]}"
    plant_filter(path, marker)

    with pytest.raises(WorkspaceTamperedError):
        await worktree_content_digest(path, ["src/module.py"], runner=world.engine.runner)
    assert not marker.exists()

    verifier = Verifier(world.sessionmaker, PoliciesConfig(), WorkspaceShellExecutor(), world.engine.reader())
    vstep = VerificationStep.build(
        key="S1", kind="implement", acceptance=[PresenceEvidence(path_glob="src/module.py", pattern="VALUE = 2")], scope=SCOPE
    )
    # engine.handle() would refuse the tampered workspace: hand the verifier the handle the runtime already holds
    out = await verifier.run(vstep, ws.to_handle(), job_id=job_id, step_id=step_id, attempt_id=None)
    assert not out.report.passed  # the reader refuses the tampered workspace as well
    async with world.sessionmaker() as s:
        row = await s.get(VerificationRun, out.run_id)
    assert row is not None and row.workspace_id == ws.id and row.content_digest is None
    assert not marker.exists(), "the verifier ran a filter driver from the workspace config"

    # the threat is real: plain git (no guard) does run the planted driver for exactly this call
    git("hash-object", "--", "src/module.py", cwd=path)
    assert marker.exists()
