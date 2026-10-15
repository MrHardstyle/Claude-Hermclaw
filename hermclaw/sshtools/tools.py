"""Runtime SSH/admin tools ``ssh_read`` / ``ssh_command`` / ``ssh_upload`` / ``ssh_service_status`` (P26 26.4–26.8).

Every call passes the same gates, in this order:

1. capability (26.8): only steps with capability ``ssh``/``deploy`` (:func:`assert_ssh_allowed`);
2. host allow-list (26.1): :meth:`HostRegistry.get` – unknown / not allow-listed hosts are refused;
3. command policy (26.3): ``read`` | ``mutate`` | ``destructive`` | ``forbidden``; ``ssh_read`` only runs ``read``;
   ``forbidden`` and un-allow-listed chaining are refused; ``destructive`` (and ``mutate`` when
   ``policies.ssh.require_approval_for_mutations``) returns ``approval_required`` with a token;
4. pre-flight (26.2): pinned host key in ``known_hosts`` and a usable key – before an approval is used up;
5. approval (26.5): a call that carries an approved token consumes it (single use, row lock on the job) in the
   same transaction that writes the ``command_runs`` row (``exit_code`` NULL = started), so a mutation never runs
   without an audit record;
6. execution (26.7): :class:`SshRunner` with timeout + kill, output caps and redaction;
7. audit (26.6): the ``command_runs`` row is completed (exit code, excerpts, duration) and an
   ``ssh.command`` event is written in one transaction.

Refusals (capability, host, policy, approval) do not create ``command_runs`` rows – nothing ran – but always
an ``ssh.command`` event (``outcome`` ``denied`` / ``approval_required``), so the audit trail shows what was tried.
Commands and outputs are redacted before they are stored, returned or logged; upload contents never are.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import posixpath
import re
import shlex
import stat
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.common import Severity
from hermclaw.contracts.events import EventType
from hermclaw.core.config import HermclawConfig
from hermclaw.core.errors import HermclawError
from hermclaw.core.logging import get_logger
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.events.store import append_event
from hermclaw.persistence.models import CommandRun, Job
from hermclaw.security.secrets import SecretStore
from hermclaw.sshtools.approvals import CONSUMED_KEY, consume_approval
from hermclaw.sshtools.client import MAX_TIMEOUT_SECONDS, SshResult, SshRunner
from hermclaw.sshtools.errors import (
    SSH_APPROVAL_INVALID,
    SSH_APPROVAL_PENDING,
    SSH_APPROVAL_REQUIRED,
    SSH_COMMAND_FAILED,
    SSH_COMMAND_INVALID,
    SSH_SERVICE_UNAVAILABLE,
    SSH_TIMEOUT,
    SSH_TRANSPORT_ERROR,
    SSH_TRANSPORT_EXIT,
    SSH_UNIT_INVALID,
    SSH_UPLOAD_INVALID,
    SshConfigError,
    SshPolicyError,
    SshTransportError,
)
from hermclaw.sshtools.keys import KeyManager
from hermclaw.sshtools.policy import APPROVAL_TOKEN_RE, SshClassification, SshCommandClass, SshCommandPolicy, SshDecision, Verdict
from hermclaw.sshtools.registry import HOST_ID_RE, HostRegistry, SshHost
from hermclaw.sshtools.separation import assert_ssh_allowed
from hermclaw.tools.output import clip_middle, excerpt, strip_ansi

log = get_logger(__name__)

SOURCE_TYPE = "ssh"
DEFAULT_EXCERPT_CHARS = 4000
DEFAULT_MAX_UPLOAD_BYTES = 16 * 1024 * 1024
_EVENT_COMMAND_CHARS = 500
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._@+-][A-Za-z0-9._/@+-]{0,998}$")
_MODE_RE = re.compile(r"^0?[0-7]{3}$")  # no setuid/setgid/sticky bits through uploads
#: replacing these changes who may log in or escalate, or disables the host-side gate: an upload there is
#: ``destructive`` (always needs an operator approval, even when mutations do not)
CRITICAL_REMOTE_FILES = frozenset(
    {
        "/etc/sudoers", "/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow", "/etc/subuid", "/etc/subgid",
        "/etc/fstab", "/etc/crontab", "/etc/environment", "/etc/ld.so.preload", "/etc/ld.so.conf", "/etc/profile",
        "/etc/bash.bashrc", "/usr/local/bin/hermclaw-ssh-gate", "/etc/hermclaw-ssh-allowlist",
    }
)  # fmt: skip
CRITICAL_REMOTE_DIRS = (
    "/etc/sudoers.d/", "/etc/ssh/", "/etc/pam.d/", "/etc/security/", "/etc/hermclaw/", "/etc/profile.d/",
    "/etc/ld.so.conf.d/", "/etc/cron.d/", "/etc/cron.hourly/", "/etc/cron.daily/", "/etc/cron.weekly/",
    "/etc/cron.monthly/", "/var/spool/cron/", "/boot/", "/root/",
)  # fmt: skip
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]{0,199}$")
UPLOAD_OK_MARKER = "hermclaw-upload: ok"
_SYSTEMD_UNAVAILABLE = (
    "not been booted with systemd",
    "failed to connect to bus",
    "systemctl: not found",
    "systemctl: command not found",
    "no such file or directory",
)
# runs under ``sh -c <script> hermclaw-upload <dir> <path> <sha256> <mode>``: positional parameters, no interpolation
_UPLOAD_SCRIPT = (
    "set -eu; umask 077; "
    'if [ -d "$2" ]; then echo "hermclaw-upload: target is a directory" >&2; exit 4; fi; '
    't=$(mktemp "$1/.hermclaw-upload.XXXXXX"); trap \'rm -f "$t"\' EXIT; '
    'cat > "$t"; '
    "if command -v sha256sum >/dev/null 2>&1; then "
    's=$(sha256sum < "$t"); '
    'if [ "${s%% *}" != "$3" ]; then echo "hermclaw-upload: checksum mismatch" >&2; exit 3; fi; '
    "fi; "
    'chmod "$4" "$t"; mv -f "$t" "$2"; trap - EXIT; '
    f'echo "{UPLOAD_OK_MARKER}"'
)


class SshOutcome(StrEnum):
    ok = "ok"  # ran, exit code 0 (service status: the state could be queried)
    failed = "failed"  # ran, non-zero exit code
    timed_out = "timed_out"  # killed after the timeout
    transport_error = "transport_error"  # ssh could not connect/authenticate (exit 255), client/key/host-key problem
    denied = "denied"  # capability / host / command policy refused it – nothing ran
    approval_required = "approval_required"  # needs (or still waits for) an operator approval – nothing ran


_SEVERITY: dict[SshOutcome, Severity] = {
    SshOutcome.ok: Severity.info,
    SshOutcome.failed: Severity.warning,
    SshOutcome.timed_out: Severity.warning,
    SshOutcome.transport_error: Severity.error,
    SshOutcome.denied: Severity.warning,
    SshOutcome.approval_required: Severity.warning,
}


@dataclass(frozen=True)
class SshCallContext:
    """Who calls: job/step/attempt for the audit, the step capability for the 26.8 gate."""

    job_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    tool_call_id: uuid.UUID | None = None
    capability: str | None = None
    actor: str = "agent"  # agent | runtime | operator


@dataclass
class SshToolResult:
    tool: str
    host: str
    outcome: SshOutcome
    command: str  # redacted command line as audited
    classification: str | None = None
    reason: str = ""
    code: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int | None = None
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    approval_token: str | None = None
    command_run_ids: list[uuid.UUID] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome == SshOutcome.ok

    def to_dict(self) -> dict[str, Any]:
        """Compact machine-readable view (no stdout/stderr – those are in :meth:`render`)."""
        return {
            "tool": self.tool,
            "host": self.host,
            "outcome": self.outcome.value,
            "classification": self.classification,
            "code": self.code,
            "reason": self.reason,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "approval": self.approval_token,  # key name avoids the generic '*token*' redaction of the event store
            "command_run_ids": [str(i) for i in self.command_run_ids],
            **self.data,
        }

    def render(self, limit: int = 12000) -> str:
        """Bounded text for the model / UI."""
        head = f"{self.tool} on {self.host}: {self.outcome.value}"
        facts = []
        if self.classification:
            facts.append(f"class {self.classification}")
        if self.exit_code is not None:
            facts.append(f"exit {self.exit_code}")
        if self.duration_ms is not None:
            facts.append(f"{self.duration_ms} ms")
        if facts:
            head += f" ({', '.join(facts)})"
        lines = [head, f"$ {self.command}"]
        if self.reason:
            lines.append(self.reason)
        if self.outcome == SshOutcome.approval_required and self.approval_token:
            lines.append(
                f"An operator has to approve {self.approval_token} for this job; afterwards repeat the identical call "
                f"and pass {self.approval_token} as approval_token."
            )
        for key in ("active_state", "remote_path", "bytes", "sha256"):
            if key in self.data:
                lines.append(f"{key}: {self.data[key]}")
        out, err = strip_ansi(self.stdout), strip_ansi(self.stderr)
        if out:
            lines += ["--- stdout ---" + (" (truncated)" if self.stdout_truncated else ""), out.rstrip("\n")]
        if err:
            lines += ["--- stderr ---" + (" (truncated)" if self.stderr_truncated else ""), err.rstrip("\n")]
        return clip_middle("\n".join(lines), limit)[0]


@dataclass(frozen=True)
class _Call:
    """One tool invocation: tool name, requested host, caller context and the redacted command for audit/output."""

    tool: str
    host: str
    ctx: SshCallContext
    display: str


@dataclass(frozen=True)
class _Planned:
    """One remote command on an allow-listed host together with the policy decision about it."""

    host: SshHost
    command: str  # exact command line
    decision: SshDecision
    builtin: bool  # assembled by the runtime from validated parts (upload, service status)
    stdin: bytes | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _remote_path_problem(remote_path: str) -> str | None:
    if not _REMOTE_PATH_RE.match(remote_path):
        return "remote_path must be an absolute path of letters, digits and ._@+-/ (no spaces or shell characters)"
    if posixpath.normpath(remote_path) != remote_path or remote_path == "/":
        return "remote_path must be normalised (no '..', '.', '//' or trailing '/') and name a file"
    return None


def _normalise_mode(mode: str) -> str | None:
    """``0644``-style mode without special bits and without world write (a world-writable server file is an escalation)."""
    if not _MODE_RE.match(mode) or int(mode, 8) & 0o002:
        return None
    return mode.rjust(4, "0")


def critical_remote_path(remote_path: str) -> str | None:
    """Why replacing ``remote_path`` is security critical (login, privilege, boot, gate config), else ``None``."""
    parts = remote_path.split("/")
    if ".ssh" in parts or parts[-1].startswith("authorized_keys"):
        return "it changes SSH login keys"
    if remote_path in CRITICAL_REMOTE_FILES or remote_path.startswith(CRITICAL_REMOTE_DIRS):
        return "it changes accounts, privileges, login/boot configuration or the hermclaw ssh gate"
    return None


def _opened_path(fd: int) -> Path | None:
    """Real path of an open descriptor (Linux ``/proc``); ``None`` where that is not available."""
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def read_upload_source(path: Path, max_bytes: int, *, within: Path | None = None) -> bytes:
    """Read a regular local file without following a final symlink, bounded by ``max_bytes``.

    ``within``: the opened file must (still) lie below this directory – closes the window between resolving a
    workspace path and opening it, in which a directory component could be swapped for a symlink.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SshPolicyError(f"upload source {path.name!r} cannot be opened: {exc.strerror}", code=SSH_UPLOAD_INVALID) from None
    with os.fdopen(fd, "rb") as fh:
        if within is not None:
            opened = _opened_path(fh.fileno())
            if opened is not None and within.resolve() not in opened.parents:
                raise SshPolicyError(f"upload source {path.name!r} resolves outside the workspace", code=SSH_UPLOAD_INVALID)
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise SshPolicyError(f"upload source {path.name!r} is not a regular file", code=SSH_UPLOAD_INVALID)
        if st.st_size > max_bytes:
            raise SshPolicyError(f"upload source is larger than {max_bytes} bytes", code=SSH_UPLOAD_INVALID)
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SshPolicyError(f"upload source is larger than {max_bytes} bytes", code=SSH_UPLOAD_INVALID)
    return data


class SshTools:
    """The four SSH tools with policy, approvals, audit and persistence."""

    def __init__(
        self,
        runner: SshRunner,
        policy: SshCommandPolicy,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        redactor: Redactor = DEFAULT_REDACTOR,
    ) -> None:
        self.runner = runner
        self.policy = policy
        self._sm = sessionmaker
        self.excerpt_chars = excerpt_chars
        self.max_upload_bytes = max_upload_bytes
        self.redactor = redactor

    @classmethod
    def from_config(
        cls,
        config: HermclawConfig,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        secret_store: SecretStore | None = None,
        runtime_dir: Path | None = None,
        compound_allowlist: Sequence[str] | None = None,
        ssh_binary: str = "ssh",
    ) -> SshTools:
        """``compound_allowlist`` defaults to ``policies.ssh.compound_allowlist``."""
        allowed = config.policies.ssh.compound_allowlist if compound_allowlist is None else compound_allowlist
        policy = SshCommandPolicy(config.policies.commands, config.policies.ssh, compound_allowlist=allowed)
        runner = SshRunner(
            HostRegistry.from_config(config),
            KeyManager(secret_store=secret_store, runtime_dir=runtime_dir),
            policy=policy,
            default_timeout=float(config.policies.ssh.default_timeout_seconds),
            max_output_bytes=config.policies.commands.max_output_bytes,
            ssh_binary=ssh_binary,
        )
        return cls(runner, policy, sessionmaker)

    # ================================================================================================ tools
    async def ssh_read(
        self,
        host: str,
        command: str,
        *,
        ctx: SshCallContext | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 – forwarded to the runner (kill on expiry)
    ) -> SshToolResult:
        """Run a command the policy classifies as ``read`` (anything else is refused with ``SSH_NOT_READ_ONLY``)."""
        call = _Call("ssh_read", host, ctx or SshCallContext(), self._display(command))
        gate = await self._gate(call)
        if isinstance(gate, SshToolResult):
            return gate
        decision = self.policy.decide(gate, command, step_id=call.ctx.step_id, read_only=True)
        return await self._run_planned(call, _Planned(gate, command, decision, builtin=False), approval_token=None, timeout=timeout)

    async def ssh_command(
        self,
        host: str,
        command: str,
        *,
        approval_token: str | None = None,
        ctx: SshCallContext | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> SshToolResult:
        """Run any non-forbidden command; mutating/destructive commands follow the approval policy."""
        call = _Call("ssh_command", host, ctx or SshCallContext(), self._display(command))
        gate = await self._gate(call)
        if isinstance(gate, SshToolResult):
            return gate
        decision = self.policy.decide(gate, command, step_id=call.ctx.step_id)
        return await self._run_planned(
            call, _Planned(gate, command, decision, builtin=False), approval_token=approval_token, timeout=timeout
        )

    async def ssh_upload(
        self,
        host: str,
        source: Path | bytes,
        remote_path: str,
        *,
        mode: str = "0644",
        approval_token: str | None = None,
        ctx: SshCallContext | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> SshToolResult:
        """Atomically write ``source`` to ``remote_path`` (temp file in the target dir, checksum check, ``mv``)."""
        call = _Call("ssh_upload", host, ctx or SshCallContext(), f"upload -> {self._display(remote_path)[:300]}")
        gate = await self._gate(call)
        if isinstance(gate, SshToolResult):
            return gate
        problem = _remote_path_problem(remote_path)
        norm_mode = _normalise_mode(mode)
        if problem is None and norm_mode is None:
            problem = "mode must be an octal permission like 0644 (no setuid/setgid/sticky bits, not world-writable)"
        content = b""
        if problem is None:
            try:
                content = await self._upload_content(source)
            except SshPolicyError as exc:
                problem = exc.message
        if problem is not None or norm_mode is None:
            return await self._refuse(call, SshOutcome.denied, problem or "invalid upload", SSH_UPLOAD_INVALID, classification="mutate")
        digest = hashlib.sha256(content).hexdigest()
        parts = ["sh", "-c", shlex.quote(_UPLOAD_SCRIPT), "hermclaw-upload", shlex.quote(posixpath.dirname(remote_path))]
        command = " ".join([*parts, shlex.quote(remote_path), digest, norm_mode])
        call = _Call(call.tool, call.host, call.ctx, f"upload {remote_path} (mode {norm_mode}, {len(content)} bytes, sha256 {digest})")
        subject = f"ssh_upload {remote_path} mode={norm_mode} sha256={digest}"  # the approval covers exactly this content
        critical = critical_remote_path(remote_path)
        kind: SshCommandClass = "destructive" if critical else "mutate"
        reason = f"upload overwrites a security-critical file: {critical}" if critical else "upload writes a file"
        decision = self.policy.decide_builtin(gate, kind, subject, step_id=call.ctx.step_id, reason=reason)
        extra: dict[str, Any] = {"remote_path": remote_path, "bytes": len(content), "sha256": digest, "mode": norm_mode}
        if critical:
            extra["critical_path"] = True
        planned = _Planned(gate, command, decision, builtin=True, stdin=content, extra=extra)
        result = await self._run_planned(call, planned, approval_token=approval_token, timeout=timeout)
        if result.outcome == SshOutcome.ok and UPLOAD_OK_MARKER not in result.stdout:
            result.outcome, result.code, result.reason = SshOutcome.failed, SSH_UPLOAD_INVALID, "upload did not report success"
        elif result.outcome == SshOutcome.failed and result.exit_code == 3:
            result.code, result.reason = SSH_UPLOAD_INVALID, "checksum of the uploaded file does not match; the target was not changed"
        elif result.outcome == SshOutcome.failed and result.exit_code == 4:
            result.code, result.reason = SSH_UPLOAD_INVALID, "remote_path is a directory"
        return result

    async def ssh_service_status(
        self,
        host: str,
        unit: str,
        *,
        ctx: SshCallContext | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> SshToolResult:
        """``systemctl is-active`` + ``systemctl status --no-pager`` of one unit (both read-only, runtime-built)."""
        call = _Call("ssh_service_status", host, ctx or SshCallContext(), f"systemctl status {self._display(unit)[:200]}")
        gate = await self._gate(call)
        if isinstance(gate, SshToolResult):
            return gate
        if not UNIT_RE.match(unit):
            return await self._refuse(call, SshOutcome.denied, "invalid systemd unit name", SSH_UNIT_INVALID, classification="read")
        extra = {"unit": unit}
        active_cmd = f"systemctl is-active {unit}"
        decision = self.policy.decide_builtin(gate, "read", active_cmd, step_id=call.ctx.step_id, reason="systemd state query")
        active_call = _Call(call.tool, call.host, call.ctx, active_cmd)
        active = await self._run_planned(
            active_call, _Planned(gate, active_cmd, decision, builtin=True, extra=extra), approval_token=None, timeout=timeout
        )
        if active.outcome in (SshOutcome.timed_out, SshOutcome.transport_error, SshOutcome.denied):
            return active
        if _systemd_missing(active):
            active.outcome, active.code = SshOutcome.failed, SSH_SERVICE_UNAVAILABLE
            active.reason = "systemd/systemctl is not available on this host"
            active.data.update({"active_state": "unavailable", "active": False, "systemd_available": False})
            return active
        state = (active.stdout.strip().splitlines() or [""])[0].strip()
        status_cmd = f"systemctl status --no-pager --full --lines=40 {unit}"
        decision = self.policy.decide_builtin(gate, "read", status_cmd, step_id=call.ctx.step_id, reason="systemd status query")
        status_call = _Call(call.tool, call.host, call.ctx, status_cmd)
        status = await self._run_planned(
            status_call, _Planned(gate, status_cmd, decision, builtin=True, extra=extra), approval_token=None, timeout=timeout
        )
        status.command_run_ids = active.command_run_ids + status.command_run_ids
        status.data.update(
            {
                "active_state": state or "unknown",
                "active": state == "active",
                "systemd_available": True,
                "is_active_exit_code": active.exit_code,
            }
        )
        if status.outcome == SshOutcome.failed and status.exit_code in (1, 2, 3, 4):
            # LSB status codes: 1-3 = not running, 4 = unknown unit. The state was queried successfully.
            status.outcome, status.code, status.reason = SshOutcome.ok, None, ""
            if status.exit_code == 4:
                status.data["active_state"] = "not-found"
        return status

    async def record_refusal(self, tool: str, host: str, reason: str, code: str, *, ctx: SshCallContext, display: str) -> SshToolResult:
        """Audit a refusal decided outside these tools (e.g. the provider refused an upload source path)."""
        shown = host if HOST_ID_RE.match(host) else repr(host[:40])
        return await self._refuse(_Call(tool, shown, ctx, self._display(display)), SshOutcome.denied, reason, code)

    # ================================================================================================ gates
    async def _gate(self, call: _Call) -> SshHost | SshToolResult:
        try:
            assert_ssh_allowed(call.ctx.capability)
            return self.runner.registry.get(call.host)
        except SshPolicyError as exc:
            shown = call.host if HOST_ID_RE.match(call.host) else repr(call.host[:40])
            return await self._refuse(_Call(call.tool, shown, call.ctx, call.display), SshOutcome.denied, exc.message, exc.code)

    async def _upload_content(self, source: Path | bytes) -> bytes:
        if isinstance(source, bytes):
            if len(source) > self.max_upload_bytes:
                raise SshPolicyError(f"upload is larger than {self.max_upload_bytes} bytes", code=SSH_UPLOAD_INVALID)
            return source
        return await asyncio.to_thread(read_upload_source, source, self.max_upload_bytes)

    async def _run_planned(
        self,
        call: _Call,
        planned: _Planned,
        *,
        approval_token: str | None,
        timeout: float | None,  # noqa: ASYNC109
    ) -> SshToolResult:
        decision, cls, host = planned.decision, planned.decision.classification, planned.host
        if timeout is not None and not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            reason = f"timeout must be within (0, {MAX_TIMEOUT_SECONDS}] seconds"
            return await self._refuse(call, SshOutcome.denied, reason, SSH_COMMAND_INVALID, classification=cls, extra=planned.extra)
        if decision.verdict == Verdict.deny:
            return await self._refuse(call, SshOutcome.denied, decision.reason, decision.code, classification=cls, extra=planned.extra)
        token = decision.approval_token if decision.verdict == Verdict.approval_required else None
        if decision.verdict == Verdict.approval_required:
            code, why = _approval_problem(approval_token, token, call.ctx)
            if code is not None:
                return await self._refuse(
                    call,
                    SshOutcome.approval_required,
                    why or decision.reason,
                    code,
                    classification=cls,
                    token=token,
                    extra=planned.extra,
                )
        try:
            await self.runner.preflight(host.id)
        except (SshConfigError, SshPolicyError) as exc:
            outcome = SshOutcome.denied if isinstance(exc, SshPolicyError) else SshOutcome.transport_error
            return await self._refuse(call, outcome, exc.message, exc.code, classification=cls, extra=planned.extra)
        started = await self._begin(call, planned, token)
        if started is None:
            reason = f"approval {token} is not granted (or was already used) for this job"
            return await self._refuse(
                call, SshOutcome.approval_required, reason, SSH_APPROVAL_PENDING, classification=cls, token=token, extra=planned.extra
            )
        run_id, approved_by = started
        return await self._execute(call, planned, run_id=run_id, timeout=timeout, token=token, approved_by=approved_by)

    # ================================================================================================ persistence
    async def _begin(self, call: _Call, planned: _Planned, token: str | None) -> tuple[uuid.UUID, str | None] | None:
        """Consume the approval (if any) and write the 'started' ``command_runs`` row in one transaction."""
        ctx = call.ctx
        run_id = uuid.uuid4()
        approved_by: str | None = None
        async with self._sm() as session, session.begin():
            if token is not None:
                if ctx.job_id is None:
                    return None
                if not await consume_approval(session, ctx.job_id, token, host_id=planned.host.id, step_id=ctx.step_id):
                    return None
                approved_by = await _approver(session, ctx.job_id, token)
            session.add(
                CommandRun(
                    id=run_id,
                    job_id=ctx.job_id,
                    step_id=ctx.step_id,
                    attempt_id=ctx.attempt_id,
                    tool_call_id=ctx.tool_call_id,
                    worker_id=planned.host.id,
                    target="ssh",
                    command=call.display,
                    classification=planned.decision.classification.kind,
                    cwd=None,
                    exit_code=None,
                    timed_out=False,
                    network=True,
                )
            )
        return run_id, approved_by

    async def _execute(
        self,
        call: _Call,
        planned: _Planned,
        *,
        run_id: uuid.UUID,
        timeout: float | None,  # noqa: ASYNC109
        token: str | None,
        approved_by: str | None,
    ) -> SshToolResult:
        cls = planned.decision.classification
        result = SshToolResult(
            tool=call.tool,
            host=planned.host.id,
            outcome=SshOutcome.transport_error,
            command=call.display,
            classification=cls.kind,
            approval_token=token,
            command_run_ids=[run_id],
            data={**planned.extra, "sudo": cls.sudo, "compound": cls.compound},
        )
        raw: SshResult | None = None
        try:
            raw = await self.runner.execute(planned.host.id, planned.command, timeout=timeout, stdin=planned.stdin, builtin=planned.builtin)
        except asyncio.CancelledError:
            result.outcome, result.code, result.reason = SshOutcome.failed, SSH_COMMAND_FAILED, "cancelled; the ssh client was stopped"
            await asyncio.shield(self._finish(result, call.ctx, run_id, approved_by))
            raise
        except SshPolicyError as exc:
            result.outcome, result.code, result.reason = SshOutcome.denied, exc.code, exc.message
        except (SshConfigError, SshTransportError) as exc:
            result.code, result.reason = exc.code, exc.message
        except (OSError, ValueError, HermclawError) as exc:
            result.code, result.reason = SSH_TRANSPORT_ERROR, f"{type(exc).__name__}: {self.redactor.text(str(exc))[:300]}"
        if raw is not None:
            _apply(result, raw)
        await self._finish(result, call.ctx, run_id, approved_by)
        return result

    async def _finish(self, result: SshToolResult, ctx: SshCallContext, run_id: uuid.UUID, approved_by: str | None) -> None:
        stdout = self.redactor.text(result.stdout)
        stderr = self.redactor.text(result.stderr or (result.reason if result.exit_code is None else ""))
        try:
            async with self._sm() as session, session.begin():
                await session.execute(
                    update(CommandRun)
                    .where(CommandRun.id == run_id)
                    .values(
                        exit_code=result.exit_code,
                        timed_out=result.timed_out,
                        stdout_excerpt=excerpt(stdout, self.excerpt_chars),
                        stderr_excerpt=excerpt(stderr, self.excerpt_chars),
                        duration_ms=result.duration_ms,
                    )
                )
                await self._event(session, result, ctx, run_id=run_id, approved_by=approved_by)
        except Exception:
            # the command already ran: never report it as not run; the 'started' row stays as evidence
            log.exception("ssh audit record could not be completed", extra={"host": result.host, "command_run_id": str(run_id)})
            result.data["audit_incomplete"] = True

    async def _refuse(
        self,
        call: _Call,
        outcome: SshOutcome,
        reason: str,
        code: str | None,
        *,
        classification: SshClassification | str | None = None,
        token: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> SshToolResult:
        kind = classification.kind if isinstance(classification, SshClassification) else classification
        result = SshToolResult(
            tool=call.tool,
            host=call.host,
            outcome=outcome,
            command=call.display,
            classification=kind,
            reason=self.redactor.text(reason),
            code=code,
            approval_token=token,
            data=dict(extra or {}),
        )
        if isinstance(classification, SshClassification):
            result.data.update({"sudo": classification.sudo, "compound": classification.compound})
        async with self._sm() as session, session.begin():
            await self._event(session, result, call.ctx, run_id=None, approved_by=None)
        log.info("ssh call refused", extra={"tool": call.tool, "host": call.host, "outcome": outcome.value, "code": code})
        return result

    async def _event(
        self, session: AsyncSession, result: SshToolResult, ctx: SshCallContext, *, run_id: uuid.UUID | None, approved_by: str | None
    ) -> None:
        payload: dict[str, Any] = {
            "tool": result.tool,
            "host": result.host,
            "outcome": result.outcome.value,
            "classification": result.classification,
            "command": result.command[:_EVENT_COMMAND_CHARS],
            "code": result.code,
            "reason": result.reason[:500],
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "actor": ctx.actor,
            "capability": ctx.capability,
            "command_run_id": str(run_id) if run_id else None,
        }
        if result.approval_token:
            payload["approval"] = result.approval_token
        if approved_by:
            payload["approved_by"] = approved_by
        for key in _EVENT_DATA_KEYS:
            if key in result.data:
                payload[key] = result.data[key]
        if result.stdout_truncated or result.stderr_truncated:
            payload["truncated"] = True
        await append_event(
            session,
            EventType.SSH_COMMAND,
            source_type=SOURCE_TYPE,
            source_id=result.host[:100],
            job_id=ctx.job_id,
            step_id=ctx.step_id,
            attempt_id=ctx.attempt_id,
            severity=_SEVERITY[result.outcome],
            payload=payload,
            duration_ms=result.duration_ms,
        )

    def _display(self, command: str) -> str:
        return self.redactor.text(command.strip())[:4000]


# ==================================================================================================== module helpers
_EVENT_DATA_KEYS = (
    "stdout_bytes",
    "stderr_bytes",
    "remote_path",
    "bytes",
    "sha256",
    "mode",
    "unit",
    "sudo",
    "compound",
    "critical_path",
    "audit_incomplete",
)


def _approval_problem(given: str | None, expected: str | None, ctx: SshCallContext) -> tuple[str | None, str | None]:
    """``(code, reason)`` why an approval-requiring call may not run yet; ``(None, None)`` when it may try."""
    if expected is None or given is None:
        return SSH_APPROVAL_REQUIRED, None
    if not APPROVAL_TOKEN_RE.match(given) or not hmac.compare_digest(given, expected):
        return SSH_APPROVAL_INVALID, "approval_token does not belong to this host, command and step"
    if ctx.job_id is None:
        return SSH_APPROVAL_PENDING, "approvals are stored per job; this call has no job context"
    return None, None


async def _approver(session: AsyncSession, job_id: uuid.UUID, token: str) -> str | None:
    meta = (await session.scalar(select(Job.metadata_).where(Job.id == job_id))) or {}
    for entry in reversed(meta.get(CONSUMED_KEY) or []):
        if isinstance(entry, dict) and entry.get("token") == token:
            value = entry.get("approved_by")
            return str(value)[:200] if value else None
    return None


def _apply(result: SshToolResult, raw: SshResult) -> None:
    result.exit_code = raw.exit_code
    result.stdout, result.stderr = raw.stdout, raw.stderr
    result.duration_ms = raw.duration_ms
    result.timed_out = raw.timed_out
    result.stdout_truncated, result.stderr_truncated = raw.stdout_truncated, raw.stderr_truncated
    result.data.update({"stdout_bytes": raw.stdout_bytes, "stderr_bytes": raw.stderr_bytes})
    if raw.timed_out:
        result.outcome, result.code, result.reason = SshOutcome.timed_out, SSH_TIMEOUT, "timed out; the ssh client was killed"
    elif raw.exit_code == SSH_TRANSPORT_EXIT:
        result.outcome, result.code = SshOutcome.transport_error, SSH_TRANSPORT_ERROR
        result.reason = "ssh exit 255: connection, authentication or host-key failure (or the remote command exited 255)"
    elif raw.exit_code == 0:
        result.outcome, result.code, result.reason = SshOutcome.ok, None, ""
    else:
        result.outcome, result.code, result.reason = SshOutcome.failed, SSH_COMMAND_FAILED, f"remote command exited {raw.exit_code}"


def _systemd_missing(result: SshToolResult) -> bool:
    """systemctl absent (127) or present without a running systemd (containers, chroots)."""
    if result.exit_code == 127:
        return True
    if result.exit_code == 0:
        return False
    text = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in text for marker in _SYSTEMD_UNAVAILABLE)


__all__ = [
    "CRITICAL_REMOTE_DIRS",
    "CRITICAL_REMOTE_FILES",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "UNIT_RE",
    "UPLOAD_OK_MARKER",
    "SshCallContext",
    "SshOutcome",
    "SshToolResult",
    "SshTools",
    "critical_remote_path",
    "read_upload_source",
]
