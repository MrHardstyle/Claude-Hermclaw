"""Shared support for full-stack runtime tests (composition root, scheduler process, recovery).

``StackModels`` scripts the model roles by alias – Gemma (``planner-gemma``, chat), fast router (``fast-router``),
Qwen3-Coder (``coder-main``), Qwen3.8 heavy reviewer (``heavy-review``) – so the *real* components run end to end:
GitEngine, ScopeEngine, ToolEngine, ContextBuilder, VerifierAdapter, WorkspaceReviewer, RepoIntelligence (lexical),
ResourceManager, Scheduler, RuntimeJobDriver. The coder answers from the state of its prompt (diff present or not),
so a resumed or restarted step continues correctly instead of replaying a fixed list.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult
from tests.unit.test_planner_support import ScriptedChat, plan, step

T = TypeVar("T", bound=BaseModel)


def act(tool: str, **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args, "status": tool, "decision": tool}


EDIT = act("replace_text", path="src/module.py", old="VALUE = 1", new="VALUE = 2")
DONE = act("complete_step", summary="VALUE set to 2", changed_files=["src/module.py"], tests_run=[])


def checked_plan() -> dict[str, Any]:
    """implement → test (deterministic acceptance) → review (whole change, heavy reviewer)."""
    return plan(
        "Set VALUE to 2 and check it",
        [
            step(
                "S001",
                "implement",
                "coding",
                "Change VALUE in src/module.py from 1 to 2.",
                repo_hints=["src/module.py"],
                acceptance=[{"type": "presence", "path_glob": "src/module.py", "pattern": "VALUE = 2"}],
            ),
            step(
                "S002",
                "test",
                "testing",
                "Check that VALUE is 2 and the old value is gone.",
                depends_on=["S001"],
                acceptance=[
                    {"type": "presence", "path_glob": "src/module.py", "pattern": "VALUE = 2"},
                    {"type": "absence", "path_glob": "src/*.py", "pattern": "VALUE = 1", "expected_matches": 0},
                ],
            ),
            step("S003", "review", "review", "Review the whole change.", depends_on=["S002"]),
        ],
    )


class StackModels(ScriptedChat):
    """Gemma plans from ``plans`` (chat); the other roles answer structurally by alias.

    ``hang_marker``: when set, the process blocks forever at ``hang_on`` after writing this file – the moment a
    recovery test kills it: ``coder`` = the coder's *second* call (the edit of the first call is already in the
    workspace), ``plan`` = Gemma's planning call (job in the preparation phase)."""

    def __init__(self, plans: list[Any] | None = None, *, hang_marker: Path | None = None, hang_on: str = "coder") -> None:
        super().__init__(list(plans or []))
        self.hang_marker = hang_marker
        self.hang_on = hang_on  # "coder" (2nd coder call) or "plan" (Gemma planning call)
        self.coder_calls = 0
        self.review_calls = 0
        self.aliases: list[str] = []

    async def structured(  # type: ignore[override]
        self, alias: str, messages: list[ChatMessage], schema: type[T], *, ctx: CallContext, **_: Any
    ) -> StructuredResult[T]:
        self.aliases.append(alias)
        if alias == "fast-router":
            return self._ok(schema, {"intent": "code_change"}, alias, "qwen3:8b")
        if alias == "heavy-review":
            self.review_calls += 1
            return self._ok(schema, {"verdict": "pass", "summary": "change is minimal and correct", "findings": []}, alias, "qwen3.8:27b")
        assert alias == "coder-main", alias
        self.coder_calls += 1
        if self.hang_marker is not None and self.hang_on == "coder" and self.coder_calls >= 2:
            self.hang_marker.write_text(str(os.getpid()), encoding="utf-8")
            await asyncio.Event().wait()  # killed here
        prompt = "\n".join(m.content for m in messages)
        edited = "+VALUE = 2" in prompt  # CURRENT DIFF of the step (also after a restart: the edit is uncommitted)
        return self._ok(schema, DONE if edited else EDIT, alias, "qwen3-coder:30b")

    async def chat(self, alias: str, messages: list[ChatMessage], *, ctx: CallContext, **kw: Any) -> ChatResult:  # type: ignore[override]
        if self.hang_marker is not None and self.hang_on == "plan":
            self.hang_marker.write_text(str(os.getpid()), encoding="utf-8")
            await asyncio.Event().wait()  # killed while Gemma plans
        return await super().chat(alias, messages, ctx=ctx, **kw)

    @staticmethod
    def _ok(schema: type[T], data: dict[str, Any], alias: str, model: str) -> StructuredResult[T]:
        return StructuredResult(value=schema.model_validate(data), result=ChatResult(content=json.dumps(data), alias=alias, model=model))


__all__ = ["DONE", "EDIT", "StackModels", "act", "checked_plan"]
