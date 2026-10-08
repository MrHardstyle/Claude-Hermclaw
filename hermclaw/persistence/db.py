"""Async engine/session management and transaction boundaries (P03 3.6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from hermclaw.core.settings import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def make_engine(url: str | None = None, *, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(
        url or get_settings().database_url,
        pool_size=pool_size,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def init_engine(url: str | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    _engine = make_engine(url)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One transaction: commit on success, rollback on error."""
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def db_health() -> dict[str, object]:
    async with get_engine().connect() as conn:
        version = (await conn.execute(text("select version()"))).scalar_one()
        vector = (await conn.execute(text("select extversion from pg_extension where extname='vector'"))).scalar()
        rev = None
        try:
            rev = (await conn.execute(text("select version_num from alembic_version"))).scalar()
        except Exception:
            rev = None
    vparts = tuple(int(x) for x in str(vector).split(".")[:3]) if vector else ()
    return {
        "ok": True,
        "postgres": str(version).split(",")[0],
        "pgvector": vector,
        "pgvector_cve_2026_3172_patched": bool(vparts and vparts >= (0, 8, 2)),
        "schema_revision": rev,
    }
