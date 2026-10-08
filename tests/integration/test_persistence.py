import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, select

from hermclaw.contracts.common import JobStatus
from hermclaw.persistence.base import Base
from hermclaw.persistence.models import ALL_TABLES, Job, Step, StepDependency

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {
    "jobs",
    "job_inputs",
    "steps",
    "step_dependencies",
    "step_attempts",
    "events",
    "workers",
    "worker_capabilities",
    "worker_health",
    "model_profiles",
    "model_invocations",
    "resource_leases",
    "repositories",
    "workspaces",
    "artifacts",
    "plans",
    "plan_versions",
    "scope_contracts",
    "tool_calls",
    "command_runs",
    "test_runs",
    "verification_runs",
    "verification_checks",
    "review_runs",
    "review_findings",
    "research_runs",
    "research_sources",
    "research_claims",
    "git_operations",
    "deployments",
    "wake_events",
    "bug_records",
    "memory_entries",
}


def _sync_url(url: str) -> str:
    return url


def test_all_required_tables_exist(db_url):
    assert set(ALL_TABLES) >= REQUIRED
    with psycopg.connect(db_url.replace("+psycopg", "")) as conn:
        names = {r[0] for r in conn.execute("select tablename from pg_tables where schemaname='public'")}
    assert names >= REQUIRED


def test_migration_matches_models(db_url):
    eng = create_engine(db_url)
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    eng.dispose()
    assert diff == [], diff


def test_migration_downgrade_and_upgrade_roundtrip(db_url):
    # separate scratch DB so the session DB stays intact
    admin = os.environ.get("HERMCLAW_TEST_ADMIN_URL", "postgresql://postgres@127.0.0.1:5432/postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS hermclaw_migtest")
        conn.execute("CREATE DATABASE hermclaw_migtest")
    url = admin.rsplit("/", 1)[0].replace("postgresql://", "postgresql+psycopg://") + "/hermclaw_migtest"
    old = os.environ["HERMCLAW_DATABASE_URL"]
    os.environ["HERMCLAW_DATABASE_URL"] = url
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        with psycopg.connect(url.replace("+psycopg", "")) as conn:
            left = {r[0] for r in conn.execute("select tablename from pg_tables where schemaname='public'")}
        assert left <= {"alembic_version"}
        command.upgrade(cfg, "head")
    finally:
        os.environ["HERMCLAW_DATABASE_URL"] = old
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("DROP DATABASE IF EXISTS hermclaw_migtest")


async def test_restart_persistence(db_url):
    """Data written through one engine survives engine disposal (simulated process restart)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from hermclaw.persistence.db import make_engine

    e1 = make_engine(db_url)
    sm1 = async_sessionmaker(e1, expire_on_commit=False)
    async with sm1() as s:
        job = Job(title="restart", prompt="p", status=JobStatus.queued.value)
        s.add(job)
        await s.flush()
        a = Step(job_id=job.id, step_key="S001", title="a", kind="implement", capability="coding", goal="g")
        b = Step(job_id=job.id, step_key="S002", title="b", kind="test", capability="testing", goal="g")
        s.add_all([a, b])
        await s.flush()
        s.add(StepDependency(step_id=b.id, depends_on_step_id=a.id))
        await s.commit()
        job_id = job.id
    await e1.dispose()
    e2 = make_engine(db_url)
    sm2 = async_sessionmaker(e2, expire_on_commit=False)
    async with sm2() as s:
        loaded = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
        n = (await s.execute(select(func.count()).select_from(Step).where(Step.job_id == job_id))).scalar_one()
        deps = (await s.execute(select(func.count()).select_from(StepDependency))).scalar_one()
    await e2.dispose()
    assert loaded.title == "restart" and n == 2 and deps >= 1


async def test_constraints_enforced(sessionmaker):
    from sqlalchemy.exc import IntegrityError

    async with sessionmaker() as s:
        s.add(Job(title="bad", prompt="p", priority=500))
        with pytest.raises(IntegrityError):
            await s.flush()
        await s.rollback()
    async with sessionmaker() as s:
        job = Job(title="dup", prompt="p")
        s.add(job)
        await s.flush()
        s.add_all(
            [
                Step(job_id=job.id, step_key="S001", title="a", kind="implement", capability="coding", goal="g"),
                Step(job_id=job.id, step_key="S001", title="b", kind="implement", capability="coding", goal="g"),
            ]
        )
        with pytest.raises(IntegrityError):
            await s.flush()
        await s.rollback()
