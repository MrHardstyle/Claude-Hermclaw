"""Adapter: the SSH tools behind the tool engine's operations-tool protocol (``hermclaw.tools.ops``).

The tool engine has already validated the arguments (``SshReadArgs`` …) and the capability gate
(``OPS_TOOLS_BY_CAPABILITY``); :class:`SshTools` checks the capability again (26.8, defence in depth) together with
host allow-list, command policy, approvals and audit. ``ssh_upload`` reads its source from the job workspace:
the path must stay inside the workspace (no ``..``, no symlink escape, no ``.git``) and must not match the always
forbidden scope globs (``**/.env``, ``**/*.pem`` …) – secrets in the workspace are never shipped to a host.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from hermclaw.contracts.tools import SSH_TOOLS, ToolName
from hermclaw.core.config import HermclawConfig
from hermclaw.scope.guard import any_match
from hermclaw.sshtools.errors import SSH_UPLOAD_INVALID, SshPolicyError
from hermclaw.sshtools.tools import SshCallContext, SshToolResult, SshTools, read_upload_source
from hermclaw.tools.ops import OpsCall, OpsResult
from hermclaw.tools.registry import SshCommandArgs, SshReadArgs, SshServiceStatusArgs, SshUploadArgs

DEFAULT_OUTPUT_CHARS = 12000


def resolve_workspace_file(root: Path, relative: str, *, forbidden_globs: Sequence[str] = ()) -> Path:
    """Resolve ``relative`` inside ``root`` for an upload; refuses traversal, symlink escapes, ``.git`` and secrets."""
    if not relative or "\x00" in relative or relative.startswith(("/", "\\")):
        raise SshPolicyError("upload path must be relative to the repository root", code=SSH_UPLOAD_INVALID)
    raw = relative.replace("\\", "/").removeprefix("./")
    rel = PurePosixPath(raw)
    # PurePosixPath silently collapses 'a//b' and 'a/./b': check the raw segments so the audited path is the used one
    if any(part in ("..", "", ".") for part in raw.split("/")) or ".git" in rel.parts:
        raise SshPolicyError(
            "upload path must be normalised (no '..', '.', empty segments) and must not touch '.git'", code=SSH_UPLOAD_INVALID
        )
    normalised = rel.as_posix()
    try:
        forbidden = bool(forbidden_globs) and any_match(normalised, list(forbidden_globs))
    except ValueError:
        forbidden = True
    if forbidden:
        raise SshPolicyError(f"'{normalised}' matches an always-forbidden path (secrets are never uploaded)", code=SSH_UPLOAD_INVALID)
    base = root.resolve()
    try:
        target = (base / normalised).resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise SshPolicyError(f"upload source '{normalised}' does not exist in the workspace", code=SSH_UPLOAD_INVALID) from None
    except (OSError, RuntimeError) as exc:  # symlink loops
        raise SshPolicyError(f"upload source '{normalised}' cannot be resolved: {exc}", code=SSH_UPLOAD_INVALID) from None
    if base not in target.parents:
        raise SshPolicyError(f"upload source '{normalised}' resolves outside the workspace", code=SSH_UPLOAD_INVALID)
    inner = target.relative_to(base).as_posix()
    if ".git" in PurePosixPath(inner).parts or (forbidden_globs and any_match(inner, list(forbidden_globs))):
        raise SshPolicyError(f"upload source '{normalised}' points to a forbidden path", code=SSH_UPLOAD_INVALID)
    return target


class SshOpsProvider:
    """``OpsToolProvider`` for ``ssh_read`` / ``ssh_command`` / ``ssh_upload`` / ``ssh_service_status``."""

    def __init__(self, tools: SshTools, *, forbidden_upload_globs: Sequence[str] = (), output_chars: int = DEFAULT_OUTPUT_CHARS) -> None:
        self.tools = tools
        self.forbidden_upload_globs = list(forbidden_upload_globs)
        self.output_chars = output_chars

    @classmethod
    def for_config(cls, tools: SshTools, config: HermclawConfig) -> SshOpsProvider:
        return cls(
            tools,
            forbidden_upload_globs=config.policies.scope.always_forbidden,
            output_chars=config.policies.coder.tool_output_chars,
        )

    def provides(self, tool: ToolName) -> bool:
        return tool in SSH_TOOLS

    async def call(self, call: OpsCall) -> OpsResult:
        ctx = SshCallContext(
            job_id=call.job_id,
            step_id=call.step_id,
            attempt_id=call.attempt_id,
            capability=call.capability,
            actor="agent",
        )
        args = call.args
        result: SshToolResult
        if call.tool == ToolName.ssh_read and isinstance(args, SshReadArgs):
            result = await self.tools.ssh_read(args.host, args.command, ctx=ctx, timeout=args.timeout_seconds)
        elif call.tool == ToolName.ssh_command and isinstance(args, SshCommandArgs):
            result = await self.tools.ssh_command(
                args.host, args.command, approval_token=args.approval_token, ctx=ctx, timeout=args.timeout_seconds
            )
        elif call.tool == ToolName.ssh_upload and isinstance(args, SshUploadArgs):
            try:
                source = resolve_workspace_file(call.workspace.path, args.path, forbidden_globs=self.forbidden_upload_globs)
                content = await asyncio.to_thread(read_upload_source, source, self.tools.max_upload_bytes)
            except SshPolicyError as exc:
                result = await self.tools.record_refusal(
                    "ssh_upload", args.host, exc.message, exc.code, ctx=ctx, display=f"upload {args.path[:200]} -> {args.remote_path[:300]}"
                )
                return OpsResult(False, result.render(self.output_chars), result.to_dict(), error_code=exc.code)
            result = await self.tools.ssh_upload(
                args.host, content, args.remote_path, mode=args.mode, approval_token=args.approval_token, ctx=ctx
            )
        elif call.tool == ToolName.ssh_service_status and isinstance(args, SshServiceStatusArgs):
            result = await self.tools.ssh_service_status(args.host, args.unit, ctx=ctx)
        else:
            return OpsResult(False, f"{call.tool.value} is not an SSH tool or got unexpected arguments", error_code="OPS_TOOL_UNAVAILABLE")
        return OpsResult(result.ok, result.render(self.output_chars), result.to_dict(), error_code=None if result.ok else result.code)


__all__ = ["SshOpsProvider", "resolve_workspace_file"]
