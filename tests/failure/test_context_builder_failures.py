"""Failure behaviour of the context builder: provider errors/timeouts, garbage data, secrets, limits, cancellation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hermclaw.context_builder import ContextBuilder, ContextBuilderConfig, StepBrief, TurnContextInput
from hermclaw.core.errors import ValidationFailed
from hermclaw.core.interfaces import RepoHit, WorkspaceHandle
from hermclaw.core.redaction import Redactor
from tests.unit.test_context_builder_support import WS, FakeGit, FakeRepo, default_repo, make_builder, make_input


@pytest.mark.parametrize("method", ["context_for", "read", "inventory_summary", "search"])
async def test_repo_provider_errors_degrade_gracefully(method: str) -> None:
    repo = default_repo()
    repo.fail = {method}
    builder, _, _ = make_builder(repo=repo)
    built = await builder.build(make_input())
    user = built.messages[1].content
    assert "## STEP GOAL" in user and "## SCOPE" in user
    assert built.report.warnings, method
    assert any(w.startswith(f"repo.{method}") for w in built.report.warnings)
    blob = " ".join(built.report.warnings) + str(built.report.to_event_payload())
    assert "hunter2hunter2" not in blob  # exception text is redacted
    assert "hunter2hunter2" not in user


@pytest.mark.parametrize("method", ["status", "diff", "changed_files"])
async def test_git_reader_errors_degrade_gracefully(method: str) -> None:
    builder, _, _ = make_builder(git=FakeGit(diff_text="diff --git a/x b/x\n", fail={method}))
    built = await builder.build(make_input())
    assert any(w.startswith(f"git.{method}") for w in built.report.warnings)
    assert "## CURRENT REPO FACTS" in built.messages[1].content


async def test_provider_timeouts_are_bounded() -> None:
    repo = default_repo()
    repo.delay = {"context_for": 5.0, "search": 5.0}
    builder, _, _ = make_builder(repo=repo, provider_timeout_seconds=0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    built = await builder.build(make_input())
    assert loop.time() - start < 2.0
    assert any("repo.context_for: timeout" in w for w in built.report.warnings)
    assert "### app.py lines 1-" in built.messages[1].content  # target reads still arrived


async def test_garbage_provider_results_are_ignored() -> None:
    class Garbage(FakeRepo):
        async def context_for(self, workspace: WorkspaceHandle, goal: str, *, budget_chars: int = 24_000) -> Any:
            return [None, "x", RepoHit("", 1, 1), RepoHit("ok.py", 0, -5, float("nan"), "fine\n"), {"path": "a"}]

        async def inventory_summary(self, workspace: WorkspaceHandle) -> Any:
            return ["not", "a", "mapping"]

        async def read(
            self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000
        ) -> Any:
            return 42

    class GarbageGit(FakeGit):
        async def status(self, workspace: WorkspaceHandle) -> Any:
            return "nope"

        async def diff(self, workspace: WorkspaceHandle, paths: list[str] | None = None, *, max_bytes: int = 200_000) -> Any:
            return None

    builder, _, _ = make_builder(repo=Garbage(), git=GarbageGit())
    built = await builder.build(make_input())
    user = built.messages[1].content
    assert "### ok.py lines 1-1 [context]" in user and "fine" in user
    assert not built.report.section("CURRENT DIFF").present
    assert built.report.estimated_prompt_tokens <= builder.plan.total_tokens


async def test_secrets_are_redacted_everywhere_in_the_prompt() -> None:
    repo = default_repo()
    repo.files["settings.py"] = "DEBUG = True\npassword = 'pa55word-very-secret'\nTOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'\n"
    repo.context_hits = [RepoHit("settings.py", 1, 3, 1.0, "")]
    failure = "Traceback: connect failed postgresql://hermclaw:dbpass123@db/x\nAuthorization: Bearer abcdefghijklmnop\n"
    redactor = Redactor(["literal-secret-value"])
    builder = ContextBuilder(repo, FakeGit(), ContextBuilderConfig(), redactor=redactor)
    inp = make_input(latest_failure=failure, completion_contract="never print literal-secret-value")
    built = await builder.build(inp)
    text = "\n".join(m.content for m in built.messages)
    for secret in (
        "pa55word-very-secret",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "dbpass123",
        "abcdefghijklmnop",
        "literal-secret-value",
    ):
        assert secret not in text, secret
    assert "***REDACTED***" in text
    assert "DEBUG = True" in text  # the rest of the snippet is kept


async def test_redaction_cannot_be_disabled() -> None:
    with pytest.raises(TypeError):
        ContextBuilderConfig(redact=False)  # type: ignore[call-arg]
    builder, _, _ = make_builder()
    built = await builder.build(make_input(latest_failure="password=abcd1234 failed\n"))
    assert "abcd1234" not in built.messages[1].content


def test_invalid_turn_input_rejected() -> None:
    step = StepBrief(goal="do it", kind="implement")
    with pytest.raises(ValidationFailed):
        TurnContextInput(step=step, workspace=WS, turn=0, max_turns=20)
    with pytest.raises(ValidationFailed):
        TurnContextInput(step=step, workspace=WS, turn=1, max_turns=0)
    with pytest.raises(ValidationFailed):
        TurnContextInput(step=StepBrief(goal="   ", kind="implement"), workspace=WS, turn=1, max_turns=1)


async def test_turn_budget_exhaustion_is_announced() -> None:
    builder, _, _ = make_builder()
    built = await builder.build(make_input(turn=19, max_turns=20))
    system = built.messages[0].content
    assert "1 turn(s) remain" in system and "almost exhausted" in system
    built2 = await builder.build(make_input(turn=25, max_turns=20))
    assert "0 turn(s) remain" in built2.messages[0].content


async def test_concurrency_limit_is_honoured() -> None:
    active = 0
    peak = 0

    class Counting(FakeRepo):
        async def read(
            self, workspace: WorkspaceHandle, path: str, start: int = 1, end: int | None = None, *, max_chars: int = 12_000
        ) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return await super().read(workspace, path, start, end, max_chars=max_chars)

    base = default_repo()
    repo = Counting(files={f"f{i}.py": "x\n" for i in range(12)} | base.files)
    repo.context_hits = [RepoHit(f"f{i}.py", 1, 1, 1.0, "") for i in range(12)]
    builder, _, _ = make_builder(repo=repo, max_concurrency=2)
    await builder.build(make_input())
    assert peak <= 2


async def test_cancellation_propagates() -> None:
    repo = default_repo()
    repo.delay = {"context_for": 10.0}
    builder, _, _ = make_builder(repo=repo, provider_timeout_seconds=30)
    task = asyncio.create_task(builder.build(make_input()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_many_targets_and_refs_are_capped() -> None:
    from hermclaw.contracts.scope import ScopeContract
    from tests.unit.test_context_builder_support import make_step

    repo = default_repo()
    repo.files.update({f"t{i}.py": "x\n" for i in range(30)})
    step = make_step(scope=ScopeContract(target_paths=[f"t{i}.py" for i in range(30)]))
    failure = "".join(f"t{i}.py:1: error\n" for i in range(30))
    builder, _, _ = make_builder(repo=repo, max_target_files=5, max_failure_refs=3)
    built = await builder.build(make_input(step=step, latest_failure=failure))
    reads = [c[1][0] for c in repo.calls if c[0] == "read"]
    assert len({p for p in reads if p.startswith("t")}) <= 5 + 3
    assert sum(1 for d in built.report.dropped if d.reason == "limit") >= 25
