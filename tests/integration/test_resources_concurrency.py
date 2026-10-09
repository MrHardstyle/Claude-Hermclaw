"""Concurrency proofs for the resource manager: many tasks, several engines (= processes), real PostgreSQL (P09)."""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import LeasePolicy
from hermclaw.persistence.db import make_engine
from hermclaw.persistence.models import ResourceLease, ResourceRequest
from hermclaw.resources import OwnerKind, ResourceBudget, ResourceManager

pytestmark = pytest.mark.integration

POLICY = LeasePolicy(default_ttl_seconds=30, heartbeat_seconds=1, preemption_grace_seconds=2)


def rname(prefix: str = "res") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest.fixture
async def second_sessionmaker(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    eng = make_engine(db_url, pool_size=12)
    yield async_sessionmaker(eng, expire_on_commit=False, class_=AsyncSession)
    await eng.dispose()


async def holding_count(sm, resource: str) -> int:
    async with sm() as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(ResourceLease)
                .where(ResourceLease.resource == resource, ResourceLease.state.in_(["active", "preempting"]))
            )
            or 0
        )


async def test_exclusivity_never_violated_under_concurrency(sessionmaker, second_sessionmaker) -> None:
    res = rname("excl")
    in_section = 0
    max_in_section = 0
    grants = 0
    violations: list[int] = []

    async def worker(i: int) -> None:
        nonlocal in_section, max_in_section, grants
        sm = sessionmaker if i % 2 == 0 else second_sessionmaker
        m = ResourceManager(sm, POLICY, f"proc-{i}-{uuid.uuid4().hex[:6]}", poll_interval=0.01)
        for _ in range(3):
            lease = await m.acquire(res, owner_kind=OwnerKind.CODER, wait_timeout=60)
            in_section += 1
            grants += 1
            max_in_section = max(max_in_section, in_section)
            n = await holding_count(sm, res)
            if n != 1:
                violations.append(n)
            await asyncio.sleep(random.uniform(0, 0.01))
            in_section -= 1
            assert await m.release(lease.id)

    await asyncio.wait_for(asyncio.gather(*(worker(i) for i in range(24))), 120)
    assert grants == 72
    assert max_in_section == 1
    assert violations == []
    assert await holding_count(sessionmaker, res) == 0
    async with sessionmaker() as s:
        states = (await s.execute(select(ResourceRequest.state).where(ResourceRequest.resource == res))).scalars().all()
    assert len(states) == 72 and set(states) == {"granted"}


async def test_budget_never_exceeded_under_concurrency(sessionmaker, second_sessionmaker) -> None:
    big, small = rname("big"), rname("small")
    budget = ResourceBudget(name=rname("budget"), capacity=10, members=frozenset({big, small}))
    current = 0.0
    peak = 0.0
    db_peak = 0.0
    big_concurrent = 0
    big_peak = 0

    async def worker(i: int) -> None:
        nonlocal current, peak, db_peak, big_concurrent, big_peak
        sm = sessionmaker if i % 2 == 0 else second_sessionmaker
        m = ResourceManager(sm, POLICY, f"bud-{i}-{uuid.uuid4().hex[:6]}", budgets=[budget], poll_interval=0.01)
        for j in range(3):
            if (i + j) % 4 == 0:
                lease = await m.acquire(big, owner_kind=OwnerKind.CODER, weight=6, wait_timeout=60)
                big_concurrent += 1
                big_peak = max(big_peak, big_concurrent)
            else:
                lease = await m.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=random.choice([1, 2, 3]), wait_timeout=60)
            current += lease.weight
            peak = max(peak, current)
            db_peak = max(db_peak, (await m.budget_usage(budget)).used)
            await asyncio.sleep(random.uniform(0, 0.01))
            current -= lease.weight
            if lease.resource == big:
                big_concurrent -= 1
            await m.release(lease.id)

    await asyncio.wait_for(asyncio.gather(*(worker(i) for i in range(20))), 120)
    assert 0 < peak <= 10
    assert db_peak <= 10
    assert big_peak == 1


async def test_priority_order_holds_under_concurrency(sessionmaker, second_sessionmaker) -> None:
    res = rname("prio")
    holder = ResourceManager(sessionmaker, POLICY, f"holder-{uuid.uuid4().hex[:6]}", poll_interval=0.01)
    held = await holder.acquire(res, owner_kind=OwnerKind.CODER)
    priorities = [random.randint(1, 99) for _ in range(14)]
    granted: list[int] = []

    async def waiter(i: int, prio: int) -> None:
        sm = sessionmaker if i % 2 == 0 else second_sessionmaker
        m = ResourceManager(sm, POLICY, f"w-{i}-{uuid.uuid4().hex[:6]}", poll_interval=0.01)
        lease = await m.acquire(res, owner_kind="exec", priority=prio, wait_timeout=60)
        granted.append(prio)
        await asyncio.sleep(0.01)
        await m.release(lease.id)

    tasks = [asyncio.create_task(waiter(i, p)) for i, p in enumerate(priorities)]
    loop = asyncio.get_running_loop()
    end = loop.time() + 20
    while len(await holder.waiting_requests(res)) < len(priorities):
        assert loop.time() < end
        await asyncio.sleep(0.02)
    await holder.release(held.id)
    await asyncio.wait_for(asyncio.gather(*tasks), 60)
    assert granted == sorted(priorities, reverse=True)


async def test_unique_index_rejects_second_exclusive_lease(sessionmaker) -> None:
    res = rname("idx")
    row = {
        "resource": res,
        "resource_group": res,
        "owner_kind": "coder",
        "holder": "raw",
        "priority": 50,
        "state": "active",
        "exclusive": True,
        "weight": 0.0,
        "preemptible": True,
        "expires_at": func.now(),
    }
    async with sessionmaker() as s, s.begin():
        await s.execute(insert(ResourceLease).values(id=uuid.uuid4(), **row))
    with pytest.raises(IntegrityError):
        async with sessionmaker() as s, s.begin():
            await s.execute(insert(ResourceLease).values(id=uuid.uuid4(), **{**row, "state": "preempting"}))


class _RacyManager(ResourceManager):
    """Simulates a broken/raced decision: always believes the resource is free."""

    async def _blocked_reason(self, s, spec, req, now):  # type: ignore[no-untyped-def]
        return None


async def test_unique_index_race_is_treated_as_contention(sessionmaker) -> None:
    res = rname("race")
    good = ResourceManager(sessionmaker, POLICY, f"good-{uuid.uuid4().hex[:6]}", poll_interval=0.01)
    held = await good.acquire(res, owner_kind=OwnerKind.CODER)
    racy = _RacyManager(sessionmaker, POLICY, f"racy-{uuid.uuid4().hex[:6]}", poll_interval=0.01)
    assert await racy.try_acquire(res, owner_kind=OwnerKind.PLANNER) is None  # IntegrityError -> not granted, no crash
    assert [lease.id for lease in await good.list_leases(res)] == [held.id]
    assert await racy.waiting_requests(res) == []  # the abandoned request was cancelled
