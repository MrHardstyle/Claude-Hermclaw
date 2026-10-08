"""Shared fixtures: a throw-away PostgreSQL database per test session with migrations applied."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("HERMCLAW_TEST_ADMIN_URL", "postgresql://postgres@127.0.0.1:5432/postgres")


def _db_available() -> bool:
    try:
        with psycopg.connect(ADMIN_URL, connect_timeout=3):
            return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    if not DB_AVAILABLE:
        pytest.skip("PostgreSQL not available (set HERMCLAW_TEST_ADMIN_URL)")
    name = f"hermclaw_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    base = ADMIN_URL.rsplit("/", 1)[0]
    url = f"{base.replace('postgresql://', 'postgresql+psycopg://', 1)}/{name}"
    os.environ["HERMCLAW_DATABASE_URL"] = url
    os.environ.setdefault("HERMCLAW_ENV", "test")
    from hermclaw.core.settings import reset_settings_cache

    reset_settings_cache()
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    yield url
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(scope="session")
async def engine(db_url: str) -> AsyncIterator[object]:
    from hermclaw.persistence.db import dispose_engine, init_engine

    eng = init_engine(db_url)
    yield eng
    await dispose_engine()


@pytest.fixture
async def sessionmaker(engine: object) -> object:
    from hermclaw.persistence.db import get_sessionmaker

    return get_sessionmaker()


@pytest.fixture
async def session(sessionmaker: object) -> AsyncIterator[object]:
    async with sessionmaker() as s:  # type: ignore[operator]
        yield s
        await s.rollback()


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A small git repository with one commit on ``main``."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True, env=env)
    return repo
