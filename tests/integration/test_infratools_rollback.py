"""P28.6 – rollback artifacts (artifact store + PostgreSQL) and guarded config deployments with automatic restore."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import HostConfig, HostsConfig
from hermclaw.infratools import InfraTools
from hermclaw.infratools.errors import (
    INFRA_DEPLOY_FAILED,
    INFRA_INVALID_ARGUMENT,
    INFRA_PATH_INVALID,
    INFRA_ROLLBACK_CORRUPT,
    INFRA_ROLLBACK_INVALID,
    INFRA_ROLLBACK_UNKNOWN,
    INFRA_STORE_UNAVAILABLE,
    InfraToolError,
)
from hermclaw.infratools.process import HostExecutor, InfraContext, LocalRunner
from hermclaw.infratools.rollback import ConfigDeployer, RollbackStore, TargetFiles, restore_command, validate_target_path
from hermclaw.persistence.models import Artifact, CommandRun, Deployment, Event, Job

pytestmark = pytest.mark.integration
HOSTS = HostsConfig(hosts=[HostConfig(id="web-223", address="127.0.0.1", role="webui")])


@dataclass
class Env:
    maker: async_sessionmaker[AsyncSession]
    root: Path  # allowed deployment root (stands in for /etc/nginx)
    artifacts: Path
    store: RollbackStore
    deployer: ConfigDeployer
    runner: LocalRunner
    job_id: uuid.UUID

    @property
    def ctx(self) -> InfraContext:
        return InfraContext(job_id=self.job_id, actor="test")

    async def rows(self, model: Any, **where: Any) -> list[Any]:
        async with self.maker() as s:
            stmt = select(model)
            for key, value in where.items():
                stmt = stmt.where(getattr(model, key) == value)
            return list((await s.execute(stmt)).scalars())


@pytest.fixture
async def env(sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path) -> Env:
    root = tmp_path / "etc" / "nginx"
    root.mkdir(parents=True)
    artifacts = tmp_path / "artifacts"
    runner = LocalRunner()
    executor = HostExecutor(remote=runner, hosts=HOSTS)
    store = RollbackStore(sessionmaker, artifacts, TargetFiles(executor, timeout=30), allowed_roots=[str(tmp_path / "etc")])
    async with sessionmaker() as s:
        job = Job(title="deploy nginx site", prompt="p")
        s.add(job)
        await s.commit()
        job_id = job.id
    return Env(sessionmaker, root, artifacts, store, ConfigDeployer(store), runner, job_id)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------------------------- artifacts
async def test_create_stores_content_checksum_manifest_and_row(env: Env) -> None:
    target = env.root / "site.conf"
    target.write_bytes(b"server { listen 80; }\n")
    target.chmod(0o640)
    art = await env.store.create(str(target), reload_argv=["nginx", "-s", "reload"], ctx=env.ctx)
    assert art.existed and art.sha256 == sha(b"server { listen 80; }\n") and art.mode == 0o640
    stored = Path(art.file)
    assert stored.read_bytes() == target.read_bytes()
    assert stored.parent.parent.parent == env.artifacts / str(env.job_id)  # <artifacts>/<job>/rollback/<id>/<file>
    assert oct(stored.stat().st_mode & 0o777) == "0o600" and oct(stored.parent.stat().st_mode & 0o777) == "0o700"
    manifest = json.loads((stored.parent / "manifest.json").read_text())
    assert (
        manifest["sha256"] == art.sha256 and manifest["target_path"] == str(target) and manifest["restore_command"] == art.restore_command
    )
    assert art.restore_command.startswith("install -m 0640 -- ") and art.restore_command.endswith("&& nginx -s reload")
    (row,) = await env.rows(Artifact, id=art.id)
    assert row.kind == "rollback" and row.sha256 == art.sha256 and row.job_id == env.job_id and row.path == art.file
    assert row.metadata_["infratools"]["existed"] is True


async def test_restore_verifies_then_restores_and_records(env: Env) -> None:
    target = env.root / "site.conf"
    target.write_text("old\n")
    art = await env.store.create(str(target), ctx=env.ctx)
    target.write_text("broken new config\n")
    target.chmod(0o600)
    report = await env.store.restore(str(art.id), ctx=env.ctx)
    assert report.ok and report.action == "restored" and target.read_text() == "old\n"
    (dep,) = await env.rows(Deployment, id=report.deployment_id)
    assert dep.kind == "restore:config" and dep.status == "succeeded" and dep.rollback_artifact_id == art.id
    events = await env.rows(Event, job_id=env.job_id)
    assert any(e.event_type == "deployment.finished" and e.payload.get("action") == "rollback_restore" for e in events)


async def test_absent_target_restore_removes_file(env: Env) -> None:
    target = env.root / "new-site.conf"
    art = await env.store.create(str(target), ctx=env.ctx)
    assert not art.existed and Path(art.file).name.endswith(".absent") and art.restore_command.startswith("rm -f -- ")
    target.write_text("created by a deployment\n")
    report = await env.store.restore(art.id, ctx=env.ctx)
    assert report.action == "removed" and report.ok and not target.exists()


async def test_corrupt_artifact_is_refused_and_target_untouched(env: Env) -> None:
    target = env.root / "site.conf"
    target.write_text("v1\n")
    art = await env.store.create(str(target), ctx=env.ctx)
    target.write_text("v2\n")
    Path(art.file).write_text("tampered\n")
    with pytest.raises(InfraToolError) as err:
        await env.store.restore(art.id, ctx=env.ctx)
    assert err.value.code == INFRA_ROLLBACK_CORRUPT and target.read_text() == "v2\n"
    # manifest disagreeing with the row is corrupt as well
    art2 = await env.store.create(str(target), ctx=env.ctx)
    manifest = Path(art2.file).parent / "manifest.json"
    data = json.loads(manifest.read_text())
    data["target_path"] = str(env.root / "other.conf")
    manifest.write_text(json.dumps(data))
    with pytest.raises(InfraToolError) as err:
        await env.store.restore(art2.id, ctx=env.ctx)
    assert err.value.code == INFRA_ROLLBACK_CORRUPT
    Path(art2.file).unlink()
    with pytest.raises(InfraToolError) as err:
        await env.store.restore(art2.id, ctx=env.ctx)
    assert err.value.code == INFRA_ROLLBACK_CORRUPT


async def test_tampered_rows_cannot_redirect_restores(env: Env, tmp_path: Path) -> None:
    target = env.root / "site.conf"
    target.write_text("v1\n")
    art = await env.store.create(str(target), ctx=env.ctx)
    outside = tmp_path / "outside.bin"
    outside.write_text("v1\n")
    async with env.maker() as s, s.begin():
        await s.execute(update(Artifact).where(Artifact.id == art.id).values(path=str(outside)))
    with pytest.raises(InfraToolError) as err:
        await env.store.restore(art.id, ctx=env.ctx)
    assert err.value.code == INFRA_ROLLBACK_INVALID
    art2 = await env.store.create(str(target), ctx=env.ctx)
    meta = {"infratools": {**(await env.rows(Artifact, id=art2.id))[0].metadata_["infratools"], "target_path": "/etc/passwd"}}
    async with env.maker() as s, s.begin():
        await s.execute(update(Artifact).where(Artifact.id == art2.id).values(metadata_=meta))
    with pytest.raises(InfraToolError) as err:
        await env.store.restore(art2.id, ctx=env.ctx)
    assert err.value.code == INFRA_PATH_INVALID
    async with env.maker() as s, s.begin():
        s.add(Artifact(kind="report", name="r", path=str(outside), sha256="x", metadata_={}))
        await s.flush()
        other = (await s.execute(select(Artifact.id).where(Artifact.kind == "report", Artifact.path == str(outside)))).scalar_one()
    with pytest.raises(InfraToolError) as err:
        await env.store.get(other)
    assert err.value.code == INFRA_ROLLBACK_INVALID
    with pytest.raises(InfraToolError) as err:
        await env.store.get(uuid.uuid4())
    assert err.value.code == INFRA_ROLLBACK_UNKNOWN
    with pytest.raises(InfraToolError) as err:
        await env.store.get("not-a-uuid")
    assert err.value.code == INFRA_ROLLBACK_INVALID


@pytest.mark.parametrize(
    "path", ["relative/site.conf", "/etc/passwd", "{root}/../escape.conf", "{root}/a//b", "{root}/x\nnext", "{root}/dir/"]
)
def test_target_path_validation(env: Env, path: str) -> None:
    with pytest.raises(InfraToolError) as err:
        validate_target_path(path.format(root=env.root), env.store.allowed_roots)
    assert err.value.code == INFRA_PATH_INVALID


async def test_symlinks_are_refused(env: Env, tmp_path: Path) -> None:
    real = env.root / "real.conf"
    real.write_text("x\n")
    os.symlink(real, env.root / "link.conf")
    with pytest.raises(InfraToolError, match="symbolic link"):
        await env.store.create(str(env.root / "link.conf"), ctx=env.ctx)
    escape = tmp_path / "elsewhere"
    escape.mkdir()
    os.symlink(escape, env.root / "sneaky")
    with pytest.raises(InfraToolError, match="resolves outside"):
        await env.store.create(str(env.root / "sneaky" / "site.conf"), ctx=env.ctx)
    with pytest.raises(InfraToolError, match="parent directory"):
        await env.store.create(str(env.root / "missing-dir" / "site.conf"), ctx=env.ctx)


async def test_remote_snapshot_survives_transport_redaction_and_binary(env: Env) -> None:
    """The SSH runner redacts output; secret-looking or non-UTF-8 content falls back to a checksummed base64 transfer."""
    target = env.root / "litellm.env"
    secret = b"DATABASE_URL=postgresql://litellm:supersecretpass@127.0.0.1:5432/litellm\napi_key = sk-abcdefghijklmnopqrstuvwx\n"
    target.write_bytes(secret)
    target.chmod(0o600)
    art = await env.store.create(str(target), host="web-223", ctx=env.ctx)
    assert Path(art.file).read_bytes() == secret and art.host == "web-223" and art.mode == 0o600
    assert any(c.startswith("base64 -w 0 -- ") for c in env.runner.commands)
    assert "# on host web-223" in art.restore_command
    binary = env.root / "blob.bin"
    binary.write_bytes(bytes(range(256)))
    art2 = await env.store.create(str(binary), host="web-223", ctx=env.ctx)
    assert Path(art2.file).read_bytes() == bytes(range(256))
    # the raw file content is never written to the command audit
    target.write_text("changed\n")
    report = await env.store.restore(art.id, ctx=env.ctx)
    assert report.ok and target.read_bytes() == secret and oct(target.stat().st_mode & 0o777) == "0o600"
    assert any(c.startswith("install -m 0600 /dev/stdin ") for c in env.runner.commands)


async def test_store_failure_cleans_up_files(env: Env) -> None:
    target = env.root / "site.conf"
    target.write_text("x\n")
    with pytest.raises(InfraToolError) as err:  # job id that does not exist → FK violation
        await env.store.create(str(target), ctx=InfraContext(job_id=uuid.uuid4()))
    assert err.value.code == INFRA_STORE_UNAVAILABLE
    assert not any(p.is_file() for p in env.artifacts.rglob("*")) if env.artifacts.exists() else True


# ---------------------------------------------------------------------------------------------- deployments
async def test_deploy_success_references_rollback_and_emits_events(env: Env) -> None:
    target = env.root / "site.conf"
    target.write_text("server { listen 80; }\n")
    target.chmod(0o640)
    report = await env.deployer.deploy(
        str(target), "server { listen 8080; }\n", kind="nginx", check_argv=["true"], reload_argv=["true"], ctx=env.ctx
    )
    assert report.ok and report.status == "succeeded", report.to_dict()
    assert target.read_text() == "server { listen 8080; }\n" and oct(target.stat().st_mode & 0o777) == "0o640"
    assert report.steps == ["claimed", "rollback_artifact", "written", "verified", "checked", "reloaded"]
    (dep,) = await env.rows(Deployment, id=report.deployment_id)
    assert dep.status == "succeeded" and dep.kind == "nginx" and dep.finished_at is not None
    assert report.rollback is not None and dep.rollback_artifact_id == report.rollback.id
    assert dep.manifest["previous_sha256"] == sha(b"server { listen 80; }\n") and dep.manifest["sha256"] == sha(
        b"server { listen 8080; }\n"
    )
    (art,) = await env.rows(Artifact, id=dep.rollback_artifact_id)
    assert art.kind == "rollback" and Path(art.path).read_text() == "server { listen 80; }\n"
    types = [e.event_type for e in await env.rows(Event, job_id=env.job_id)]
    assert "deployment.started" in types and "deployment.finished" in types
    # and the operator can roll the successful deployment back later
    restored = await env.store.restore(dep.rollback_artifact_id, ctx=env.ctx)
    assert restored.ok and target.read_text() == "server { listen 80; }\n"


@pytest.mark.parametrize(
    ("check", "reload", "step"),
    [(["false"], None, "check"), (["true"], ["sh", "-c", "grep -q good {target}"], "reload")],
)
async def test_failed_check_or_reload_rolls_back(env: Env, check: list[str], reload: list[str] | None, step: str) -> None:
    target = env.root / "site.conf"
    target.write_text("good\n")
    reload = [a.format(target=target) for a in reload] if reload else None  # reload fails for the new content only
    report = await env.deployer.deploy(str(target), "bad\n", check_argv=check, reload_argv=reload, ctx=env.ctx)
    assert report.status == "rolled_back" and report.code == INFRA_DEPLOY_FAILED, report.to_dict()
    assert target.read_text() == "good\n"
    assert report.restore is not None and report.restore.verified
    assert step in (report.error or "")
    if step == "reload":  # the old config is reloaded again after the restore
        assert report.restore.reload is not None and report.restore.reload.ok
    (dep,) = await env.rows(Deployment, id=report.deployment_id)
    assert dep.status == "rolled_back" and dep.rollback_artifact_id == report.rollback.id  # type: ignore[union-attr]


async def test_reload_failing_for_the_old_config_too_is_rollback_failed(env: Env) -> None:
    target = env.root / "site.conf"
    target.write_text("good\n")
    report = await env.deployer.deploy(str(target), "bad\n", reload_argv=["sh", "-c", "exit 4"], ctx=env.ctx)
    assert report.status == "rollback_failed" and target.read_text() == "good\n"  # file restored, service state unknown
    assert "restore could not be verified" in (report.error or "")


async def test_new_file_failed_check_removes_it_again(env: Env) -> None:
    target = env.root / "fresh.conf"
    report = await env.deployer.deploy(str(target), "x\n", check_argv=["false"], ctx=env.ctx)
    assert report.status == "rolled_back" and not target.exists()


async def test_one_deployment_per_target_at_a_time(env: Env) -> None:
    target = env.root / "site.conf"
    target.write_text("v0\n")
    slow = asyncio.create_task(env.deployer.deploy(str(target), "v1\n", check_argv=["sleep", "1.5"], ctx=env.ctx))
    await asyncio.sleep(0.5)
    with pytest.raises(InfraToolError) as err:
        await env.deployer.deploy(str(target), "v2\n", ctx=env.ctx)
    assert err.value.code == INFRA_DEPLOY_FAILED and "in progress" in err.value.message
    assert (await slow).ok and target.read_text() == "v1\n"
    assert (await env.deployer.deploy(str(target), "v3\n", ctx=env.ctx)).ok  # free again afterwards


async def test_cancelled_deployment_restores_previous_state(env: Env) -> None:
    target = env.root / "site.conf"
    target.write_text("stable\n")
    task = asyncio.create_task(env.deployer.deploy(str(target), "half-done\n", check_argv=["sleep", "5"], ctx=env.ctx))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if target.read_text() == "half-done\n":
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert target.read_text() == "stable\n"
    deps = [d for d in await env.rows(Deployment, job_id=env.job_id) if d.target.endswith(str(target))]
    assert deps and deps[-1].status == "rolled_back"


async def test_remote_deploy_via_runner(env: Env) -> None:
    target = env.root / "remote.conf"
    target.write_text("remote-old\n")
    target.chmod(0o644)
    report = await env.deployer.deploy(str(target), b"remote-new\n", host="web-223", check_argv=["false"], ctx=env.ctx)
    assert report.status == "rolled_back" and target.read_text() == "remote-old\n"
    ok = await env.deployer.deploy(str(target), b"remote-new\n", host="web-223", mode=0o640, ctx=env.ctx)
    assert ok.ok and target.read_text() == "remote-new\n" and oct(target.stat().st_mode & 0o777) == "0o640"
    assert any(c.startswith("sha256sum -- ") for c in env.runner.commands)


async def test_invalid_deploy_arguments(env: Env) -> None:
    target = env.root / "site.conf"
    cases: list[dict[str, Any]] = [{"check_argv": []}, {"reload_argv": ["nginx", "-s\nreload"]}, {"mode": 0o17777}]
    for kwargs in cases:
        with pytest.raises(InfraToolError) as err:
            await env.deployer.deploy(str(target), "x", ctx=env.ctx, **kwargs)
        assert err.value.code == INFRA_INVALID_ARGUMENT
    with pytest.raises(InfraToolError) as err:
        await env.deployer.deploy("/etc/shadow", "x", ctx=env.ctx)
    assert err.value.code == INFRA_PATH_INVALID


async def test_facade_wiring_and_audit(sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    root = tmp_path / "srv"
    root.mkdir()
    tools = InfraTools.create(sessionmaker=sessionmaker, artifacts_dir=tmp_path / "art", deploy_roots=(str(root),))
    target = root / "app.conf"
    target.write_text("token = abcdefghijklmnop\n")
    report = await tools.deploy_config(str(target), "token = qrstuvwxyzabcdef\n", check_argv=["cat", str(target)])
    assert report.ok
    async with sessionmaker() as s:
        runs = list((await s.execute(select(CommandRun).where(CommandRun.command.like(f"%{target}%")))).scalars())
    assert runs and all("qrstuvwxyzabcdef" not in (r.stdout_excerpt or "") for r in runs)
    with pytest.raises(InfraToolError) as err:
        await InfraTools(HostExecutor()).rollback_restore(str(uuid.uuid4()))
    assert err.value.code == INFRA_STORE_UNAVAILABLE


def test_restore_command_rendering() -> None:
    assert restore_command("local", "/etc/x", "/a/b", existed=True, mode=0o644, reload_argv=None) == "install -m 0644 -- /a/b /etc/x"
    remote = restore_command("web-223", "/etc/x", "/a/b c", existed=True, mode=None, reload_argv=["systemctl", "reload", "nginx"])
    assert remote == "install -m 0644 /dev/stdin /etc/x < '/a/b c' && systemctl reload nginx  # on host web-223"


async def test_remote_symlinked_parent_directory_is_refused(env: Env, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, env.root / "linked")
    with pytest.raises(InfraToolError) as err:
        await env.store.create(str(env.root / "linked" / "site.conf"), host="web-223", ctx=env.ctx)
    assert err.value.code == INFRA_PATH_INVALID and "resolves to" in err.value.message
    assert any(c.startswith("readlink -f -- ") for c in env.runner.commands)


async def test_failed_rollback_artifact_blocks_the_write_and_releases_the_claim(env: Env) -> None:
    target = env.root / "site.conf"
    target.write_text("keep\n")
    bad_ctx = InfraContext(job_id=uuid.uuid4())  # artifacts.job_id FK violation → artifact cannot be stored
    report = await env.deployer.deploy(str(target), "never written\n", ctx=bad_ctx)
    assert report.status == "failed" and report.code == INFRA_STORE_UNAVAILABLE and target.read_text() == "keep\n"
    assert report.steps == ["claimed"]
    (dep,) = await env.rows(Deployment, id=report.deployment_id)
    assert dep.status == "failed" and dep.rollback_artifact_id is None
    assert (await env.deployer.deploy(str(target), "next\n", ctx=env.ctx)).ok  # the claim was released
