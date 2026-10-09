"""Resource manager against real PostgreSQL: acquire/release/heartbeat/expiry, queue, budgets (P09 9.1–9.5)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from hermclaw.contracts.events import EventType
from hermclaw.core.config import LeasePolicy
from hermclaw.core.errors import ConflictError, NotFoundError, ResourceUnavailable, ValidationFailed
from hermclaw.persistence.models import Event, ResourceLease, ResourceRequest
from hermclaw.resources import PRIORITIES, LeaseLost, LeaseNotOwned, OwnerKind, ResourceBudget, ResourceManager

pytestmark = pytest.mark.integration

POLICY = LeasePolicy(default_ttl_seconds=30, heartbeat_seconds=1, preemption_grace_seconds=2)


def rname(prefix: str = "res") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def mgr(sessionmaker, holder: str | None = None, budgets=(), policy: LeasePolicy = POLICY) -> ResourceManager:
    return ResourceManager(sessionmaker, policy, holder or f"test-{uuid.uuid4().hex[:8]}", budgets=budgets, poll_interval=0.05)


async def events_for(sessionmaker, resource: str, event_type: str | None = None) -> list[Event]:
    async with sessionmaker() as s:
        stmt = select(Event).where(Event.source_type == "resource_manager", Event.payload["resource"].astext == resource)
        if event_type:
            stmt = stmt.where(Event.event_type == event_type)
        return list((await s.execute(stmt.order_by(Event.sequence))).scalars())


async def wait_queued(m: ResourceManager, resource: str, n: int, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while len(await m.waiting_requests(resource)) < n:
        assert loop.time() < end, f"expected {n} waiting requests on {resource}"
        await asyncio.sleep(0.02)


async def test_acquire_release_roundtrip_emits_events(sessionmaker) -> None:
    m = mgr(sessionmaker)
    res = rname()
    job, step = uuid.uuid4(), uuid.uuid4()
    lease = await m.acquire(res, owner_kind=OwnerKind.CODER, job_id=job, step_id=step, ttl_seconds=10)
    assert lease.state == "active" and lease.holding and not lease.should_yield
    assert lease.priority == PRIORITIES["coder"] == 50
    assert lease.exclusive and lease.preemptible and lease.holder == m.holder_id
    assert lease.job_id == job and lease.step_id == step and lease.resource_group == res
    assert timedelta(seconds=9) < lease.expires_at - lease.acquired_at <= timedelta(seconds=10, milliseconds=5)

    assert await m.release(lease.id, "completed", detail="step done") is True
    assert await m.release(lease.id, "completed") is False  # idempotent
    row = await m.get_lease(lease.id)
    assert row is not None and row.state == "released" and row.release_reason == "completed" and row.released_at is not None

    evs = await events_for(sessionmaker, res)
    assert [e.event_type for e in evs] == [EventType.RESOURCE_REQUESTED, EventType.RESOURCE_ACQUIRED, EventType.RESOURCE_RELEASED]
    assert all(e.job_id == job and e.step_id == step and e.source_id == m.holder_id for e in evs)
    assert evs[1].payload["lease_id"] == str(lease.id)
    assert evs[2].payload["reason"] == "completed" and evs[2].payload["detail"] == "step done"
    async with sessionmaker() as s:
        req = (await s.execute(select(ResourceRequest).where(ResourceRequest.resource == res))).scalar_one()
        assert req.state == "granted"


async def test_release_unknown_lease_raises_not_found(sessionmaker) -> None:
    with pytest.raises(NotFoundError):
        await mgr(sessionmaker).release(uuid.uuid4())


async def test_validation_errors(sessionmaker) -> None:
    m = mgr(sessionmaker)
    with pytest.raises(ValidationFailed):
        await m.acquire("Bad Name; drop table", owner_kind="coder")
    with pytest.raises(ValidationFailed):
        await m.acquire(rname(), owner_kind="coder", priority=5000)
    with pytest.raises(ValidationFailed):
        await m.acquire(rname(), owner_kind="coder", ttl_seconds=0)
    with pytest.raises(ValidationFailed):
        await m.acquire(rname(), owner_kind="custom-kind")  # no policy priority for unknown kinds
    with pytest.raises(ValidationFailed):
        await m.acquire(rname(), owner_kind="coder", weight=-1)
    with pytest.raises(ValidationFailed):
        await m.release(uuid.uuid4(), "Free Text Reason")


async def test_heartbeat_extends_expiry_and_checks_owner(sessionmaker) -> None:
    m = mgr(sessionmaker)
    lease = await m.acquire(rname(), owner_kind=OwnerKind.FAST, ttl_seconds=5)
    await asyncio.sleep(0.05)
    st = await m.heartbeat(lease.id, ttl_seconds=20)
    assert st.state == "active" and not st.should_yield
    assert st.expires_at > lease.expires_at + timedelta(seconds=14)
    row = await m.get_lease(lease.id)
    assert row is not None and row.heartbeat_at > lease.heartbeat_at
    # default heartbeat uses the lease's own TTL
    st2 = await m.heartbeat(lease.id)
    assert st2.expires_at < st.expires_at
    with pytest.raises(LeaseNotOwned):
        await mgr(sessionmaker).heartbeat(lease.id)
    with pytest.raises(LeaseLost):
        await m.heartbeat(uuid.uuid4())
    await m.release(lease.id)
    with pytest.raises(LeaseLost):
        await m.heartbeat(lease.id)


async def _force_expired(sessionmaker, lease_id: uuid.UUID) -> None:
    async with sessionmaker() as s, s.begin():
        await s.execute(
            update(ResourceLease).where(ResourceLease.id == lease_id).values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )


async def test_expired_lease_is_swept_with_event_and_heartbeat_reports_loss(sessionmaker) -> None:
    m = mgr(sessionmaker)
    res = rname()
    lease = await m.acquire(res, owner_kind=OwnerKind.CODER)
    await _force_expired(sessionmaker, lease.id)
    assert (await m.status(lease.id)).state == "expired"  # overdue is reported even before the sweep
    assert await m.should_yield(lease.id)
    report = await m.sweep_expired()
    assert lease.id in [x.id for x in report.expired]
    row = await m.get_lease(lease.id)
    assert row is not None and row.state == "expired" and row.release_reason == "ttl_expired"
    [ev] = await events_for(sessionmaker, res, EventType.RESOURCE_EXPIRED)
    assert ev.severity == "warning" and ev.payload["reason"] == "ttl_expired" and ev.payload["subject"] == "lease"
    with pytest.raises(LeaseLost):
        await m.heartbeat(lease.id)
    other = await mgr(sessionmaker).try_acquire(res, owner_kind=OwnerKind.CODER)
    assert other is not None


async def test_overdue_lease_does_not_block_acquire_without_sweeper(sessionmaker) -> None:
    m = mgr(sessionmaker)
    res = rname()
    lease = await m.acquire(res, owner_kind=OwnerKind.CODER)
    await _force_expired(sessionmaker, lease.id)
    other = await mgr(sessionmaker).acquire(res, owner_kind=OwnerKind.FAST, wait_timeout=0)
    assert other.holding
    assert (await m.get_lease(lease.id)).state == "expired"  # type: ignore[union-attr]
    # heartbeat of the overdue lease (before any sweep) also reports the loss
    m2 = mgr(sessionmaker)
    res2 = rname()
    l2 = await m2.acquire(res2, owner_kind=OwnerKind.CODER)
    await _force_expired(sessionmaker, l2.id)
    with pytest.raises(LeaseLost):
        await m2.heartbeat(l2.id)
    assert (await m2.get_lease(l2.id)).state == "expired"  # type: ignore[union-attr]


async def test_exclusive_lease_blocks_until_release(sessionmaker) -> None:
    a, b = mgr(sessionmaker), mgr(sessionmaker)
    res = rname()
    la = await a.acquire(res, owner_kind=OwnerKind.CODER)
    assert await b.try_acquire(res, owner_kind=OwnerKind.CODER) is None
    waiter = asyncio.create_task(b.acquire(res, owner_kind=OwnerKind.CODER, wait_timeout=5))
    await wait_queued(b, res, 1)
    assert not waiter.done()
    await a.release(la.id)
    lb = await asyncio.wait_for(waiter, 5)
    assert lb.holder == b.holder_id
    assert len(await a.list_leases(res)) == 1


async def test_wait_timeout_cancels_request_with_event(sessionmaker) -> None:
    a, b = mgr(sessionmaker), mgr(sessionmaker)
    res = rname()
    await a.acquire(res, owner_kind=OwnerKind.CODER)
    with pytest.raises(ResourceUnavailable) as ei:
        await b.acquire(res, owner_kind=OwnerKind.PLANNER, wait_timeout=0.3)
    assert ei.value.code == "RESOURCE_WAIT_TIMEOUT" and ei.value.details["blocked"] == "held"
    assert await b.waiting_requests(res) == []
    evs = await events_for(sessionmaker, res, EventType.RESOURCE_EXPIRED)
    assert [e.payload["reason"] for e in evs] == ["wait_timeout"] and evs[0].payload["subject"] == "request"


async def test_cancelled_waiter_leaves_no_request(sessionmaker) -> None:
    a, b = mgr(sessionmaker), mgr(sessionmaker)
    res = rname()
    await a.acquire(res, owner_kind=OwnerKind.CODER)
    t = asyncio.create_task(b.acquire(res, owner_kind=OwnerKind.CODER))
    await wait_queued(b, res, 1)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert await b.waiting_requests(res) == []
    async with sessionmaker() as s:
        states = (await s.execute(select(ResourceRequest.state).where(ResourceRequest.resource == res))).scalars().all()
    assert sorted(states) == ["cancelled", "granted"]


async def test_shared_and_exclusive_interplay(sessionmaker) -> None:
    a, b, c = mgr(sessionmaker), mgr(sessionmaker), mgr(sessionmaker)
    res = rname()
    s1 = await a.acquire(res, owner_kind=OwnerKind.FAST, exclusive=False, weight=1)
    s2 = await b.acquire(res, owner_kind=OwnerKind.EMBEDDING, exclusive=False, weight=1)
    assert await c.try_acquire(res, owner_kind=OwnerKind.VIDEO) is None  # exclusive needs all shared holders gone
    await a.release(s1.id)
    assert await c.try_acquire(res, owner_kind=OwnerKind.VIDEO) is None
    await b.release(s2.id)
    ex = await c.acquire(res, owner_kind=OwnerKind.VIDEO, wait_timeout=0)
    assert await a.try_acquire(res, owner_kind=OwnerKind.FAST, exclusive=False) is None  # shared waits for exclusive
    await c.release(ex.id)
    assert await a.try_acquire(res, owner_kind=OwnerKind.FAST, exclusive=False) is not None


async def test_priority_then_fifo_queue_order(sessionmaker) -> None:
    holder = mgr(sessionmaker)
    res = rname()
    held = await holder.acquire(res, owner_kind=OwnerKind.CODER)
    order: list[str] = []

    async def waiter(label: str, prio: int) -> None:
        m = mgr(sessionmaker)
        lease = await m.acquire(res, owner_kind="exec", priority=prio, wait_timeout=10)
        order.append(label)
        await asyncio.sleep(0.03)
        await m.release(lease.id)

    tasks = []
    for i, (label, prio) in enumerate([("p10", 10), ("p50-first", 50), ("p30", 30), ("p50-second", 50), ("p90", 90)]):
        tasks.append(asyncio.create_task(waiter(label, prio)))
        await wait_queued(holder, res, i + 1)
    queue = await holder.waiting_requests(res)
    assert [q.priority for q in queue] == [90, 50, 50, 30, 10]
    await holder.release(held.id)
    await asyncio.wait_for(asyncio.gather(*tasks), 10)
    assert order == ["p90", "p50-first", "p50-second", "p30", "p10"]


async def test_lower_priority_waiter_cannot_grab_free_resource_while_higher_waits(sessionmaker) -> None:
    """The low waiter polls first after the release, the high waiter is not polling at that moment – still the low
    one must wait for the high one."""
    holder = mgr(sessionmaker)
    res = rname()
    held = await holder.acquire(res, owner_kind=OwnerKind.CODER)
    low = mgr(sessionmaker)
    low_task = asyncio.create_task(low.acquire(res, owner_kind=OwnerKind.FAST, wait_timeout=10))
    await wait_queued(holder, res, 1)
    high = ResourceManager(sessionmaker, POLICY, f"high-{uuid.uuid4().hex[:6]}", poll_interval=0.4)
    high_task = asyncio.create_task(high.acquire(res, owner_kind=OwnerKind.PLANNER, wait_timeout=10))
    await wait_queued(holder, res, 2)
    await holder.release(held.id)
    await asyncio.sleep(0.2)  # low polled ~4 times on a free resource
    assert not low_task.done()
    hl = await asyncio.wait_for(high_task, 5)
    assert not low_task.done()
    await high.release(hl.id)
    ll = await asyncio.wait_for(low_task, 5)
    assert ll.holder == low.holder_id


async def test_budget_accounting(sessionmaker) -> None:
    big, small = rname("big"), rname("small")
    budget = ResourceBudget(name=rname("budget"), capacity=10, members=frozenset({big, small}))
    a, b, c = (mgr(sessionmaker, budgets=[budget]) for _ in range(3))
    lb = await a.acquire(big, owner_kind=OwnerKind.CODER, weight=6)
    ls = await b.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=3)
    usage = await a.budget_usage(budget.name)
    assert usage.used == 9 and usage.free == 1 and usage.by_resource == {big: 6.0, small: 3.0}
    assert await c.try_acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=3) is None
    assert await c.try_acquire(small, owner_kind=OwnerKind.EMBEDDING, exclusive=False, weight=1) is not None  # exactly 10
    with pytest.raises(ResourceUnavailable) as ei:
        await c.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=11)
    assert ei.value.code == "RESOURCE_OVER_CAPACITY"
    with pytest.raises(ResourceUnavailable) as ei2:
        await c.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=3, wait_timeout=0.2)
    assert ei2.value.details["blocked"] == "budget"
    await a.release(lb.id)
    assert await c.try_acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=3) is not None
    assert (await a.budget_usage(budget)).used == 7
    await b.release(ls.id)
    with pytest.raises(NotFoundError):
        await a.budget_usage("no-such-budget")


async def test_higher_priority_budget_waiter_blocks_lower_on_other_member(sessionmaker) -> None:
    big, small = rname("big"), rname("small")
    budget = ResourceBudget(name=rname("budget"), capacity=10, members=frozenset({big, small}))
    a, planner, fast = (mgr(sessionmaker, budgets=[budget]) for _ in range(3))
    held_small = await a.acquire(small, owner_kind=OwnerKind.EMBEDDING, exclusive=False, weight=4)
    big_task = asyncio.create_task(planner.acquire(big, owner_kind=OwnerKind.PLANNER, weight=8, wait_timeout=10))
    await wait_queued(a, big, 1)
    # 4 + 1 would fit, but the planner (70) waits for capacity on the same budget: fast (30) must not take it
    with pytest.raises(ResourceUnavailable) as ei:
        await fast.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=1, wait_timeout=0.2)
    assert ei.value.details["blocked"] == "budget_queued"
    await a.release(held_small.id)
    lb = await asyncio.wait_for(big_task, 5)
    assert lb.weight == 8
    assert await fast.try_acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=1) is not None  # 8 + 1 fits


async def test_leases_are_not_reentrant_for_the_same_step(sessionmaker) -> None:
    m = mgr(sessionmaker)
    res, step = rname(), uuid.uuid4()
    await m.acquire(res, owner_kind=OwnerKind.CODER, step_id=step)
    with pytest.raises(ConflictError) as ei:
        await m.acquire(res, owner_kind=OwnerKind.CODER, step_id=step, wait_timeout=1)
    assert ei.value.code == "RESOURCE_ALREADY_HELD"
    assert await m.waiting_requests(res) == []


async def test_hold_context_heartbeats_and_releases(sessionmaker) -> None:
    m = mgr(sessionmaker)
    res = rname()
    async with m.hold(res, owner_kind=OwnerKind.CODER, ttl_seconds=0.6, keeper_interval=0.1) as held:
        await asyncio.sleep(1.2)  # two TTLs: only the keeper's heartbeats keep the lease alive
        assert (await m.status(held.lease.id)).state == "active"
        assert not held.yield_requested.is_set() and not held.lost.is_set()
    row = await m.get_lease(held.lease.id)
    assert row is not None and row.state == "released" and row.release_reason == "completed"

    with pytest.raises(RuntimeError):
        async with m.hold(res, owner_kind=OwnerKind.CODER) as held2:
            raise RuntimeError("boom")
    assert (await m.get_lease(held2.lease.id)).release_reason == "error"  # type: ignore[union-attr]


async def test_keeper_reports_lost_lease(sessionmaker) -> None:
    m = mgr(sessionmaker)
    lost: list[uuid.UUID] = []
    async with m.hold(rname(), owner_kind=OwnerKind.CODER, keeper_interval=0.05, on_lost=lambda lease: lost.append(lease.id)) as held:
        await _force_expired(sessionmaker, held.lease.id)
        await asyncio.wait_for(held.lost.wait(), 3)
    assert lost == [held.lease.id]
    assert (await m.get_lease(held.lease.id)).state == "expired"  # type: ignore[union-attr]


async def test_queries(sessionmaker) -> None:
    m = mgr(sessionmaker)
    res = rname()
    lease = await m.acquire(res, owner_kind=OwnerKind.HEAVY, metadata={"note": "x", "api_key": "sk-supersecretvalue123456"})
    [listed] = await m.list_leases(res)
    assert listed.id == lease.id and listed.priority == 60
    assert await m.list_leases(res, holder="someone-else") == []
    assert await m.get_lease(uuid.uuid4()) is None
    assert lease.metadata["info"]["note"] == "x"
    assert "supersecret" not in str(lease.metadata)  # metadata is redacted before it is stored
    with pytest.raises(NotFoundError):
        await m.status(uuid.uuid4())
    assert await m.should_yield(uuid.uuid4()) is True


async def test_reentrancy_is_detected_even_behind_a_higher_waiter(sessionmaker) -> None:
    """Regression: the self-held check must run before the queue check, otherwise the step waits for itself."""
    m = mgr(sessionmaker)
    res, step = rname(), uuid.uuid4()
    held = await m.acquire(res, owner_kind=OwnerKind.CODER, step_id=step)
    other = mgr(sessionmaker)
    t = asyncio.create_task(other.acquire(res, owner_kind=OwnerKind.VIDEO, wait_timeout=10))
    await wait_queued(m, res, 1)
    with pytest.raises(ConflictError):
        await m.acquire(res, owner_kind=OwnerKind.CODER, step_id=step, wait_timeout=5)
    with pytest.raises(ConflictError):  # shared request while the same step holds the resource exclusively
        await m.acquire(res, owner_kind=OwnerKind.CODER, step_id=step, exclusive=False, wait_timeout=5)
    await m.release(held.id)
    assert (await asyncio.wait_for(t, 5)).holding

    shared_res, shared_step = rname(), uuid.uuid4()
    first = await m.acquire(shared_res, owner_kind=OwnerKind.FAST, step_id=shared_step, exclusive=False)
    second = await m.acquire(shared_res, owner_kind=OwnerKind.FAST, step_id=shared_step, exclusive=False, wait_timeout=0)
    assert first.id != second.id  # shared + shared for the same step is fine
