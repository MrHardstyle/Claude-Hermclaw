"""Subprocess execution for the backup tools (pg_dump, pg_restore, pg_dumpall, rsync).

* explicit, minimal environment: the parent's environment is NOT inherited (no stray ``PGPASSWORD``/tokens);
  credentials reach the tools via ``PGPASSFILE`` (a 0600 file in a private temp dir), never via argv;
* own process group, killed on timeout or cancellation;
* captured output is truncated and redacted before it is logged or put into errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR

log = get_logger(__name__)
MAX_CAPTURE = 256 * 1024
_BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR")


@dataclass(frozen=True)
class ProcResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def error_text(self, limit: int = 600) -> str:
        """First lines of stderr (or stdout), redacted – safe for logs, events and exceptions."""
        text = (self.stderr or self.stdout).strip()
        if self.timed_out:
            text = f"timed out; {text}"
        return DEFAULT_REDACTOR.text(text[:limit])


def base_env(extra: Mapping[str, str] | None = None, *, home: Path | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _BASE_ENV_KEYS if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env["LC_ALL"] = "C.UTF-8"  # stable, parseable tool output
    if home is not None:
        env["HOME"] = str(home)
    elif "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]
    if extra:
        env.update(extra)
    return env


async def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    limit_seconds: float,
    cwd: Path | None = None,
    stdin_devnull: bool = True,
) -> ProcResult:
    """Run ``argv`` (no shell) for at most ``limit_seconds``.

    Never raises for a non-zero exit; raises ``FileNotFoundError`` for a missing binary.
    """
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL if stdin_devnull else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
        cwd=str(cwd) if cwd else None,
        start_new_session=True,
    )
    timed_out = False
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=limit_seconds)
    except TimeoutError:
        timed_out = True
        _kill_group(proc)
        out_b, err_b = await proc.communicate()
    except BaseException:  # cancellation: never leave a dump/rsync running behind
        _kill_group(proc)
        with contextlib.suppress(Exception):
            await asyncio.shield(proc.wait())
        raise
    duration = int((time.monotonic() - started) * 1000)
    result = ProcResult(
        argv=tuple(argv),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out_b[:MAX_CAPTURE].decode("utf-8", "replace"),
        stderr=err_b[:MAX_CAPTURE].decode("utf-8", "replace"),
        duration_ms=duration,
        timed_out=timed_out,
    )
    log.debug(
        "backup tool finished",
        extra={"tool": Path(argv[0]).name, "returncode": result.returncode, "duration_ms": duration, "timed_out": timed_out},
    )
    return result


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
