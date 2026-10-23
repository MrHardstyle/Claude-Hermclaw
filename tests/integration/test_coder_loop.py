"""Coder loop (P19) against the real tool engine, real context builder, real git workspace and PostgreSQL.
Only the model is scripted (test-only fake)."""

from __future__ import annotations

import asyncio
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
from hermclaw.core.errors import ModelOutputInvalid, ModelOutputTruncated, ModelTimeout
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


async def test_refused_repeat_keeps_the_real_failure_in_the_next_prompt(sessionmaker: Any, tmp_repo: Path) -> None:
    """Live job 137c9c41 (J2 of the rc11 battery): after a failing test run the coder repeated the command, the repeat
    guard refused it and its refusal replaced the test output as LATEST FAILURE – the traceback was gone, so the model
    kept re-running the tests to see it until stagnation stopped the step. A refused no-op repeat is shown above the
    real latest failure instead of replacing it."""
    cmd = f"{PY} -m pytest -q -p no:cacheprovider tests/test_app.py"
    actions = [
        act("write_file", "Test anlegen", path="tests/test_app.py", content=TEST_FILE),
        act("run_test", "Test (rot)", command=cmd),
        act("run_test", "nochmal", command=cmd),
        act("read_file", path="app.py"),
    ]
    h, loop, chat = await _setup(sessionmaker, tmp_repo, actions, max_turns=4)
    res = await _run(h, loop)
    assert res.history[2].error_code == "COMMAND_REPEATED"
    after_refusal = "\n".join(m.content for m in chat.prompts[3])
    assert "command refused" in after_refusal and "test_add3" in after_refusal
    assert res.last_failure and "test_add3" in res.last_failure and "command refused" not in res.last_failure


async def test_checks_are_withheld_after_a_refused_repeat_until_a_file_changes(sessionmaker: Any, tmp_repo: Path) -> None:
    """Live battery J1 on 0.1.0rc14: after a refused repeat the coder proposed the same test command three more times.
    run_test/run_command are withheld until something changes; an edit brings them back (BUG-181). The catalogue itself
    stays the same – it is part of the cached system message (D-026)."""
    cmd = f"{PY} -m pytest -q -p no:cacheprovider tests/test_app.py"
    actions = [
        act("write_file", "Test anlegen", path="tests/test_app.py", content=TEST_FILE),
        act("run_test", "Test (rot)", command=cmd),
        act("run_test", "nochmal", command=cmd),
        act("read_file", path="app.py"),
        act("write_file", "gleicher Inhalt", path="tests/test_app.py", content=TEST_FILE),
        act("write_file", "Fix", path="app.py", content='"""Adds."""\n\n\ndef add(a, b):\n    return a + b\n'),
        act("read_file", path="app.py"),
    ]
    h, loop, chat = await _setup(sessionmaker, tmp_repo, actions, max_turns=7)
    await _run(h, loop)

    def prompt(turn: int) -> str:
        return "\n".join(m.content for m in chat.prompts[turn - 1])

    assert "- run_test(" in prompt(3), "a first repeat is still offered (the guard refuses it)"
    for turn in (4, 5, 6):  # neither a read (turn 4) nor a write of identical content (turn 5, BUG-182) changes anything
        assert "withheld until something changes" in prompt(turn)
        assert chat.prompts[turn - 1][0].content == chat.prompts[0][0].content, "the system message stays the same"
    assert "- run_test(" in prompt(7) and "withheld" not in prompt(7), "the edit in turn 6 brings the checks back"


async def test_reads_are_withheld_after_a_refused_re_read_until_a_file_changes(sessionmaker: Any, tmp_repo: Path) -> None:
    """Live G1 (BUG-271): after refused re-reads the coder asked for the same lines again across three attempts of
    S014 without one edit until stagnation blocked the step and the job failed. read_file/read_range leave the tool
    catalogue after a refused re-read until a file changes – now by the turn notice, the catalogue stays (D-026)."""
    actions = [
        act("read_file", path="app.py"),
        act("read_file", "nochmal", path="app.py"),
        act("list_files", path="."),
        act("write_file", "Fix", path="app.py", content='"""Adds."""\n\n\ndef add(a, b):\n    return a + b\n'),
        act("list_files", path="."),
    ]
    h, loop, chat = await _setup(sessionmaker, tmp_repo, actions, max_turns=5)
    res = await _run(h, loop)
    assert res.history[1].error_code == "READ_REPEATED"

    def prompt(turn: int) -> str:
        return "\n".join(m.content for m in chat.prompts[turn - 1])

    assert "- read_file(" in prompt(2), "a first re-read is still offered (the guard refuses it)"
    for turn in (3, 4):
        assert "read_file and read_range are withheld until a file changes" in prompt(turn)
        assert chat.prompts[turn - 1][0].content == chat.prompts[0][0].content, "the system message stays the same"
        assert "- run_test(" in prompt(turn) and "- write_file(" in prompt(turn)
    assert "- read_file(" in prompt(5) and "withheld" not in prompt(5), "the edit in turn 4 brings the reads back"


async def test_a_passing_check_proposed_again_after_the_refusal_completes_the_step(sessionmaker: Any, tmp_repo: Path) -> None:
    """rc21 battery J5: the tests were green, the repeat guard told the coder to call complete_step, and it proposed
    the same command three more times. The second re-proposal completes the step on its behalf (BUG-207); a failing
    check is never completed this way."""
    fixed = '"""Adds."""\n\n\ndef add(*numbers):\n    return sum(numbers)\n'
    cmd = f"{PY} -m pytest -q -p no:cacheprovider tests/test_app.py"
    actions = [
        act("write_file", "Fix", path="app.py", content=fixed),
        act("write_file", "Test", path="tests/test_app.py", content=TEST_FILE),
        act("run_test", "Test (grün)", command=cmd),
        act("run_test", "nochmal", command=cmd),
        act("run_test", "und nochmal", command=cmd),
        act("read_file", path="app.py"),
    ]
    h, loop, chat = await _setup(sessionmaker, tmp_repo, actions, max_turns=6)
    res = await _run(h, loop)
    assert res.outcome == "completed" and res.turns == 5 and len(chat.prompts) == 5
    assert [r.tool for r in res.history][-3:] == ["run_test", "run_test", "complete_step"]
    assert "Completed by the runtime" in str(res.completion)
    # red check proposed again: never completed by the runtime
    broken = '"""Adds."""\n\n\ndef add(a, b):\n    return a + b\n'
    actions = [
        act("write_file", "kaputt", path="app.py", content=broken),
        act("write_file", "Test", path="tests/test_app.py", content=TEST_FILE),
        act("run_test", "rot", command=cmd),
        act("run_test", "nochmal", command=cmd),
        act("run_test", "und nochmal", command=cmd),
    ]
    h2, loop2, _ = await _setup(sessionmaker, tmp_repo, actions, max_turns=5)
    res2 = await _run(h2, loop2)
    assert res2.outcome != "completed" and "complete_step" not in [r.tool for r in res2.history]


class HangingCoder(ScriptedCoder):
    """A model call that never answers on its own (a long generation with repairs)."""

    def __init__(self) -> None:
        super().__init__([])
        self.interrupted = False

    async def structured(self, alias: str, messages: list[ChatMessage], schema: type[T], **_: Any) -> StructuredResult[T]:
        self.prompts.append(list(messages))
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.interrupted = True
            raise
        raise AssertionError("unreachable")


async def test_a_cancel_interrupts_the_running_model_call(sessionmaker: Any, tmp_repo: Path) -> None:
    """Live J6 on 0.1.0rc14: the cancel waited until the coder's model call with its repairs ended (BUG-184)."""
    h, loop, _ = await _setup(sessionmaker, tmp_repo, [])
    hanging = HangingCoder()
    loop.chat = hanging
    token = CancelToken()
    run = asyncio.create_task(_run(h, loop, token))
    while not hanging.prompts:
        await asyncio.sleep(0.01)
    token.cancel("cancelled via API")
    res = await asyncio.wait_for(run, timeout=5)
    assert res.outcome == "cancelled" and res.detail == "cancelled via API" and hanging.interrupted


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


SHARED_STATE_TESTS = (
    "ITEMS = []\n\n\ndef test_first():\n    ITEMS.append(1)\n    assert len(ITEMS) == 1\n\n\n"
    "def test_second():\n    ITEMS.append(2)\n    assert len(ITEMS) == 1\n\n\ndef test_wrong():\n    assert 1 == 2\n"
)


async def test_a_test_that_passes_alone_is_reported_as_shared_state(sessionmaker: Any, tmp_repo: Path) -> None:
    """Live cycle 2 M1 on 0.1.0rc60: two HTTP tests found four tasks instead of two because all tests used one SQLite
    file; each passed alone, the coder never found out and the job failed after three plan versions. Once the same
    tests keep failing, the runtime runs the first failing test alone and names shared state when it passes (BUG-291)."""
    cmd = f"{PY} -m pytest -q -p no:cacheprovider tests/test_app.py"
    hook = ScriptedStagnation({2: StagnationDirective("warning", message="Stagnation warning: same failing tests 2x.")})
    actions = [
        act("write_file", "Tests anlegen", path="tests/test_app.py", content=SHARED_STATE_TESTS),
        act("run_test", "Tests (rot)", command=cmd),
        act("read_file", path="tests/test_app.py"),
    ]
    h, loop, chat = await _setup(sessionmaker, tmp_repo, actions, max_turns=3, stagnation=hook)
    await _run(h, loop)
    third = "\n".join(m.content for m in chat.prompts[2])
    assert "Stagnation warning" in third
    # the first failing test is probed: test_second passes alone, test_wrong would fail alone too
    assert "Isolation check by the runtime: 'tests/test_app.py::test_second' passes when it runs alone" in third
    assert "-q -p no:cacheprovider tests/test_app.py::test_second" in third
    async with sessionmaker() as s:
        runs = (
            (
                await s.execute(
                    select(Event).where(Event.job_id == h.step.job_id, Event.event_type == "test.started").order_by(Event.sequence)
                )
            )
            .scalars()
            .all()
        )
    assert [r.payload["command"].endswith("tests/test_app.py::test_second") for r in runs] == [False, True]


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


async def test_cut_off_answer_asks_for_writing_in_parts_and_gets_one_extra_turn(sessionmaker: Any, tmp_repo: Path) -> None:
    """BUG-223: an oversized write_file was cut off at the output limit; the model has to write the file in parts."""
    cut = ModelOutputTruncated("hit the output limit", code="MODEL_OUTPUT_TRUNCATED")
    h, loop, chat = await _setup(sessionmaker, tmp_repo, [cut, cut, cut])
    res = await _run(h, loop)
    assert res.outcome == "model_failed" and res.turns == 3 and res.error_code == "MODEL_OUTPUT_TRUNCATED"
    note = [m.content for m in chat.prompts[1] if "cut off" in m.content]
    assert note and '"append": true' in note[0] and "250 lines" in note[0]

    h2, loop2, _ = await _setup(sessionmaker, tmp_repo, [cut, ModelOutputInvalid("bad", code="MODEL_OUTPUT_INVALID")])
    assert (await _run(h2, loop2)).turns == 2  # a plain invalid answer keeps the normal limit


async def test_a_cut_off_write_file_keeps_its_complete_lines(sessionmaker: Any, tmp_repo: Path) -> None:
    """Live G1 on rc39 (BUG-252): the coder wrote a whole module in one answer, was cut off at 8192 tokens after 5.5
    minutes and lost all of it, twice. A cut-off write_file is salvaged: its complete lines are written and the model
    continues with append after the shown last line."""
    import json as _json

    body = "".join(f"def f{i}():\n    return {i}\n" for i in range(20))  # 40 lines, then "def f20():" completes the 41st
    partial = _json.dumps({"tool": "write_file", "args": {"path": "app.py", "content": body + "def f20():\n    ret"}})[:-4]
    cut = ModelOutputTruncated("hit the output limit", code="MODEL_OUTPUT_TRUNCATED")
    cut.partial = partial
    rest = act("write_file", path="app.py", content="    return 20\n", append=True)  # continues after the shown tail
    done = act("complete_step", summary="module written in parts", changed_files=["app.py"])
    h, loop, chat = await _setup(sessionmaker, tmp_repo, [cut, rest, done])
    res = await _run(h, loop)
    assert res.outcome == "completed", res
    assert (tmp_repo / "app.py").read_text() == body + "def f20():\n    return 20\n"
    note = "\n".join(m.content for m in chat.prompts[1])
    assert "first 41 complete lines of app.py were written" in note and "    return 19\ndef f20():" in note and '"append": true' in note
    assert res.history[0].tool == "write_file" and res.history[0].ok
    # too little to salvage, or no write_file: the old handling (cut-off note, no file change)
    tiny = ModelOutputTruncated("hit the output limit", code="MODEL_OUTPUT_TRUNCATED")
    tiny.partial = '{"tool": "write_file", "args": {"path": "app.py", "content": "x = 1\\ny'
    before = (tmp_repo / "app.py").read_text()
    h2, loop2, chat2 = await _setup(sessionmaker, tmp_repo, [tiny, tiny, tiny])
    assert (await _run(h2, loop2)).error_code == "MODEL_OUTPUT_TRUNCATED"
    assert (tmp_repo / "app.py").read_text() == before


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
