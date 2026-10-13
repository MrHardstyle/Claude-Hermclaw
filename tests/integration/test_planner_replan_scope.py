"""Replanning supersedes *every* open scope version of superseded steps (P24 24.6, real PostgreSQL).

Invariant shared with the scope engine (``persist_scope_version``): a step has at most one non-superseded
``scope_contracts`` row. A superseded step whose current scope version is ``unavailable`` (no resolvable scope) must
not keep that row open after the replan.
"""

from __future__ import annotations

import uuid
from collections import Counter

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.events import EventType
from hermclaw.core.config import get_config
from hermclaw.persistence.models import ScopeContractRow, Step
from hermclaw.planner import Replanner
from hermclaw.scope.audit import ScopeAuditor
from tests.integration.test_planner_replan import _events, _planned_fastapi, _replan_answer, _sm, _trigger
from tests.unit.test_planner_support import FASTAPI, ScriptedChat

pytestmark = pytest.mark.integration


async def _scope_rows(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> list[ScopeContractRow]:
    async with sm() as s:
        stmt = (
            select(ScopeContractRow).where(ScopeContractRow.job_id == job_id).order_by(ScopeContractRow.step_id, ScopeContractRow.version)
        )
        return list((await s.execute(stmt)).scalars())


async def _add_unavailable_scopes(sm: async_sessionmaker[AsyncSession], job_id: uuid.UUID, ids: dict[str, uuid.UUID]) -> None:
    """S002: v1 superseded by an ``unavailable`` v2 (current); S003 (running): only an ``unavailable`` v1 (current)."""
    async with sm() as s, s.begin():
        await s.execute(
            update(ScopeContractRow)
            .where(ScopeContractRow.step_id == ids["S002"], ScopeContractRow.version == 1)
            .values(status="superseded", evidence={"superseded": {"by_version": 2, "previous_status": "active"}})
        )
        unavailable = {"unavailable_reason": {"code": "no_resolvable_scope"}}
        s.add(
            ScopeContractRow(
                job_id=job_id,
                step_id=ids["S002"],
                version=2,
                status="unavailable",
                contract={"target_paths": [], "allowed_operations": []},
                evidence=unavailable,
                reason="no resolvable scope",
            )
        )
        s.add(
            ScopeContractRow(
                job_id=job_id,
                step_id=ids["S003"],
                version=1,
                status="unavailable",
                contract={"target_paths": [], "allowed_operations": []},
                evidence=unavailable,
                reason="no resolvable scope",
            )
        )
        await s.execute(update(Step).where(Step.id == ids["S002"]).values(current_scope_version=2))
        await s.execute(update(Step).where(Step.id == ids["S003"]).values(current_scope_version=1))


async def test_replan_supersedes_unavailable_scope_versions(sessionmaker: object) -> None:
    sm = _sm(sessionmaker)
    job_id, ids = await _planned_fastapi(sm, s003_running=True)
    await _add_unavailable_scopes(sm, job_id, ids)
    before = {(r.step_id, r.version): r for r in await _scope_rows(sm, job_id)}

    result = await Replanner(ScriptedChat([_replan_answer()]), sm, get_config()).replan(job_id, _trigger(ids), FASTAPI.inputs)
    assert result.superseded_step_keys == ["S002", "S003"] and result.preserved_step_keys == ["S001"]

    rows = await _scope_rows(sm, job_id)
    by_key = {(r.step_id, r.version): r for r in rows}
    # every open version of a superseded step is superseded now – unavailable ones included
    for key in [(ids["S002"], 2), (ids["S003"], 1)]:
        row = by_key[key]
        assert row.status == "superseded", key
        assert "plan version 2" in (row.reason or "")
        assert row.evidence["superseded"]["previous_status"] == "unavailable"
        assert row.evidence["superseded"]["previous_reason"] == "no resolvable scope"
        assert row.evidence["superseded"]["by_plan_version"] == 2
        assert row.evidence["unavailable_reason"] == {"code": "no_resolvable_scope"}  # evidence preserved
    # an already superseded version is left exactly as it was
    old, old_before = by_key[(ids["S002"], 1)], before[(ids["S002"], 1)]
    assert old.status == "superseded" and old.evidence == old_before.evidence and old.reason == old_before.reason
    # the kept completed step's active scope is untouched
    assert by_key[(ids["S001"], 1)].status == "active" and "superseded" not in by_key[(ids["S001"], 1)].evidence

    # invariant: at most one non-superseded scope row per step
    open_rows = Counter(r.step_id for r in rows if r.status != "superseded")
    assert open_rows == Counter({ids["S001"]: 1})

    created = next(p for t, p in await _events(sm, job_id) if t == EventType.REPLAN_CREATED)
    assert created["scopes_superseded"] == 2  # S002 v2 + S003 v1; S002 v1 was already superseded

    # the superseded step's current version is no longer an open scope: audits deny everything
    report = await ScopeAuditor(sm, get_config()).audit_changes(ids["S003"], [("README.md", "modify")])
    assert report.scope_status == "superseded" and not report.ok
