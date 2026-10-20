"""R-022: per-job sandbox toolchain image – selector (PostgreSQL) and a real podman run with the python image.

The podman part needs the locally built ``localhost/hermclaw-sandbox-python:1``
(``scripts/ops/hermclaw-build-sandbox-images --only python``); it is skipped with a clear reason otherwise. It proves that a
project's tests run with pytest inside the read-only, network-less sandbox purely through the image selection
(``ExecutionRequest.image`` stays ``None`` as the coder/verifier send it)."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.worker import CommandResult
from hermclaw.core.config import SandboxPolicy
from hermclaw.core.interfaces import ExecutionRequest, WorkspaceHandle
from hermclaw.persistence.models import Job, Repository, Workspace
from hermclaw.runtime.images import ImageDefaultingExecutor, SandboxImageSelector
from hermclaw.tools.executors import LocalSandboxExecutor

pytestmark = pytest.mark.integration

PYTHON_IMAGE = "localhost/hermclaw-sandbox-python:1"
NODE_IMAGE = "localhost/hermclaw-sandbox-node:1"
DEFAULT_IMAGE = "docker.io/library/alpine:3.20"


def _image_missing(image: str) -> str | None:
    if shutil.which("podman") is None:
        return "podman not installed"
    rc = subprocess.run(["podman", "image", "exists", image], capture_output=True, timeout=60, check=False).returncode
    return None if rc == 0 else f"{image} not built (scripts/ops/hermclaw-build-sandbox-images --only python)"


class FakeRepo:
    def __init__(self, languages: list[str], *, fail: bool = False) -> None:
        self.languages, self.fail, self.calls = languages, fail, 0

    async def inventory_summary(self, handle: WorkspaceHandle) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("index not built")
        return {"primary_languages": self.languages}


class RecordingExecutor:
    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []
        self.dropped = False

    async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
        self.requests.append(req)
        return CommandResult(request_id="r", exit_code=0, stdout="", stderr="", duration_ms=1)

    async def drop(self, workspace: WorkspaceHandle) -> None:
        self.dropped = True


def policy(**images: str) -> SandboxPolicy:
    return SandboxPolicy(engine="podman", image=DEFAULT_IMAGE, images=images, cpus=1.0, memory="512m", pids_limit=128)


async def _workspace(sm: async_sessionmaker[AsyncSession], path: Path, metadata: dict[str, Any] | None = None) -> WorkspaceHandle:
    async with sm() as s, s.begin():
        repo = Repository(name=f"img-{uuid.uuid4().hex[:8]}", url="file:///dev/null", metadata_=metadata or {})
        s.add(repo)
        await s.flush()
        job = Job(title="image", prompt="image selection", repository_id=repo.id)
        s.add(job)
        await s.flush()
        ws = Workspace(job_id=job.id, repository_id=repo.id, path=str(path), branch="hermclaw/x", base_branch="main", base_sha="0" * 40)
        s.add(ws)
        await s.flush()
        return WorkspaceHandle(
            id=ws.id, job_id=job.id, path=path, branch="hermclaw/x", base_branch="main", base_sha="0" * 40, repository_key=repo.name
        )


# ------------------------------------------------------------------------------------------------ selector


async def test_repository_metadata_image_wins(sessionmaker: Any, tmp_path: Path) -> None:
    ws = await _workspace(sessionmaker, tmp_path, {"sandbox_image": "node"})
    repo = FakeRepo(["python"])
    sel = SandboxImageSelector(sessionmaker, policy(python=PYTHON_IMAGE, node=NODE_IMAGE), repo)
    assert await sel.image_for(ws) == "node"
    assert repo.calls == 0  # explicit project image: no inventory lookup


async def test_language_alias_only_when_configured(sessionmaker: Any, tmp_path: Path) -> None:
    ws = await _workspace(sessionmaker, tmp_path)
    assert await SandboxImageSelector(sessionmaker, policy(python=PYTHON_IMAGE), FakeRepo(["Python"])).image_for(ws) == "python"
    assert await SandboxImageSelector(sessionmaker, policy(node=NODE_IMAGE), FakeRepo(["python", "typescript"])).image_for(ws) == "node"
    assert await SandboxImageSelector(sessionmaker, policy(), FakeRepo(["python"])).image_for(ws) is None
    assert await SandboxImageSelector(sessionmaker, policy(python=PYTHON_IMAGE), FakeRepo(["go"])).image_for(ws) is None
    assert await SandboxImageSelector(sessionmaker, policy(python=PYTHON_IMAGE), None).image_for(ws) is None
    # a PowerShell repository runs in the PowerShell toolchain (K3 could not run its script at all)
    ps = policy(python=PYTHON_IMAGE, powershell="localhost/hermclaw-sandbox-powershell:1")
    assert await SandboxImageSelector(sessionmaker, ps, FakeRepo(["powershell"])).image_for(ws) == "powershell"


async def test_disallowed_repository_image_falls_back(sessionmaker: Any, tmp_path: Path) -> None:
    ws = await _workspace(sessionmaker, tmp_path, {"sandbox_image": "docker.io/evil/miner:latest"})
    sel = SandboxImageSelector(sessionmaker, policy(python=PYTHON_IMAGE), FakeRepo(["python"]))
    assert await sel.image_for(ws) == "python"  # not allowed → language alias
    ws2 = await _workspace(sessionmaker, tmp_path, {"sandbox_image": PYTHON_IMAGE})
    assert await sel.image_for(ws2) == PYTHON_IMAGE  # a full reference of an allowed image is fine


async def test_inventory_failure_uses_default_and_result_is_cached(sessionmaker: Any, tmp_path: Path) -> None:
    ws = await _workspace(sessionmaker, tmp_path)
    failing = FakeRepo([], fail=True)
    assert await SandboxImageSelector(sessionmaker, policy(python=PYTHON_IMAGE), failing).image_for(ws) is None
    repo = FakeRepo(["python"])
    sel = SandboxImageSelector(sessionmaker, policy(python=PYTHON_IMAGE), repo)
    assert await sel.image_for(ws) == "python"
    assert await sel.image_for(ws) == "python"
    assert repo.calls == 1


async def test_empty_repository_gets_the_toolchain_once_it_has_code(sessionmaker: Any, tmp_path: Path) -> None:
    """BUG-155 (live job eadf1b6f): an empty repository has no primary language, so the first selection is the
    generic default image. That None was cached for the workspace, and the steps after the one that created the
    first Python file still ran in the generic image without pytest. Only a real selection is cached."""
    ws = await _workspace(sessionmaker, tmp_path)
    repo = FakeRepo([])
    sel = SandboxImageSelector(sessionmaker, policy(python=PYTHON_IMAGE), repo)
    assert await sel.image_for(ws) is None
    repo.languages = ["python"]  # S002 created app/main.py
    assert await sel.image_for(ws) == "python"
    assert await sel.image_for(ws) == "python"
    assert repo.calls == 2


async def test_workspace_without_repository_row(sessionmaker: Any, tmp_path: Path) -> None:
    ghost = WorkspaceHandle(
        id=uuid.uuid4(), job_id=uuid.uuid4(), path=tmp_path, branch="b", base_branch="main", base_sha="0" * 40, repository_key="x"
    )
    assert await SandboxImageSelector(sessionmaker, policy(python=PYTHON_IMAGE), FakeRepo(["python"])).image_for(ghost) == "python"


# ------------------------------------------------------------------------------------------------ executor wrapper


async def test_defaulting_executor_sets_image_only_when_unset(tmp_path: Path) -> None:
    inner = RecordingExecutor()
    ws = WorkspaceHandle(
        id=uuid.uuid4(), job_id=uuid.uuid4(), path=tmp_path, branch="b", base_branch="main", base_sha="0" * 40, repository_key="x"
    )
    ex = ImageDefaultingExecutor(inner, "python")
    await ex.run(ws, ExecutionRequest(command="pytest -q"))
    await ex.run(ws, ExecutionRequest(command="node -v", image="node"))
    await ImageDefaultingExecutor(inner, None).run(ws, ExecutionRequest(command="true"))
    assert [r.image for r in inner.requests] == ["python", "node", None]
    await ex.drop(ws)  # other executor methods are delegated
    assert inner.dropped


async def test_manifest_identifies_a_new_project_and_the_executor_asks_again(sessionmaker: Any, tmp_path: Path) -> None:
    """D-014/BUG-155: a step in an empty repository writes requirements.txt and installs dependencies; its next
    commands (same executor) must run in the Python toolchain, whose interpreter matches the installed wheels."""
    ws = await _workspace(sessionmaker, tmp_path)
    sel = SandboxImageSelector(sessionmaker, policy(python=PYTHON_IMAGE, node=NODE_IMAGE), FakeRepo([]))
    inner = RecordingExecutor()
    ex = ImageDefaultingExecutor(inner, await sel.image_for(ws), sel.image_for)
    await ex.run(ws, ExecutionRequest(command="ls"))
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    await ex.run(ws, ExecutionRequest(command="pytest -q"))
    await ex.run(ws, ExecutionRequest(command="pytest -q"))
    assert [r.image for r in inner.requests] == [None, "python", "python"]
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")  # the first selection stays
    assert await sel.image_for(ws) == "python"


# ------------------------------------------------------------------------------------------------ real podman


@pytest.mark.skipif(_image_missing(PYTHON_IMAGE) is not None, reason=_image_missing(PYTHON_IMAGE) or "")
async def test_pytest_runs_in_selected_python_image(sessionmaker: Any, tmp_path: Path) -> None:
    from worker.execution.sandbox import PodmanSandbox

    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (proj / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (proj / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["."]\n')
    ws = await _workspace(sessionmaker, proj)
    pol = policy(python=PYTHON_IMAGE)
    sel = SandboxImageSelector(sessionmaker, pol, FakeRepo(["python"]))
    runner = PodmanSandbox(pol, pull="never", managed_label="hermclaw.r022test=true")
    ex = ImageDefaultingExecutor(LocalSandboxExecutor(pol, runner=runner), await sel.image_for(ws))

    r = await ex.run(ws, ExecutionRequest(command="python -m pytest -q -p no:cacheprovider", timeout_seconds=120, purpose="test"))
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "1 passed" in r.stdout
    lint = await ex.run(ws, ExecutionRequest(command="ruff --version && mypy --version", timeout_seconds=60))
    assert lint.exit_code == 0, lint.stderr
    # still network-less and read-only outside /workspace and /tmp
    iso = await ex.run(
        ws,
        ExecutionRequest(
            command="touch /usr/x 2>/dev/null && echo RW || echo RO; python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 2)\" 2>/dev/null && echo NET || echo NONET"
        ),
    )
    assert "RO" in iso.stdout and "NONET" in iso.stdout


@pytest.mark.skipif(_image_missing(DEFAULT_IMAGE) is not None, reason=_image_missing(DEFAULT_IMAGE) or "")
async def test_missing_images_reports_unbuilt_toolchain_images_without_pulling() -> None:
    from worker.execution.sandbox import PodmanSandbox

    sbx = PodmanSandbox(policy(node="localhost/hermclaw-sandbox-never-built:1"), pull="missing", managed_label="hermclaw.r022test=true")
    assert await sbx.missing_images() == ["localhost/hermclaw-sandbox-never-built:1"]  # alpine default is present
    assert subprocess.run(["podman", "image", "exists", "localhost/hermclaw-sandbox-never-built:1"], check=False).returncode == 1
