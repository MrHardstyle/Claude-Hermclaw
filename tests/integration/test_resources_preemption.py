"""Safe preemption, large-model exclusivity and media (video/image) priority against real PostgreSQL (P09 9.6–9.8)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from hermclaw.contracts.events import EventType
from hermclaw.core.config import LeasePolicy, ModelProfileConfig, ModelsConfig
from hermclaw.core.errors import ResourceUnavailable
from hermclaw.persistence.models import Event
from hermclaw.resources import LeaseLost, OwnerKind, ResourceBudget, ResourceManager, model_host_budgets

pytestmark = pytest.mark.integration

POLICY = LeasePolicy(default_ttl_seconds=30, heartbeat_seconds=1, preemption_grace_seconds=2)
FAST_GRACE = LeasePolicy(default_ttl_seconds=30, heartbeat_seconds=1, preemption_grace_seconds=1)


def rname(prefix: str = "res") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def mgr(sessionmaker, budgets=(), policy: LeasePolicy = POLICY) -> ResourceManager:
    return ResourceManager(sessionmaker, policy, f"test-{uuid.uuid4().hex[:8]}", budgets=budgets, poll_interval=0.05)


async def lease_events(sessionmaker, lease_id: uuid.UUID, event_type: str | None = None) -> list[Event]:
    async with sessionmaker() as s:
        stmt = select(Event).where(Event.source_type == "resource_manager", Event.payload["lease_id"].astext == str(lease_id))
        if event_type:
            stmt = stmt.where(Event.event_type == event_type)
        return list((await s.execute(stmt.order_by(Event.sequence))).scalars())


async def test_preemption_happy_path_holder_checkpoints_and_releases(sessionmaker) -> None:
    res = rname()
    coder_mgr, planner_mgr = mgr(sessionmaker), mgr(sessionmaker)
    checkpoints: list[str] = []
    callbacks: list[str] = []
    holder_ready = asyncio.Event()

    async def coder_step() -> uuid.UUID:
        async with coder_mgr.hold(
            res,
            owner_kind=OwnerKind.CODER,
            keeper_interval=0.05,
            on_preempt=lambda lease, status: callbacks.append(status.preempt_reason or ""),
        ) as held:
            holder_ready.set()
            await asyncio.wait_for(held.yield_requested.wait(), 5)  # "work" until the manager asks to yield
            checkpoints.append("checkpoint-saved")
            return held.lease.id

    coder_task = asyncio.create_task(coder_step())
    await holder_ready.wait()
    planner_lease = await planner_mgr.acquire(res, owner_kind=OwnerKind.PLANNER, preempt=True, wait_timeout=5, reason="replan-needed")
    coder_lease_id = await coder_task
    assert checkpoints == ["checkpoint-saved"] and callbacks == ["replan-needed"]
    assert planner_lease.priority == 70

    coder_row = await coder_mgr.get_lease(coder_lease_id)
    assert coder_row is not None and coder_row.state == "released" and coder_row.release_reason == "preempted"
    evs = await lease_events(sessionmaker, coder_lease_id)
    types = [e.event_type for e in evs]
    assert types == [EventType.RESOURCE_ACQUIRED, EventType.RESOURCE_PREEMPT_REQUESTED, EventType.RESOURCE_RELEASED]
    pre = evs[1].payload
    assert pre["action"] == "requested" and pre["requester_priority"] == 70 and pre["requester_kind"] == "planner"
    assert pre["reason"] == "replan-needed" and pre["requester_holder"] == planner_mgr.holder_id
    assert evs[2].payload["was_preempting"] is True
    assert not await lease_events(sessionmaker, coder_lease_id, EventType.RESOURCE_EXPIRED)  # nothing was force-killed


async def test_request_preemption_should_yield_and_grace_cap(sessionmaker) -> None:
    res = rname()
    holder, admin = mgr(sessionmaker), mgr(sessionmaker)
    lease = await holder.acquire(res, owner_kind=OwnerKind.CODER, ttl_seconds=300)
    assert not await holder.should_yield(lease.id)
    result = await admin.request_preemption(res, 100, "video-needs-gpu", requester_kind="video")
    assert [x.id for x in result.requested] == [lease.id] and result.pending[0].state == "preempting"
    assert await holder.should_yield(lease.id)
    st = await holder.heartbeat(lease.id, ttl_seconds=300)
    assert st.state == "preempting" and st.should_yield and st.preempt_reason == "video-needs-gpu"
    assert st.preempt_deadline is not None and st.expires_at <= st.preempt_deadline  # never extended beyond the grace
    again = await admin.request_preemption(res, 100, "video-needs-gpu")
    assert not again.requested and [x.id for x in again.already_preempting] == [lease.id]
    assert await admin.withdraw_preemption(lease.id) is True
    st2 = await holder.heartbeat(lease.id)
    assert st2.state == "active" and not st2.should_yield
    assert await admin.withdraw_preemption(lease.id) is False


async def test_grace_timeout_force_expires_with_event(sessionmaker) -> None:
    res = rname()
    stubborn, planner = mgr(sessionmaker, policy=FAST_GRACE), mgr(sessionmaker, policy=FAST_GRACE)
    lease = await stubborn.acquire(res, owner_kind=OwnerKind.CODER, ttl_seconds=60)
    stop = asyncio.Event()

    async def keep_heartbeating() -> None:  # alive but ignores the preemption request
        with pytest.raises(LeaseLost):
            while not stop.is_set():
                await stubborn.heartbeat(lease.id)
                await asyncio.sleep(0.1)

    hb = asyncio.create_task(keep_heartbeating())
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    planner_lease = await planner.acquire(res, owner_kind=OwnerKind.PLANNER, preempt=True, wait_timeout=10)
    waited = loop.time() - t0
    assert 0.8 <= waited < 5  # the grace period was honoured despite the heartbeats
    await asyncio.wait_for(hb, 5)
    stop.set()
    row = await stubborn.get_lease(lease.id)
    assert row is not None and row.state == "expired" and row.release_reason == "preemption_grace_timeout"
    [ev] = await lease_events(sessionmaker, lease.id, EventType.RESOURCE_EXPIRED)
    assert ev.severity == "warning" and ev.payload["reason"] == "preemption_grace_timeout"
    assert ev.payload["preemption"]["requester_priority"] == 70
    assert planner_lease.holding


async def test_non_preemptible_lease_is_never_preempted(sessionmaker) -> None:
    res = rname()
    holder, video = mgr(sessionmaker), mgr(sessionmaker)
    lease = await holder.acquire(res, owner_kind=OwnerKind.FAST, preemptible=False)
    result = await video.request_preemption(res, 100, "media-video")
    assert not result.requested and [x.id for x in result.refused_non_preemptible] == [lease.id]
    with pytest.raises(ResourceUnavailable):
        await video.acquire(res, owner_kind=OwnerKind.VIDEO, preempt=True, wait_timeout=0.4)
    row = await holder.get_lease(lease.id)
    assert row is not None and row.state == "active" and not await holder.should_yield(lease.id)
    assert not await lease_events(sessionmaker, lease.id, EventType.RESOURCE_PREEMPT_REQUESTED)


async def test_equal_or_higher_priority_holder_is_not_preempted(sessionmaker) -> None:
    res = rname()
    heavy, coder = mgr(sessionmaker), mgr(sessionmaker)
    lease = await heavy.acquire(res, owner_kind=OwnerKind.HEAVY)
    result = await coder.request_preemption(res, 50, "coder-wants-model")
    assert [x.id for x in result.refused_priority] == [lease.id]
    with pytest.raises(ResourceUnavailable):
        await coder.acquire(res, owner_kind=OwnerKind.CODER, preempt=True, wait_timeout=0.3)
    with pytest.raises(ResourceUnavailable):
        await coder.acquire(res, owner_kind=OwnerKind.HEAVY, preempt=True, wait_timeout=0.3)  # equal priority
    assert (await heavy.get_lease(lease.id)).state == "active"  # type: ignore[union-attr]


async def test_preemption_withdrawn_when_requester_gives_up(sessionmaker) -> None:
    res = rname()
    holder, planner = mgr(sessionmaker), mgr(sessionmaker)  # grace 2s > wait timeout
    lease = await holder.acquire(res, owner_kind=OwnerKind.CODER, ttl_seconds=60)
    with pytest.raises(ResourceUnavailable):
        await planner.acquire(res, owner_kind=OwnerKind.PLANNER, preempt=True, wait_timeout=0.3)
    row = await holder.get_lease(lease.id)
    assert row is not None and row.state == "active" and row.preemption is None
    assert (row.expires_at - datetime.now(row.expires_at.tzinfo)).total_seconds() > 30  # TTL restored, not the grace cap
    actions = [e.payload["action"] for e in await lease_events(sessionmaker, lease.id, EventType.RESOURCE_PREEMPT_REQUESTED)]
    assert actions == ["requested", "withdrawn"]


async def test_budget_preemption_picks_minimal_lowest_priority_set(sessionmaker) -> None:
    big, small = rname("big"), rname("small")
    budget = ResourceBudget(name=rname("budget"), capacity=10, members=frozenset({big, small}))
    holders = [mgr(sessionmaker, [budget]) for _ in range(3)]
    emb = await holders[0].acquire(small, owner_kind=OwnerKind.EMBEDDING, exclusive=False, weight=2)
    fast_old = await holders[1].acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=3)
    fast_new = await holders[2].acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=3)
    heavy = mgr(sessionmaker, [budget])
    task = asyncio.create_task(heavy.acquire(big, owner_kind=OwnerKind.HEAVY, weight=6, preempt=True, wait_timeout=10))
    loop = asyncio.get_running_loop()
    end = loop.time() + 5
    preempting: set[uuid.UUID] = set()
    while len(preempting) < 2:
        assert loop.time() < end
        preempting = {x.id for x in await heavy.list_leases(small) if x.state == "preempting"}
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    preempting = {x.id for x in await heavy.list_leases(small) if x.state == "preempting"}
    assert preempting == {emb.id, fast_new.id}  # lowest priority first, then the newest; 2 + 3 >= 8 + 6 - 10
    assert (await holders[1].get_lease(fast_old.id)).state == "active"  # type: ignore[union-attr]
    await holders[0].release(emb.id, "preempted")
    await holders[2].release(fast_new.id, "preempted")
    got = await asyncio.wait_for(task, 5)
    assert got.weight == 6 and (await heavy.budget_usage(budget)).used == 9


def _profiles(large: str, small: str) -> ModelsConfig:
    def p(alias: str, role: str, prio: int, mem: float, group: str, exclusive: bool) -> ModelProfileConfig:
        return ModelProfileConfig(
            alias=alias,
            role=role,
            model=f"{alias}:test",
            priority=prio,
            resource_group=group,
            exclusive=exclusive,
            memory_gb=mem,  # type: ignore[arg-type]
        )

    return ModelsConfig(
        model_host_capacity_gb=34,
        profiles=[
            p("fast-router", "fast", 30, 7, small, False),
            p("planner-gemma", "planner", 70, 19, large, True),
            p("coder-main", "coder", 50, 23, large, True),
            p("heavy-review", "heavy", 60, 25, large, True),
            p("embedding", "embedding", 20, 2, small, False),
        ],
    )


async def test_large_model_exclusivity_and_model_budget(sessionmaker) -> None:
    large, small = rname("large-model"), rname("small-model")
    models = _profiles(large, small)
    [budget] = model_host_budgets(models)
    assert budget.members == {large, small} and budget.capacity == 34
    a, b, c = (mgr(sessionmaker, [budget]) for _ in range(3))
    coder = await a.acquire_model(models.by_alias("coder-main"))
    assert coder.resource == large and coder.exclusive and coder.weight == 23 and coder.priority == 50 and coder.owner_kind == "coder"
    assert coder.metadata["info"]["model_alias"] == "coder-main"
    with pytest.raises(ResourceUnavailable) as ei:  # only one large model at a time
        await b.acquire_model(models.by_alias("planner-gemma"), wait_timeout=0.3)
    assert ei.value.details["blocked"] == "held"
    fast = await c.acquire_model(models.by_alias("fast-router"), wait_timeout=0)
    emb = await c.acquire_model(models.by_alias("embedding"), wait_timeout=0)
    assert not fast.exclusive and fast.weight == 7 and emb.weight == 2
    assert (await a.budget_usage(budget.name)).used == 32
    await a.release(coder.id)
    heavy = await b.acquire_model(models.by_alias("heavy-review"), wait_timeout=1)  # 25 + 7 + 2 = 34 fits exactly
    assert heavy.priority == 60
    with pytest.raises(ResourceUnavailable):  # a second embedding instance would exceed the host
        await a.acquire_model(models.by_alias("embedding"), wait_timeout=0.2)


async def test_model_preemption_for_higher_priority_model(sessionmaker) -> None:
    large, small = rname("large-model"), rname("small-model")
    models = _profiles(large, small)
    budgets = model_host_budgets(models)
    coder_mgr, planner_mgr = mgr(sessionmaker, budgets), mgr(sessionmaker, budgets)

    async def coder_step() -> str:
        async with coder_mgr.hold_model(models.by_alias("coder-main"), keeper_interval=0.05) as held:
            await asyncio.wait_for(held.yield_requested.wait(), 5)
            return "checkpointed"

    t = asyncio.create_task(coder_step())
    await asyncio.sleep(0.2)
    planner = await planner_mgr.acquire_model(models.by_alias("planner-gemma"), preempt=True, wait_timeout=5)
    assert await t == "checkpointed" and planner.resource == large


async def test_video_preempts_ai_model_leases_and_blocks_ai_until_release(sessionmaker) -> None:
    large, small = rname("large-model"), rname("small-model")
    gpu, video = rname("gpu"), rname("video")
    models = _profiles(large, small)
    budgets = model_host_budgets(models)
    ai = [mgr(sessionmaker, budgets) for _ in range(3)]
    media_mgr = mgr(sessionmaker, budgets)
    started = asyncio.Event()
    preempted: list[str] = []

    async def ai_step(m: ResourceManager, alias: str) -> str:
        async with m.hold_model(models.by_alias(alias), keeper_interval=0.05) as held:
            started.set()
            await asyncio.wait_for(held.yield_requested.wait(), 10)
            preempted.append(alias)
            return alias

    ai_tasks = [
        asyncio.create_task(ai_step(ai[0], "coder-main")),
        asyncio.create_task(ai_step(ai[1], "fast-router")),
        asyncio.create_task(ai_step(ai[2], "embedding")),
    ]
    await started.wait()
    await asyncio.sleep(0.2)
    media = await media_mgr.acquire_gpu_for_media("video", gpu_resource=gpu, video_resource=video, drain=(large, small), wait_timeout=10)
    assert sorted(await asyncio.gather(*ai_tasks)) == ["coder-main", "embedding", "fast-router"]
    assert media.gpu.resource == gpu and media.video is not None and media.video.resource == video
    assert {x.resource for x in media.drained} == {large, small}
    assert all(x.priority == 100 and x.owner_kind == "video" and not x.preemptible and x.exclusive for x in media.all)
    assert all(x.weight == 0 for x in media.drained)

    async with sessionmaker() as s:
        pre = list(
            (
                await s.execute(
                    select(Event).where(
                        Event.event_type == EventType.RESOURCE_PREEMPT_REQUESTED, Event.payload["resource"].astext.in_([large, small])
                    )
                )
            ).scalars()
        )
    assert len(pre) == 3 and {e.payload["requester_kind"] for e in pre} == {"video"}
    assert {e.payload["requester_priority"] for e in pre} == {100}
    for alias in ("coder-main", "fast-router", "embedding"):
        [row] = [
            x
            for x in await ai[0].list_leases(states=["released"])
            if x.metadata.get("info", {}).get("model_alias") == alias and x.resource in (large, small)
        ]
        assert row.release_reason == "preempted"

    # while the video runs, AI workloads cannot come back onto the host
    with pytest.raises(ResourceUnavailable):
        await ai[0].acquire_model(models.by_alias("coder-main"), wait_timeout=0.3)
    assert await ai[1].try_acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=7) is None
    # media leases cannot be preempted by AI
    res = await ai[0].request_preemption(gpu, 70, "planner")
    assert not res.requested and res.refused_non_preemptible

    assert await media_mgr.release_media(media) == 4
    resumed = await ai[0].acquire_model(models.by_alias("coder-main"), wait_timeout=2)  # AI resumes
    assert resumed.holding


async def test_image_uses_gpu_only_and_video_queues_ahead_of_image(sessionmaker) -> None:
    large, small = rname("large-model"), rname("small-model")
    gpu, video = rname("gpu"), rname("video")
    budgets = model_host_budgets(_profiles(large, small))
    kw = {"gpu_resource": gpu, "video_resource": video, "drain": (large, small)}
    img_mgr, img2_mgr, vid_mgr = (mgr(sessionmaker, budgets) for _ in range(3))
    image = await img_mgr.acquire_gpu_for_media("image", **kw)
    assert image.video is None and image.gpu.priority == 90 and image.gpu.owner_kind == "image"
    order: list[str] = []

    async def media(m: ResourceManager, kind: str) -> None:
        got = await m.acquire_gpu_for_media(kind, wait_timeout=10, **kw)
        order.append(kind)
        await m.release_media(got)

    img2 = asyncio.create_task(media(img2_mgr, "image"))
    await asyncio.sleep(0.2)
    vid = asyncio.create_task(media(vid_mgr, "video"))
    await asyncio.sleep(0.3)
    await img_mgr.release_media(image)
    await asyncio.wait_for(asyncio.gather(img2, vid), 10)
    assert order == ["video", "image"]  # video (100) overtakes the earlier image request (90)


async def test_media_acquire_timeout_releases_partial_leases(sessionmaker) -> None:
    large, small = rname("large-model"), rname("small-model")
    gpu, video = rname("gpu"), rname("video")
    budgets = model_host_budgets(_profiles(large, small))
    ai, pre_ai, media_mgr = mgr(sessionmaker, budgets), mgr(sessionmaker, budgets), mgr(sessionmaker, budgets)
    stuck = await ai.acquire(large, owner_kind=OwnerKind.CODER, weight=23, preemptible=False)
    soft = await pre_ai.acquire(small, owner_kind=OwnerKind.FAST, exclusive=False, weight=7)
    with pytest.raises(ResourceUnavailable):
        await media_mgr.acquire_gpu_for_media("video", gpu_resource=gpu, video_resource=video, drain=(large, small), wait_timeout=0.5)
    assert await media_mgr.list_leases(gpu) == [] and await media_mgr.list_leases(video) == []
    assert [x.id for x in await media_mgr.list_leases(large)] == [stuck.id]
    soft_row = await pre_ai.get_lease(soft.id)
    assert soft_row is not None and soft_row.state == "active"
    assert soft_row.preemption is None  # the drain preempted it, the abandoned media request withdrew that again
    actions = [e.payload["action"] for e in await lease_events(sessionmaker, soft.id, EventType.RESOURCE_PREEMPT_REQUESTED)]
    assert actions == ["requested", "withdrawn"]
    assert await media_mgr.waiting_requests(large) == [] and await media_mgr.waiting_requests(gpu) == []
    with pytest.raises(Exception, match="media kind"):
        await media_mgr.acquire_gpu_for_media("audio")
