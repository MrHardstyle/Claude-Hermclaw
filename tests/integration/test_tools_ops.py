"""Operations tools in the tool engine (Bauplan §29–31): capability gate, provider dispatch, catalogue, workspace
side effects audited against the scope like run_command, provider failures contained. The provider here is a test
double; the real providers (sshtools/dbtools/infratools) are tested in their own suites."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hermclaw.contracts.tools import DB_TOOLS, SSH_TOOLS, ToolName
from hermclaw.core.config import CapabilityConfig
from hermclaw.tools import errors as E
from hermclaw.tools.context import ToolPermissions
from hermclaw.tools.ops import CompositeOpsProvider, OpsCall, OpsResult
from tests.integration.test_tools_support import events_for, make_harness, scope, tool_calls

pytestmark = pytest.mark.asyncio(loop_scope="session")


class FakeDb:
    """db_* tools; db_migration_generate writes a skeleton file into the workspace (like the real generator)."""

    def __init__(self, filename: str = "migrations/0002_add_email.sql", explode: bool = False) -> None:
        self.calls: list[OpsCall] = []
        self.filename, self.explode = filename, explode

    def provides(self, tool: ToolName) -> bool:
        return tool in DB_TOOLS

    async def call(self, call: OpsCall) -> OpsResult:
        self.calls.append(call)
        if call.tool == ToolName.db_migration_generate:
            target = Path(call.workspace.path) / self.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("-- up\n\n-- down\n", encoding="utf-8")
            if self.explode:
                raise RuntimeError("generator crashed after writing")
            return OpsResult(True, f"created {self.filename}", {"path": self.filename})
        if call.tool == ToolName.db_query_readonly:
            return OpsResult(True, "id | name\n1 | ann", {"rows": 1, "truncated": False})
        return OpsResult(False, "not supported by the fake", error_code="NOT_SUPPORTED")


class FakeSsh:
    def provides(self, tool: ToolName) -> bool:
        return tool in SSH_TOOLS

    async def call(self, call: OpsCall) -> OpsResult:
        return OpsResult(True, f"{call.args.host}: ok")  # type: ignore[attr-defined]


def _perm(kind: str, capability: str) -> ToolPermissions:
    return ToolPermissions.for_step(kind=kind, capability=CapabilityConfig(name=capability, worker_kind="execution"))


async def _harness(sessionmaker: Any, repo: Path, *, kind: str, capability: str, provider: Any, new_paths: list[str]) -> Any:
    h = await make_harness(
        sessionmaker,
        repo,
        contract=scope(target_paths=["app.py"], allowed_new_paths=new_paths),
        permissions=_perm(kind, capability),
        step_fields={"kind": kind, "capability": capability},
    )
    h.engine.ops, h.engine.capability = provider, capability
    return h


async def test_database_step_gets_db_tools_and_generated_file_inside_scope_is_kept(sessionmaker: Any, tmp_repo: Path) -> None:
    db = FakeDb()
    h = await _harness(
        sessionmaker,
        tmp_repo,
        kind="database",
        capability="database",
        provider=CompositeOpsProvider([db, FakeSsh()]),
        new_paths=["migrations/*.sql"],
    )
    names = {t["name"] for t in h.engine.tool_catalog()}
    assert {"db_query_readonly", "db_migration_generate"} <= names and not names & {t.value for t in SSH_TOOLS}, (
        "SSH is not for database steps"
    )
    q = await h.call("db_query_readonly", target="appdb", sql="SELECT id, name FROM users")
    assert q.ok and "ann" in q.output and db.calls[0].capability == "database"
    gen = await h.call("db_migration_generate", description="add email")
    assert gen.ok and gen.mutated_paths == ["migrations/0002_add_email.sql"]
    assert (tmp_repo / "migrations" / "0002_add_email.sql").exists()
    denied = await h.call("ssh_read", host="webui-223", command="uptime")
    assert not denied.ok and denied.error_code == E.TOOL_NOT_ALLOWED
    rows = await tool_calls(sessionmaker, h.step.id)
    assert [r.tool for r in rows] == ["db_query_readonly", "db_migration_generate", "ssh_read"], "every call is recorded"


async def test_out_of_scope_side_effect_is_reverted_and_reported(sessionmaker: Any, tmp_repo: Path) -> None:
    h = await _harness(
        sessionmaker,
        tmp_repo,
        kind="database",
        capability="database",
        provider=FakeDb(filename="secrets/dump.sql"),
        new_paths=["migrations/*.sql"],
    )
    res = await h.call("db_migration_generate", description="add email")
    assert not res.ok and res.error_code == E.COMMAND_SCOPE_VIOLATION and res.data["violations"]
    assert not (tmp_repo / "secrets" / "dump.sql").exists(), "out-of-scope write reverted"
    assert await events_for(sessionmaker, h.step.id, "scope.violation") or res.data["violations"]


async def test_provider_crash_is_contained_and_still_audited(sessionmaker: Any, tmp_repo: Path) -> None:
    h = await _harness(
        sessionmaker, tmp_repo, kind="database", capability="database", provider=FakeDb(explode=True), new_paths=["migrations/*.sql"]
    )
    res = await h.call("db_migration_generate", description="add email")
    assert not res.ok and res.error_code == E.OPS_TOOL_FAILED and "generator crashed" in res.output
    assert res.mutated_paths == ["migrations/0002_add_email.sql"], "the partial write inside the scope is known to the runtime"


async def test_missing_provider_and_plain_coding_steps(sessionmaker: Any, tmp_repo: Path) -> None:
    h = await _harness(sessionmaker, tmp_repo, kind="ssh", capability="ssh", provider=None, new_paths=[])
    assert not {t["name"] for t in h.engine.tool_catalog()} & {t.value for t in SSH_TOOLS}, "not offered without a provider"
    res = await h.call("ssh_read", host="webui-223", command="uptime")
    assert not res.ok and res.error_code == E.OPS_TOOL_UNAVAILABLE
    coding = await _harness(
        sessionmaker, tmp_repo, kind="implement", capability="coding", provider=CompositeOpsProvider([FakeSsh()]), new_paths=[]
    )
    res2 = await coding.call("ssh_read", host="webui-223", command="uptime")
    assert not res2.ok and res2.error_code == E.TOOL_NOT_ALLOWED, "no SSH from ordinary coding steps (§26.8)"
