import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from hermclaw.contracts.events import EventType
from hermclaw.events.store import EventBroadcaster, append_event, list_events, purge_events, stream_events
from hermclaw.persistence.models import Event, Job

pytestmark = pytest.mark.integration


async def _job(sessionmaker, status="queued"):
    async with sessionmaker() as s:
        j = Job(title="ev", prompt="p", status=status)
        s.add(j)
        await s.commit()
        return j.id


async def test_append_orders_and_redacts(sessionmaker):
    job_id = await _job(sessionmaker)
    async with sessionmaker() as s:
        for i in range(5):
            await append_event(s, EventType.STATUS, source_type="test", job_id=job_id, payload={"i": i, "password": "hunter2xx"})
        await s.commit()
    async with sessionmaker() as s:
        evs = await list_events(s, job_id=job_id)
    seqs = [e.sequence for e in evs]
    assert seqs == sorted(seqs) and len(seqs) == 5
    assert all(e.payload["password"] == "***REDACTED***" for e in evs)
    assert [e.payload["i"] for e in evs] == list(range(5))


async def test_sse_stream_replay_then_live_and_reconnect(sessionmaker, db_url):
    job_id = await _job(sessionmaker)
    async with sessionmaker() as s:
        first = await append_event(s, EventType.STATUS, source_type="test", job_id=job_id, payload={"n": 1})
        await append_event(s, EventType.STATUS, source_type="test", job_id=job_id, payload={"n": 2})
        await s.commit()
        first_seq = first.sequence
    b = EventBroadcaster(db_url)
    await b.start()
    await asyncio.wait_for(b.connected.wait(), 10)
    got = []

    async def consume(last_id):
        async for env in stream_events(sessionmaker, b, job_id=job_id, last_event_id=last_id, heartbeat_seconds=0.5):
            if env is None:
                continue
            got.append(env.payload["n"])
            if len(got) >= 2 and got[-1] == 3:
                return

    task = asyncio.create_task(consume(first_seq))  # reconnect with Last-Event-ID = first -> replay only n=2
    await asyncio.sleep(0.5)
    async with sessionmaker() as s:
        await append_event(s, EventType.STATUS, source_type="test", job_id=job_id, payload={"n": 3})
        # event of another job must not reach this stream
        await append_event(s, EventType.STATUS, source_type="test", job_id=uuid.uuid4(), payload={"n": 99})
        await s.commit()
    await asyncio.wait_for(task, 10)
    await b.stop()
    assert got == [2, 3]


async def test_retention_keeps_active_jobs(sessionmaker):
    done = await _job(sessionmaker, status="succeeded")
    active = await _job(sessionmaker, status="running")
    async with sessionmaker() as s:
        for j in (done, active):
            await append_event(s, EventType.STATUS, source_type="test", job_id=j, payload={})
        await s.commit()
        old = datetime.now(UTC) - timedelta(days=200)
        await s.execute(update(Event).where(Event.job_id.in_([done, active])).values(ts=old))
        await s.commit()
        removed = await purge_events(s, retention_days=90)
        await s.commit()
        assert removed >= 1
        assert await list_events(s, job_id=done) == []
        assert len(await list_events(s, job_id=active)) == 1
