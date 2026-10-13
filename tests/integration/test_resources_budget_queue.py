"""Cross-member budget queue rule with the stored request weight/exclusive flag (BUG-030), real PostgreSQL.

A lower-priority request on one member of a budget may only be held back (``budget_queued``) by higher-priority
waiters on *other* members that actually compete for capacity: waiters blocked by exclusivity on their own resource
do not, and a waiter's demand is its stored ``weight``.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, insert, select

from hermclaw.contracts.events import EventType
from hermclaw.core.config import LeasePolicy
from hermclaw.core.errors import ResourceUnavailable
from hermclaw.persistence.models import Event, ResourceRequest
from hermclaw.resources import OwnerKind, ResourceBudget, ResourceManager

pytestmark = pytest.mark.integration

POLICY = LeasePolicy(default_ttl_seconds=30, heartbeat_seconds=1, preemption_grace_seconds=2)


def rname(prefix: str = "res") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def mgr(sessionmaker: Any, budgets: Any = ()) -> ResourceManager:
    return ResourceManager(sessionmaker, POLICY, f"bq-{uuid.uuid4().hex[:8]}", budgets=budgets, poll_interval=0.05)


def budget_of(*members: str, capacity: float = 10) -> ResourceBudget:
    return ResourceBudget(name=rname("budget"), capacity=capacity, members=frozenset(members))


async def wait_blocked(m: ResourceManager, resource: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not await m.waiting_requests(resource):
        assert loop.time() < end, f"no waiting request on {resource}"
        await asyncio.sleep(0.02)


async def add_waiter(sessionmaker: Any, resource: str, *, priority: int, weight: float, exclusive: bool) -> uuid.UUID:
    """A live waiting request of another process that is between two polls (nobody polls it in the test)."""
    rid = uuid.uuid4()
    async with sessionmaker() as s, s.begin():
        await s.execute(
            insert(ResourceRequest).values(
                id=rid,
                resource=resource,
                owner_kind="other",
                holder=f"other-{uuid.uuid4().hex[:6]}",
                priority=priority,
                state="waiting",
                weight=weight,
                exclusive=exclusive,
                expires_at=func.clock_timestamp() + timedelta(seconds=60),
            )
        )
    return rid


@pytest.mark.parametrize("waiter_exclusive", [True, False], ids=["exclusive-waiter-vs-shared-holder", "shared-waiter-vs-exclusive-holder"])
async def test_waiter_blocked_only_by_exclusivity_does_not_hold_back_other_member(sessionmaker: Any, waiter_exclusive: bool) -> None:
    """BUG-030 regression: budget {large, small}; the high-priority request on 'small' waits only because of the
    holders there (exclusivity), not for capacity – a lower-priority 'large' acquisition that fits must be granted."""
    large, small = rname("large"), rname("small")
    budget = budget_of(large, small)
    holder, high, low = mgr(sessionmaker, [budget]), mgr(sessionmaker, [budget]), mgr(sessionmaker, [budget])
    # exclusive waiter vs. a shared holder, or shared waiter vs. an exclusive holder: both are exclusivity conflicts
    held = await holder.acquire(small, owner_kind=OwnerKind.EMBEDDING, exclusive=not waiter_exclusive, weight=2)
    high_task = asyncio.create_task(
        high.acquire(small, owner_kind=OwnerKind.PLANNER, exclusive=waiter_exclusive, weight=6, wait_timeout=10)
    )
    try:
        await wait_blocked(high, small)
        [req] = await high.waiting_requests(small)
        assert req.exclusive is waiter_exclusive and req.weight == 6 and req.priority == 70  # stored, not guessed
        # 2 + 5 fits; 2 + 5 + 6 would not – but the planner does not wait for capacity
        lease = await low.acquire(large, owner_kind=OwnerKind.CODER, weight=5, wait_timeout=0)
        assert lease.holding and (await low.budget_usage(budget)).used == 7
        assert not high_task.done()
        await holder.release(held.id)
        await low.release(lease.id)
        got = await asyncio.wait_for(high_task, 5)
        assert got.exclusive is waiter_exclusive and got.weight == 6
    finally:
        if not high_task.done():
            high_task.cancel()
            with contextlib.suppress(BaseException):
                await high_task
        for m in (holder, high, low):
            await m.release_many([x.id for x in await m.list_leases(holder=m.holder_id)])


async def test_higher_waiter_counts_with_its_stored_weight(sessionmaker: Any) -> None:
    large, small = rname("large"), rname("small")
    budget = budget_of(large, small)
    holder, low = mgr(sessionmaker, [budget]), mgr(sessionmaker, [budget])
    await holder.acquire(small, owner_kind=OwnerKind.EMBEDDING, exclusive=False, weight=2)
    # a planner waiting on the free 'large' with weight 4 is about to claim capacity (it fits, has not polled yet)
    await add_waiter(sessionmaker, large, priority=70, weight=4, exclusive=True)
    first = await low.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=3, wait_timeout=0)  # 2+3+4 <= 10
    assert first.holding
    with pytest.raises(ResourceUnavailable) as ei:  # 2+3+5 = 10 would fit alone, but not with the planner's 4
        await low.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=5, wait_timeout=0)
    assert ei.value.details["blocked"] == "budget_queued"
    # weightless requests take no capacity from the planner
    free = await low.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=0, wait_timeout=0)
    assert free.holding
    # equal priority never holds back
    same = await low.acquire(small, owner_kind=OwnerKind.PLANNER, exclusive=False, weight=1, wait_timeout=0)
    assert same.holding


async def test_weightless_higher_waiter_does_not_hold_back(sessionmaker: Any) -> None:
    large, small = rname("large"), rname("small")
    budget = budget_of(large, small)
    low = mgr(sessionmaker, [budget])
    await add_waiter(sessionmaker, large, priority=100, weight=0, exclusive=True)  # e.g. a media drain lease
    lease = await low.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=10, wait_timeout=0)
    assert lease.holding


async def test_only_the_grantable_head_of_an_exclusive_queue_claims_capacity(sessionmaker: Any) -> None:
    large, small = rname("large"), rname("small")
    budget = budget_of(large, small)
    holder, low = mgr(sessionmaker, [budget]), mgr(sessionmaker, [budget])
    await holder.acquire(small, owner_kind=OwnerKind.EMBEDDING, exclusive=False, weight=1)
    await add_waiter(sessionmaker, large, priority=70, weight=6, exclusive=True)
    await add_waiter(sessionmaker, large, priority=60, weight=6, exclusive=True)  # queued behind the first one
    lease = await low.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=2, wait_timeout=0)  # 1 + 2 + 6
    assert lease.holding
    with pytest.raises(ResourceUnavailable) as ei:  # 1 + 2 + 2 + 6 > 10
        await low.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=2, wait_timeout=0)
    assert ei.value.details["blocked"] == "budget_queued"


async def test_shared_waiters_behind_each_other_all_claim_capacity(sessionmaker: Any) -> None:
    large, small = rname("large"), rname("small")
    budget = budget_of(large, small)
    low = mgr(sessionmaker, [budget])
    await add_waiter(sessionmaker, small, priority=70, weight=3, exclusive=False)
    await add_waiter(sessionmaker, small, priority=60, weight=3, exclusive=False)
    with pytest.raises(ResourceUnavailable) as ei:  # 5 + 3 + 3 > 10: both shared waiters can be granted together
        await low.acquire(large, owner_kind=OwnerKind.CODER, weight=5, wait_timeout=0)
    assert ei.value.details["blocked"] == "budget_queued"
    assert (await low.acquire(large, owner_kind=OwnerKind.CODER, weight=4, wait_timeout=0)).holding  # 4 + 3 + 3


async def test_request_rows_persist_weight_exclusive_and_metadata(sessionmaker: Any) -> None:
    res = rname()
    m = mgr(sessionmaker)
    job, step = uuid.uuid4(), uuid.uuid4()
    lease = await m.acquire(
        res, owner_kind=OwnerKind.FAST, exclusive=False, weight=2.5, job_id=job, step_id=step, metadata={"model_alias": "fast"}
    )
    async with sessionmaker() as s:
        req = (await s.execute(select(ResourceRequest).where(ResourceRequest.resource == res))).scalar_one()
        ev = (
            await s.execute(select(Event).where(Event.event_type == EventType.RESOURCE_REQUESTED, Event.payload["resource"].astext == res))
        ).scalar_one()
    assert req.state == "granted" and req.weight == 2.5 and req.exclusive is False
    assert req.metadata_["incarnation"] == m.incarnation and req.metadata_["info"] == {"model_alias": "fast"}
    assert req.metadata_["ttl_seconds"] == POLICY.default_ttl_seconds and req.metadata_["preemptible"] is True
    assert ev.payload["weight"] == 2.5 and ev.payload["exclusive"] is False
    assert lease.metadata["request_id"] == str(req.id)
