"""Shared test support for the planner/replanner tests (P14.10, P24).

* ``ScriptedChat`` – a fake ``ChatModel`` that returns scripted answers in order and records every call. It is a
  test double only; production code talks to the LiteLLM gateway.
* ``REPOS`` – planner inputs + a valid model plan for five unrelated repositories / tasks: a Python FastAPI
  service, a PHP application, a React/TypeScript app, a YAML-only configuration repository and a Linux
  administration task without repository.

The few tests at the bottom keep the fixtures honest (every scripted plan is schema-valid).
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.plan import PlanContract
from hermclaw.models.protocols import CallContext, ChatMessage, ChatResult, StructuredResult
from hermclaw.persistence.models import Job, JobInput
from hermclaw.planner.inputs import ContextSnippet, PlannerInput

T = TypeVar("T", bound=BaseModel)
Hook = Callable[[], Awaitable[None]]


@dataclass
class Answer:
    """One scripted model answer."""

    content: str | dict[str, Any]
    fallback_used: bool = False
    reasoning_chars: int = 0
    before: Hook | None = None  # awaited before answering (simulates concurrent activity during a model call)
    finish_reason: str = "stop"


@dataclass
class RecordedCall:
    alias: str
    messages: list[ChatMessage]
    ctx: CallContext
    json_schema: dict[str, Any] | None
    max_tokens: int | None
    temperature: float | None
    timeout_seconds: float | None


@dataclass
class ScriptedChat:
    answers: list[Answer | Exception | str | dict[str, Any]]
    fallback_alias: str = "planner-gemma-fallback"
    calls: list[RecordedCall] = field(default_factory=list)

    async def chat(
        self,
        alias: str,
        messages: list[ChatMessage],
        *,
        ctx: CallContext,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> ChatResult:
        self.calls.append(
            RecordedCall(
                alias=alias,
                messages=[ChatMessage(role=m.role, content=m.content) for m in messages],
                ctx=ctx,
                json_schema=json_schema,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout_seconds=timeout_seconds,
            )
        )
        if not self.answers:
            raise AssertionError("ScriptedChat: no scripted answer left")
        item = self.answers.pop(0)
        if isinstance(item, Exception):
            raise item
        answer = item if isinstance(item, Answer) else Answer(content=item)
        if answer.before is not None:
            await answer.before()
        content = answer.content if isinstance(answer.content, str) else json.dumps(answer.content)
        used_alias = self.fallback_alias if answer.fallback_used else alias
        return ChatResult(
            content=content,
            alias=used_alias,
            model="gemma4:12b" if answer.fallback_used else "gemma4:26b",
            prompt_tokens=100,
            completion_tokens=50,
            latency_ms=5,
            finish_reason=answer.finish_reason,
            reasoning_chars=answer.reasoning_chars,
            invocation_id=uuid.uuid4(),
            fallback_used=answer.fallback_used,
        )

    async def structured(
        self,
        alias: str,
        messages: list[ChatMessage],
        schema: type[T],
        *,
        ctx: CallContext,
        max_repairs: int = 2,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
    ) -> StructuredResult[T]:  # pragma: no cover - the planner uses chat() + its own validation loop
        raise NotImplementedError

    def user_payload(self, index: int = 0) -> dict[str, Any]:
        """The JSON planner input of call ``index`` (the first user message)."""
        user = next(m for m in self.calls[index].messages if m.role == "user")
        value: dict[str, Any] = json.loads(user.content)
        return value


# --------------------------------------------------------------------------------------------- plan helpers
def step(sid: str, kind: str, capability: str, goal: str, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"id": sid, "title": extra.pop("title", goal[:60]), "kind": kind, "capability": capability, "goal": goal}
    data.update(extra)
    return data


def plan(goal: str, steps: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"goal": goal, "summary": extra.pop("summary", "plan"), "steps": steps, **extra}


@dataclass
class RepoFixture:
    name: str
    title: str
    goal: str
    inputs: PlannerInput
    plan: dict[str, Any]
    constraints: list[str] = field(default_factory=list)

    def answer(self) -> dict[str, Any]:
        return copy.deepcopy(self.plan)


FASTAPI = RepoFixture(
    name="python-fastapi",
    title="Paginate users endpoint",
    goal="Add limit/offset pagination to the GET /users endpoint of the FastAPI service and document it.",
    inputs=PlannerInput(
        repository_inventory={
            "languages": ["python"],
            "frameworks": ["fastapi"],
            "files": ["app/main.py", "app/routers/users.py", "app/models.py", "tests/test_users.py", "pyproject.toml", "README.md"],
            "test_command": "pytest -q",
        },
        retrieved_context=[
            ContextSnippet(
                path="app/routers/users.py",
                start_line=1,
                end_line=6,
                score=0.92,
                snippet='router = APIRouter()\n\n@router.get("/users")\ndef list_users(db: Session = Depends(get_db)):\n    return db.query(User).all()\n',
            ),
            ContextSnippet(
                path="app/models.py", start_line=1, end_line=3, score=0.5, snippet="class User(Base):\n    id = Column(Integer)\n"
            ),
        ],
        existing_tests=["tests/test_users.py"],
    ),
    plan=plan(
        "Paginate GET /users",
        [
            step(
                "S001",
                "implement",
                "coding",
                "Add limit and offset query parameters to list_users and apply them to the query.",
                repo_hints=["app/routers/users.py", "list_users"],
                acceptance=[
                    {"type": "presence", "path_glob": "app/routers/users.py", "pattern": "offset"},
                    {"type": "test", "command": "pytest -q", "framework": "pytest"},
                ],
            ),
            step(
                "S002",
                "documentation",
                "documentation",
                "Document the new pagination parameters in the README.",
                depends_on=["S001"],
                repo_hints=["README.md"],
            ),
            step("S003", "review", "review", "Review the pagination change and its documentation.", depends_on=["S001", "S002"]),
        ],
    ),
)

PHP = RepoFixture(
    name="php-app",
    title="Login rate limit",
    goal="Rate-limit failed logins in AuthService (lock the account for 15 minutes after 5 failures).",
    inputs=PlannerInput(
        repository_inventory={
            "languages": ["php"],
            "files": [
                "public/index.php",
                "src/Controller/LoginController.php",
                "src/Service/AuthService.php",
                "tests/AuthServiceTest.php",
                "composer.json",
                "phpunit.xml",
            ],
            "test_commands": ["vendor/bin/phpunit"],
        },
        retrieved_context=[
            ContextSnippet(
                path="src/Service/AuthService.php",
                start_line=10,
                end_line=14,
                score=0.88,
                snippet="final class AuthService\n{\n    public function login(string $user, string $password): bool\n    {\n",
            )
        ],
        existing_tests=["tests/AuthServiceTest.php"],
    ),
    plan=plan(
        "Rate-limit failed logins",
        [
            step(
                "S001",
                "implement",
                "coding",
                "Count failed logins per user in AuthService::login and lock the account after five failures.",
                repo_hints=["src/Service/AuthService.php", "AuthService::login"],
            ),
            step("S002", "test", "testing", "Run the PHPUnit suite against the rate-limit change.", depends_on=["S001"]),
        ],
        research_needed=[{"question": "What lockout window does OWASP recommend after repeated failed logins?", "reason": "policy"}],
    ),
)

REACT = RepoFixture(
    name="react-ts",
    title="Todo filter",
    goal="Add an all/done/open filter toggle to the TodoList component.",
    inputs=PlannerInput(
        repository_inventory={
            "languages": ["typescript"],
            "frameworks": ["react", "vite"],
            "files": ["package.json", "src/App.tsx", "src/components/TodoList.tsx", "src/components/TodoList.test.tsx", "vite.config.ts"],
            "tests": {"command": "npm test -- --run"},
        },
        retrieved_context=[
            ContextSnippet(
                path="src/components/TodoList.tsx",
                start_line=1,
                end_line=4,
                score=0.9,
                snippet="export function TodoList({ items }: Props) {\n  return <ul>{items.map(renderItem)}</ul>;\n}\n",
            )
        ],
        existing_tests=["src/components/TodoList.test.tsx"],
    ),
    plan=plan(
        "Filter toggle for TodoList",
        [
            step("S001", "discover", "discover", "Locate how TodoList receives and renders its items.", repo_hints=["src/components/"]),
            step(
                "S002",
                "implement",
                "coding",
                "Add a FilterToggle component and use it in TodoList to filter items by state.",
                depends_on=["S001"],
                repo_hints=["src/components/TodoList.tsx", "TodoList"],
                allowed_new_paths=["src/components/FilterToggle.tsx"],
                acceptance=[{"type": "presence", "path_glob": "src/components/FilterToggle.tsx"}],
            ),
            step("S003", "verify", "testing", "Verify the filter toggle with the vitest suite.", depends_on=["S002"]),
        ],
    ),
)

YAML_CONFIG = RepoFixture(
    name="yaml-config",
    title="Tune worker pool",
    goal="Raise the worker pool size to 8 in config/app.yaml and enable JSON logging in config/logging.yaml.",
    inputs=PlannerInput(
        repository_inventory={
            "languages": ["yaml"],
            "files": ["config/app.yaml", "config/logging.yaml", "deploy/values-prod.yaml", "README.md"],
        },
        retrieved_context=[
            ContextSnippet(path="config/app.yaml", start_line=1, end_line=3, score=0.8, snippet="workers:\n  pool_size: 4\n"),
        ],
    ),
    plan=plan(
        "Tune worker pool and logging",
        [
            step(
                "S001",
                "implement",
                "coding",
                "Set workers.pool_size to 8 and switch the log formatter to json.",
                repo_hints=["config/app.yaml", "config/logging.yaml"],
                acceptance=[
                    {"type": "schema", "path": "config/app.yaml", "format": "yaml"},
                    {"type": "presence", "path_glob": "config/app.yaml", "pattern": "pool_size: 8"},
                ],
            ),
            step("S002", "review", "review", "Review the configuration change for unintended effects.", depends_on=["S001"]),
        ],
    ),
)

LINUX_ADMIN = RepoFixture(
    name="linux-admin",
    title="Nginx log rotation",
    goal="On host web-01 rotate the nginx logs daily and keep 14 days (/etc/logrotate.d/nginx).",
    constraints=["do not restart nginx during business hours"],
    inputs=PlannerInput(capabilities=["ssh", "research", "review"]),
    plan=plan(
        "Daily nginx log rotation on web-01",
        [
            step(
                "S001",
                "ssh",
                "ssh",
                "Configure /etc/logrotate.d/nginx on web-01 for daily rotation with 14 kept files.",
                constraints=["dry-run logrotate before writing"],
                acceptance=[
                    {
                        "type": "command",
                        "command": "logrotate -d /etc/logrotate.d/nginx",
                        "expect_exit_code": 0,
                        "stdout_pattern": "rotate 14",
                    }
                ],
            ),
            step("S002", "review", "review", "Review the logrotate change on web-01.", depends_on=["S001"]),
        ],
        research_needed=[
            {"question": "Which logrotate directives does Debian 13 ship for nginx by default?", "reason": "avoid duplicates"}
        ],
    ),
)

REPOS: tuple[RepoFixture, ...] = (FASTAPI, PHP, REACT, YAML_CONFIG, LINUX_ADMIN)


async def create_job(
    sm: async_sessionmaker[AsyncSession], fixture: RepoFixture, *, status: str = "planning", replan_count: int = 0
) -> uuid.UUID:
    async with sm() as s:
        job = Job(title=fixture.title, prompt=fixture.goal, status=status, replan_count=replan_count, priority=60)
        s.add(job)
        await s.flush()
        for c in fixture.constraints:
            s.add(JobInput(job_id=job.id, kind="constraint", content=c))
        await s.commit()
        return job.id


# --------------------------------------------------------------------------------------------- fixture sanity
def test_fixture_plans_are_schema_valid() -> None:
    for fixture in REPOS:
        PlanContract.model_validate(fixture.answer())


def test_fixture_repositories_are_unrelated() -> None:
    names = [f.name for f in REPOS]
    assert len(set(names)) == 5
    languages = {tuple(f.inputs.repository_inventory.get("languages", [])) for f in REPOS}
    assert len(languages) == 5  # python, php, typescript, yaml, none (admin task)


async def test_scripted_chat_records_calls_and_raises_when_exhausted() -> None:
    chat = ScriptedChat([Answer(content={"a": 1}, fallback_used=True)])
    res = await chat.chat("planner-gemma", [ChatMessage(role="user", content="{}")], ctx=CallContext(purpose="planner"))
    assert res.fallback_used and res.alias == "planner-gemma-fallback"
    assert json.loads(res.content) == {"a": 1}
    try:
        await chat.chat("planner-gemma", [], ctx=CallContext(purpose="planner"))
    except AssertionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected exhaustion")
    assert len(chat.calls) == 2
