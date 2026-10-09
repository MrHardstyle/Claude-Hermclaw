"""Crash recovery and failure behaviour of the resource manager (P09 9.9) against real PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core.config import LeasePolicy
from hermclaw.persistence.db import make_engine
from hermclaw.persistence.models import Event, ResourceLease, ResourceRequest
from hermclaw.resources import OwnerKind, ResourceManager

pytestmark = pytest.mark.integration

POLICY = LeasePolicy(default_ttl_seconds=30, heartbeat_seconds=1, preemption_grace_seconds=2)


def rname(prefix: str = "res") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def mgr(sm, holder: str | None = None) -> ResourceManager:
    return ResourceManager(sm, POLICY, holder or f"test-{uuid.uuid4().hex[:8]}", poll_interval=0.05)


async def test_leases_survive_crash_and_recover_releases_previous_incarnation(db_url, sessionmaker) -> None:
    holder = f"runtime@host-{uuid.uuid4().hex[:6]}"
    res, res2 = rname(), rname()

    # process 1: acquires, then "crashes" (engine disposed, nothing released)
    eng1 = make_engine(db_url, pool_size=2)
    sm1 = async_sessionmaker(eng1, expire_on_commit=False, class_=AsyncSession)
    p1 = mgr(sm1, holder)
    lease = await p1.acquire(res, owner_kind=OwnerKind.CODER, ttl_seconds=300)
    blocker = await mgr(sessionmaker).acquire(res2, owner_kind=OwnerKind.HEAVY)
    async with sm1() as s, s.begin():  # a waiting request that was in flight when the process died
        stale_req = uuid.uuid4()
        await s.execute(
            insert(ResourceRequest).values(
                id=stale_req,
                resource=res2,
                owner_kind="coder",
                holder=holder,
                priority=50,
                state="waiting",
                expires_at=func.now() + timedelta(seconds=300),
            )
        )
    await eng1.dispose()

    # process 2: same holder id, new incarnation, new engine – the lease is still there and still blocks others
    eng2 = make_engine(db_url, pool_size=2)
    sm2 = async_sessionmaker(eng2, expire_on_commit=False, class_=AsyncSession)
    try:
        p2 = mgr(sm2, holder)
        assert p2.incarnation != p1.incarnation
        survived = await p2.get_lease(lease.id)
        assert survived is not None and survived.state == "active"
        other = mgr(sessionmaker)
        assert await other.try_acquire(res, owner_kind=OwnerKind.CODER) is None
        assert [q.id for q in await other.waiting_requests(res2)] == [stale_req]

        report = await p2.recover()
        assert report.holder == holder
        assert [x.id for x in report.released] == [lease.id]
        assert report.cancelled_requests == [stale_req]
        row = await p2.get_lease(lease.id)
        assert row is not None and row.state == "released" and row.release_reason == "holder_restarted"
        assert await other.try_acquire(res, owner_kind=OwnerKind.CODER) is not None
        assert await other.waiting_requests(res2) == []
        assert (await other.get_lease(blocker.id)).state == "active"  # type: ignore[union-attr]  # other holders untouched

        async with sessionmaker() as s:
            ev = (
                await s.execute(
                    select(Event).where(Event.event_type == EventType.RESOURCE_RELEASED, Event.payload["lease_id"].astext == str(lease.id))
                )
            ).scalar_one()
        assert ev.payload["reason"] == "holder_restarted" and p1.incarnation in ev.payload["detail"]
        assert (await p2.recover()).released == []  # idempotent
    finally:
        await eng2.dispose()


async def test_recover_keeps_leases_and_requests_of_the_current_incarnation(sessionmaker) -> None:
    holder = f"runtime@host-{uuid.uuid4().hex[:6]}"
    m = mgr(sessionmaker, holder)
    res, busy = rname(), rname()
    mine = await m.acquire(res, owner_kind=OwnerKind.CODER)
    blocker = await mgr(sessionmaker).acquire(busy, owner_kind=OwnerKind.HEAVY)
    waiting = asyncio.create_task(m.acquire(busy, owner_kind=OwnerKind.CODER, wait_timeout=10))
    loop = asyncio.get_running_loop()
    end = loop.time() + 5
    while not await m.waiting_requests(busy):
        assert loop.time() < end
        await asyncio.sleep(0.02)
    report = await m.recover()
    assert report.released == [] and report.cancelled_requests == []
    assert (await m.get_lease(mine.id)).state == "active"  # type: ignore[union-attr]
    assert len(await m.waiting_requests(busy)) == 1
    await mgr(sessionmaker).release(blocker.id)
    assert (await asyncio.wait_for(waiting, 5)).holding


async def test_recover_expires_stale_leases_of_dead_holders(sessionmaker) -> None:
    res = rname()
    dead = mgr(sessionmaker, f"dead-{uuid.uuid4().hex[:6]}")
    lease = await dead.acquire(res, owner_kind=OwnerKind.CODER)
    async with sessionmaker() as s, s.begin():
        await s.execute(
            update(ResourceLease).where(ResourceLease.id == lease.id).values(expires_at=func.clock_timestamp() - timedelta(seconds=5))
        )
    report = await mgr(sessionmaker).recover()
    assert lease.id in [x.id for x in report.sweep.expired]
    assert (await dead.get_lease(lease.id)).state == "expired"  # type: ignore[union-attr]


async def test_stale_request_of_crashed_waiter_does_not_block(sessionmaker) -> None:
    res = rname()
    stale = uuid.uuid4()
    async with sessionmaker() as s, s.begin():
        await s.execute(
            insert(ResourceRequest).values(
                id=stale,
                resource=res,
                owner_kind="video",
                holder="crashed-waiter",
                priority=100,
                state="waiting",
                expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
    m = mgr(sessionmaker)
    lease = await m.acquire(res, owner_kind=OwnerKind.EMBEDDING, wait_timeout=0)
    assert lease.holding
    async with sessionmaker() as s:
        assert (await s.get(ResourceRequest, stale)).state == "cancelled"  # type: ignore[union-attr]
        ev = (
            await s.execute(
                select(Event).where(Event.event_type == EventType.RESOURCE_EXPIRED, Event.payload["request_id"].astext == str(stale))
            )
        ).scalar_one()
    assert ev.payload["reason"] == "request_stale" and ev.payload["subject"] == "request"


async def test_live_waiter_request_is_not_considered_stale(sessionmaker) -> None:
    """A waiter that keeps polling refreshes its request and keeps its place in the queue."""
    res = rname()
    a = mgr(sessionmaker)
    held = await a.acquire(res, owner_kind=OwnerKind.CODER)
    waiter = ResourceManager(sessionmaker, POLICY, f"w-{uuid.uuid4().hex[:6]}", poll_interval=0.05, request_ttl_seconds=0.5)
    t = asyncio.create_task(waiter.acquire(res, owner_kind=OwnerKind.PLANNER, wait_timeout=10))
    await asyncio.sleep(1.5)  # three request TTLs
    [q] = await a.waiting_requests(res)
    assert q.holder == waiter.holder_id
    await a.sweep_expired()
    assert len(await a.waiting_requests(res)) == 1
    await a.release(held.id)
    assert (await asyncio.wait_for(t, 5)).holding


async def test_keeper_survives_transient_heartbeat_errors(sessionmaker, monkeypatch) -> None:
    m = mgr(sessionmaker)
    real = m.heartbeat
    calls = {"n": 0}

    async def flaky(lease_id, **kw):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("select 1", {}, Exception("connection reset"))
        return await real(lease_id, **kw)

    monkeypatch.setattr(m, "heartbeat", flaky)
    async with m.hold(rname(), owner_kind=OwnerKind.CODER, ttl_seconds=1, keeper_interval=0.1) as held:
        await asyncio.sleep(1.5)
        assert held.heartbeat_errors == 1 and not held.lost.is_set()
        assert (await m.status(held.lease.id)).state == "active"
    assert calls["n"] >= 5


async def test_grant_committed_while_cancelled_is_released(sessionmaker, monkeypatch) -> None:
    """Cancellation that arrives right after the grant committed must not leak the lease."""
    m = mgr(sessionmaker)
    res = rname()
    real = m._attempt

    async def attempt_then_cancel(spec, request_id):  # type: ignore[no-untyped-def]
        result = await real(spec, request_id)
        assert result.lease is not None
        raise asyncio.CancelledError

    monkeypatch.setattr(m, "_attempt", attempt_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await m.acquire(res, owner_kind=OwnerKind.CODER)
    async with sessionmaker() as s:
        rows = (await s.execute(select(ResourceLease).where(ResourceLease.resource == res))).scalars().all()
    assert len(rows) == 1 and rows[0].state == "released" and rows[0].release_reason == "acquire_abandoned"


async def test_maintenance_loop_sweeps_and_stops(sessionmaker) -> None:
    m = mgr(sessionmaker)
    res = rname()
    lease = await m.acquire(res, owner_kind=OwnerKind.CODER)
    async with sessionmaker() as s, s.begin():
        await s.execute(
            update(ResourceLease).where(ResourceLease.id == lease.id).values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    stop = asyncio.Event()
    task = asyncio.create_task(m.run_maintenance(stop, interval=0.05))
    loop = asyncio.get_running_loop()
    end = loop.time() + 5
    while (await m.get_lease(lease.id)).state != "expired":  # type: ignore[union-attr]
        assert loop.time() < end
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_purge_finished_requests_keeps_waiting_ones(sessionmaker) -> None:
    m = mgr(sessionmaker)
    res = rname()
    lease = await m.acquire(res, owner_kind=OwnerKind.CODER)
    waiting = asyncio.create_task(mgr(sessionmaker).acquire(res, owner_kind=OwnerKind.CODER, wait_timeout=10))
    loop = asyncio.get_running_loop()
    end = loop.time() + 5
    while not await m.waiting_requests(res):
        assert loop.time() < end
        await asyncio.sleep(0.02)
    async with sessionmaker() as s, s.begin():
        await s.execute(
            update(ResourceRequest).where(ResourceRequest.resource == res).values(created_at=func.clock_timestamp() - timedelta(days=2))
        )
    assert await m.purge_finished_requests() >= 1
    async with sessionmaker() as s:
        states = (await s.execute(select(ResourceRequest.state).where(ResourceRequest.resource == res))).scalars().all()
    assert states == ["waiting"]
    await m.release(lease.id)
    assert (await asyncio.wait_for(waiting, 5)).holding
    assert (await m.get_lease(lease.id)).state == "released"  # type: ignore[union-attr]  # lease history is kept
