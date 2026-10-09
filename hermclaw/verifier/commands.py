"""Running verifier commands through the sandbox :class:`~hermclaw.core.interfaces.CommandExecutor`.

Every command (syntax batches, compile, lint, tests, command evidence) goes through the executor – never through a
local shell. Results are bounded and redacted; each run is remembered for a ``command_runs`` row (and test evidence
for a ``test_runs`` row) which the verifier persists together with the verification run.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from hermclaw.contracts.worker import CommandResult
from hermclaw.core.interfaces import CommandExecutor, ExecutionRequest, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.tools.output import excerpt, strip_ansi
from hermclaw.verifier.text import pg_safe

log = get_logger(__name__)

EXECUTOR_GRACE_SECONDS = 60
COMMAND_ENV = {"CI": "1", "PYTHONDONTWRITEBYTECODE": "1"}


@dataclass
class CommandOutcome:
    command: str
    purpose: str
    network: bool
    timeout_seconds: int
    result: CommandResult | None = None
    error: str | None = None
    duration_ms: int = 0

    @property
    def exit_code(self) -> int | None:
        return self.result.exit_code if self.result is not None else None

    @property
    def timed_out(self) -> bool:
        return bool(self.result is not None and self.result.timed_out)

    @property
    def failed_to_run(self) -> bool:
        """The executor could not run the command at all (as opposed to the command failing)."""
        return self.result is None or (self.result.error is not None and self.result.exit_code is None and not self.result.timed_out)

    @property
    def stdout(self) -> str:
        return strip_ansi(self.result.stdout) if self.result is not None else ""

    @property
    def stderr(self) -> str:
        return strip_ansi(self.result.stderr) if self.result is not None else ""

    @property
    def output(self) -> str:
        parts = [p for p in (self.stdout, self.stderr) if p]
        return "\n".join(parts)

    @property
    def problem(self) -> str:
        if self.error:
            return self.error
        if self.result is not None and self.result.error:
            return self.result.error
        return ""


@dataclass
class CommandRecord:
    command: str
    purpose: str
    exit_code: int | None
    timed_out: bool
    network: bool
    stdout_excerpt: str
    stderr_excerpt: str
    duration_ms: int


@dataclass
class TestRecord:
    command: str
    framework: str
    status: str
    passed: int
    failed: int
    errors: int
    skipped: int
    output_excerpt: str
    duration_ms: int

    __test__ = False


@dataclass
class CommandRunner:
    executor: CommandExecutor
    workspace: WorkspaceHandle
    job_id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID | None
    before_first: Callable[[], Awaitable[None]] | None = None
    redactor: Redactor = DEFAULT_REDACTOR
    excerpt_chars: int = 8000
    records: list[CommandRecord] = field(default_factory=list)
    tests: list[TestRecord] = field(default_factory=list)
    _started: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def used(self) -> bool:
        return self._started

    async def run(self, command: str, *, purpose: str, timeout_seconds: int, network: bool = False) -> CommandOutcome:
        async with self._lock:  # one command at a time per workspace: side-effect tracking stays sound
            if not self._started:
                self._started = True
                if self.before_first is not None:
                    await self.before_first()
            return await self._run(command, purpose=purpose, timeout_seconds=timeout_seconds, network=network)

    async def _run(self, command: str, *, purpose: str, timeout_seconds: int, network: bool) -> CommandOutcome:
        req = ExecutionRequest(
            command=command,
            timeout_seconds=timeout_seconds,
            network=network,
            env=dict(COMMAND_ENV),
            job_id=self.job_id,
            step_id=self.step_id,
            attempt_id=self.attempt_id,
            purpose=purpose,
        )
        outcome = CommandOutcome(command=command, purpose=purpose, network=network, timeout_seconds=timeout_seconds)
        started = time.monotonic()
        try:
            outcome.result = await asyncio.wait_for(self.executor.run(self.workspace, req), timeout_seconds + EXECUTOR_GRACE_SECONDS)
        except TimeoutError:
            outcome.error = f"executor did not answer within {timeout_seconds + EXECUTOR_GRACE_SECONDS}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome.error = self.redactor.text(f"executor failed: {type(exc).__name__}: {exc}")[:2000]
            log.warning("verifier command could not be executed", extra={"purpose": purpose, "error": outcome.error})
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        if outcome.result is not None and outcome.result.duration_ms:
            outcome.duration_ms = outcome.result.duration_ms
        self.records.append(
            CommandRecord(
                command=pg_safe(self.redactor.text(command)),
                purpose=purpose,
                exit_code=outcome.exit_code,
                timed_out=outcome.timed_out,
                network=network,
                stdout_excerpt=self.clip(outcome.stdout or ""),
                stderr_excerpt=self.clip(outcome.stderr or outcome.problem),
                duration_ms=outcome.duration_ms,
            )
        )
        return outcome

    def clip(self, text: str, limit: int | None = None) -> str:
        return pg_safe(excerpt(self.redactor.text(text), limit or self.excerpt_chars))
