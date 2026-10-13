"""``RepoIntelligence.drop_workspace_index`` releases the in-memory state of the dropped workspace key/root.

The scheduler keeps one ``RepoIntelligence`` for its whole lifetime and the workspace janitor drops one index key
per finished job; finished index tasks, freshness locks and embedding retry clocks of removed workspaces used to
stay in the service forever (unbounded growth)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.repo_intelligence import RepoIntelligence
from tests.integration.test_repo_intelligence_support import HashingEmbedder, build_fixture_repo, workspace_handle

pytestmark = pytest.mark.integration


def _state_for(svc: RepoIntelligence, key: str, root: str) -> dict[str, int]:
    return {
        "index_tasks": sum(1 for t in svc._index_tasks if t[0] == key),
        "fresh_locks": int(key in svc._fresh_locks),
        "fresh": int(key in svc._fresh),
        "embed_attempt": int(key in svc._embed_attempt),
        "state_cache": int(root in svc._state_cache),
        "inventory_cache": sum(1 for ck in svc._inventory_cache if ck[0] == root),
        "parse_cache": sum(1 for pk in svc._parse_cache if pk[0] == root),
    }


async def test_drop_workspace_index_forgets_the_in_memory_state_of_that_workspace(
    sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    root1 = build_fixture_repo(tmp_path / "ws1")
    root2 = tmp_path / "ws2"
    shutil.copytree(root1, root2)
    svc = RepoIntelligence(sessionmaker, embedder=HashingEmbedder())
    try:
        ws1, ws2 = workspace_handle(root1, key="drop-state"), workspace_handle(root2, key="drop-state")
        for ws in (ws1, ws2):
            assert await svc.find_symbol(ws, "health")
            await svc.inventory_summary(ws)
        await svc.wait_background()
        k1, k2 = svc.index_key(ws1), svc.index_key(ws2)
        before = _state_for(svc, k1, str(root1))
        assert before["index_tasks"] and before["fresh_locks"] and before["fresh"], before
        assert before["embed_attempt"] and before["state_cache"] and before["inventory_cache"], before

        counts = await svc.drop_workspace_index(ws1)
        assert counts["symbols"] > 0 and counts["chunks"] > 0

        assert _state_for(svc, k1, str(root1)) == dict.fromkeys(before, 0), "nothing of the dropped workspace stays in memory"
        other = _state_for(svc, k2, str(root2))
        assert other["index_tasks"] and other["fresh_locks"] and other["fresh"] and other["state_cache"], "other workspaces keep theirs"
        # the dropped workspace can still be used again (a retried job re-creates it): state is rebuilt lazily
        assert await svc.find_symbol(ws1, "health")
        assert _state_for(svc, k1, str(root1))["fresh"] == 1
    finally:
        await svc.aclose()
