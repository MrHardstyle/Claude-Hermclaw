"""Coder loop (P19) against the real tool engine, real context builder, real git workspace and PostgreSQL.
Only the model is scripted (test-only fake)."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from hermclaw.coder import CoderLoop, CoderSettings, StagnationDirective, TurnObservation, history_from_checkpoint
from hermclaw.context_builder import ContextBuilder, ContextBuilderConfig, CorrectionItem, StepBrief
from hermclaw.contracts.scope import ScopeContract
from hermclaw.core.config import get_config
from hermclaw.core.errors import ModelOutputInvalid, ModelTimeout
from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult
from hermclaw.persistence.models import Event, StepAttempt
from hermclaw.scheduler import CancelToken
from tests.integration.test_tools_support import GitCliReader, ScriptedRepo, make_harness

pytestmark = pytest.mark.asyncio(loop_scope="session")
T = TypeVar("T", bound=BaseModel)
PY = sys.executable  # interpreter with pytest (the sandbox runs the workspace with the venv toolchain)
SCOPE = ScopeContract(target_paths=["app.py"], allowed_new_paths=["tests/**"], allowed_operations=["create", "modify"])


class ScriptedCoder:
    """Returns scripted CoderAction dicts (or raises scripted exceptions); records every prompt."""

    def __init__(self, actions: list[dict[str, Any] | Exception]) -> None:
        self.actions = list(actions)
        self.prompts: list[list[ChatMessage]] = []

    async def chat(self, *a: Any, **k: Any) -> ChatResult:  # pragma: no cover - loop uses structured()
        raise NotImplementedError

    async def structured(
        self, alias: str, messages: list[ChatMessage], schema: type[T], *, ctx: CallContext, **_: Any
    ) -> StructuredResult[T]:
        assert alias == "coder-main" and ctx.purpose == "coder_turn"
        self.prompts.append(list(messages))
        if not self.actions:
            raise AssertionError("no scripted action left")
        item = self.actions.pop(0)
        if isinstance(item, Exception):
            raise item
        return StructuredResult(value=schema.model_validate(item), result=ChatResult(content="{}", alias=alias, model="qwen3-coder:30b"))


def act(tool: str, status: str = "", **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args, "status": status or f"{tool}", "decision": tool}


class ScriptedStagnation:
    def __init__(self, directives: dict[int, StagnationDirective]) -> None:
        self.directives = directives
        self.seen: list[TurnObservation] = []

    async def observe(self, obs: TurnObservation) -> StagnationDirective:
        self.seen.append(obs)
        return self.directives.get(obs.turn, StagnationDirective())


async def _setup(
    sessionmaker: Any, tmp_repo: Path, actions: list[Any], *, max_turns: int = 20, stagnation: Any = None
) -> tuple[Any, CoderLoop, ScriptedCoder]:
    (tmp_repo / "tests").mkdir(exist_ok=True)
    h = await make_harness(sessionmaker, tmp_repo, contract=SCOPE)
    async with sessionmaker() as s:
        s.add(StepAttempt(id=h.attempt_id, step_id=h.step.id, job_id=h.step.job_id, attempt_no=1, kind="initial", status="running"))
        await s.commit()
    chat = ScriptedCoder(actions)
    builder = ContextBuilder(ScriptedRepo(), GitCliReader(), ContextBuilderConfig.from_config(get_config()))
    loop = CoderLoop(chat, builder, h.engine, settings=CoderSettings(max_turns=max_turns), stagnation=stagnation, sessionmaker=sessionmaker)
    return h, loop, chat


def _brief() -> StepBrief:
    return StepBrief(
        goal="Make add() also accept a third optional argument c (default 0).",
        kind="implement",
        title="extend add",
        step_key="S001",
        scope=SCOPE,
        repo_hints=("app.py",),
    )


async def _run(h: Any, loop: CoderLoop, token: CancelToken | None = None, **kw: Any) -> Any:
    return await loop.run(
        step=_brief(),
        workspace=h.workspace,
        job_id=h.step.job_id,
        step_id=h.step.id,
        attempt_id=h.attempt_id,
        token=token or CancelToken(),
        **kw,
    )


TEST_FILE = "from app import add\n\n\ndef test_add3():\n    assert add(1, 2, 3) == 6\n\n\ndef test_add2():\n    assert add(1, 2) == 3\n"


async def test_full_turn_sequence_completes_step(sessionmaker: Any, tmp_repo: Path) -> None:
    actions = [
        act("read_file", "Lese app.py", path="app.py"),
        act("write_file", "Test anlegen", path="tests/test_app.py", content=TEST_FILE),
        act("run_test", "Test (rot)", command=f"{PY} -m pytest -q -p no:cacheprovider tests/test_app.py"),
        act(
            "replace_text",
            "add erweitern",
            path="app.py",
            old="def add(a, b):\n    return a + b",
            new="def add(a, b, c=0):\n    return a + b + c",
        ),
        act("run_test", "Test (grün)", command=f"{PY} -m pytest -q -p no:cacheprovider tests/test_app.py"),
        act("git_diff", "Diff prüfen"),
        act(
            "complete_step",
            "fertig",
            summary="add() accepts an optional third argument",
            changed_files=["app.py", "tests/test_app.py"],
            tests_run=["tests/test_app.py"],
        ),
    ]
    h, loop, chat = await _setup(sessionmaker, tmp_repo, actions)
    res = await _run(h, loop)
    assert res.outcome == "completed", res
    assert res.turns == 7 and [r.tool for r in res.history] == [
        "read_file",
        "write_file",
        "run_test",
        "replace_text",
        "run_test",
        "git_diff",
        "complete_step",
    ]
    assert res.history[2].ok is False and res.history[4].ok is True, "first test run must fail, second pass"
    assert "a + b + c" in (tmp_repo / "app.py").read_text()
    assert res.completion and res.completion["report"]["changed_files"] == ["app.py", "tests/test_app.py"]
    # the failing test output was fed into the next turn's prompt; prompts never contain reasoning fields
    assert any("test_add3" in m.content for m in chat.prompts[3])
    # per-turn persistence of the digested history + context telemetry + status lines
    async with sessionmaker() as s:
        att = (await s.execute(select(StepAttempt).where(StepAttempt.id == h.attempt_id))).scalar_one()
        types = list((await s.execute(select(Event.event_type).where(Event.attempt_id == h.attempt_id))).scalars())
    assert att.turns_used == 7 and len(att.history) == 7 and att.history[0]["tool"] == "read_file"
    assert types.count("context.built") == 7 and "status" in types


async def test_out_of_scope_write_is_refused_and_reported_to_the_model(sessionmaker: Any, tmp_repo: Path) -> None:
    actions = [
        act("write_file", path="secrets.txt", content="nope"),
        act("block_step", reason_code="scope_unavailable", message="needs secrets.txt which is out of scope"),
    ]
    h, loop, chat = await _setup(sessionmaker, tmp_repo, actions)
    res = await _run(h, loop)
    assert res.outcome == "blocked" and res.block and res.block["reason_code"] == "scope_unavailable"
    assert res.history[0].ok is False and res.history[0].error_code
    assert not (tmp_repo / "secrets.txt").exists()
    assert any("secrets.txt" in m.content for m in chat.prompts[1])


async def test_turn_budget_exhausted(sessionmaker: Any, tmp_repo: Path) -> None:
    h, loop, _ = await _setup(sessionmaker, tmp_repo, [act("read_file", path="app.py")] * 3, max_turns=3)
    res = await _run(h, loop)
    assert res.outcome == "budget_exhausted" and res.turns == 3 and res.error_code == "TURN_BUDGET_EXHAUSTED"


async def test_stagnation_diagnosis_is_injected_and_stop_ends_loop(sessionmaker: Any, tmp_repo: Path) -> None:
    hook = ScriptedStagnation(
        {
            2: StagnationDirective("diagnose", message="FORCED DIAGNOSIS: same action repeated; state the root cause and change approach."),
            3: StagnationDirective("stop", recommendation="heavy_review", reasons=("same failing test 3x",)),
        }
    )
    h, loop, chat = await _setup(sessionmaker, tmp_repo, [act("read_file", path="app.py")] * 3, stagnation=hook)
    res = await _run(h, loop)
    assert res.outcome == "stagnated" and res.recommendation == "heavy_review" and res.error_code == "STAGNATION"
    assert any("FORCED DIAGNOSIS" in m.content for m in chat.prompts[2])
    assert not any("FORCED DIAGNOSIS" in m.content for m in chat.prompts[1])
    assert [o.turn for o in hook.seen] == [1, 2, 3]


async def test_invalid_model_output_twice_fails_and_timeout_is_model_failure(sessionmaker: Any, tmp_repo: Path) -> None:
    bad = ModelOutputInvalid("no valid action", code="MODEL_OUTPUT_INVALID")
    h, loop, chat = await _setup(sessionmaker, tmp_repo, [bad, bad])
    res = await _run(h, loop)
    assert res.outcome == "model_failed" and res.turns == 2 and res.error_code == "MODEL_OUTPUT_INVALID"
    assert [r.tool for r in res.history] == ["invalid_action", "invalid_action"]
    assert any("not one valid action" in m.content for m in chat.prompts[1])

    h2, loop2, _ = await _setup(sessionmaker, tmp_repo, [act("read_file", path="app.py"), ModelTimeout("slow", code="MODEL_TIMEOUT")])
    res2 = await _run(h2, loop2)
    assert res2.outcome == "model_failed" and res2.turns == 1 and res2.error_code == "MODEL_TIMEOUT"


async def test_cancel_and_checkpoint_resume(sessionmaker: Any, tmp_repo: Path) -> None:
    token = CancelToken()
    token.cancel("job cancelled")
    h, loop, chat = await _setup(sessionmaker, tmp_repo, [])
    assert (await _run(h, loop, token)).outcome == "cancelled" and chat.prompts == []

    class PauseAfterFirst(ScriptedStagnation):
        async def observe(self, obs: TurnObservation) -> StagnationDirective:
            pause.request_checkpoint("job paused")
            return StagnationDirective()

    pause = CancelToken()
    h2, loop2, _ = await _setup(sessionmaker, tmp_repo, [act("read_file", path="app.py")], stagnation=PauseAfterFirst({}))
    res = await _run(h2, loop2, pause)
    assert res.outcome == "checkpointed" and res.turns == 1
    start, history, failure = history_from_checkpoint(res.checkpoint())
    assert start == 1 and [r.tool for r in history] == ["read_file"]
    # resume in a fresh loop/attempt with the restored history
    h3, loop3, chat3 = await _setup(sessionmaker, tmp_repo, [act("complete_step", summary="nothing else needed", changed_files=[])])
    res3 = await _run(h3, loop3, history=history, start_turn=start + 1, last_failure=failure)
    assert res3.outcome == "completed" and res3.turns == 2 and len(res3.history) == 2
    assert any("read_file" in m.content for m in chat3.prompts[0]), "restored history must be in the prompt"


async def test_request_replan_and_correction_items_reach_prompt(sessionmaker: Any, tmp_repo: Path) -> None:
    h, loop, chat = await _setup(sessionmaker, tmp_repo, [act("request_replan", reason="the endpoint lives in another service entirely")])
    correction = [CorrectionItem(source="verifier", label="presence", message="pattern 'offset' not found in app.py", path="app.py")]
    res = await _run(h, loop, correction=correction)
    assert res.outcome == "replan" and "another service" in (res.replan_reason or "")
    assert any("pattern 'offset' not found" in m.content for m in chat.prompts[0])


async def test_no_reasoning_or_secrets_in_history(sessionmaker: Any, tmp_repo: Path) -> None:
    secret = "glpat-" + "A" * 24
    actions = [act("run_command", "Echo", command=f"echo {secret}"), act("block_step", reason_code="other", message="done probing")]
    h, loop, _ = await _setup(sessionmaker, tmp_repo, actions)
    res = await _run(h, loop)
    dump = str([r.__dict__ for r in res.history])
    assert secret not in dump
    async with sessionmaker() as s:
        att = (await s.execute(select(StepAttempt).where(StepAttempt.id == h.attempt_id))).scalar_one()
    assert secret not in str(att.history) and "reasoning" not in str(att.history)
    assert uuid.UUID(str(h.attempt_id))
