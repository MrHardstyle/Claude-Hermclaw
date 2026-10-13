"""GET /api/metrics (P42.5): Prometheus text format, read scope, aggregate state only."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from hermclaw.persistence.models import Job, Worker
from tests.integration.test_api import _client, app  # noqa: F401  (fixture)


async def test_metrics_exposition_and_auth(app: Any, sessionmaker: Any) -> None:  # noqa: F811
    wid = f"exec-m-{uuid.uuid4().hex[:6]}"
    async with sessionmaker() as s:
        s.add(Job(title="metrics", prompt="p", status="queued"))
        s.add(Worker(id=wid, hostname="h", address="127.0.0.1", kind="execution", state="ready", last_heartbeat_at=datetime.now(UTC)))
        await s.commit()
    async with _client(app, token=None) as c:
        assert (await c.get("/api/metrics")).status_code == 401
    async with _client(app) as c:
        r = await c.get("/api/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "# TYPE hermclaw_jobs gauge" in body and re.search(r'^hermclaw_jobs\{status="queued"\} [1-9]', body, re.M)
    assert re.search(rf'^hermclaw_worker_up\{{worker="{wid}",kind="execution",state="ready"\}} 1$', body, re.M)
    assert re.search(rf'^hermclaw_worker_heartbeat_age_seconds\{{worker="{wid}"\}} \d', body, re.M)
    assert re.search(r"^hermclaw_steps_in_flight \d+$", body, re.M) and "hermclaw_build_info{version=" in body
    # every sample line is "name{labels} value" or "name value" (parsable by Prometheus)
    for line in body.splitlines():
        assert line.startswith("#") or re.match(r"^[a-z_]+(\{[^}]*\})? -?[0-9.e+]+$", line), line
