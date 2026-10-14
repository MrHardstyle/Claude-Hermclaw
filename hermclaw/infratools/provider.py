"""Adapter: the container/proxy tools behind the tool engine's operations-tool protocol (``hermclaw.tools.ops``).

``hermclaw.runtime.ops.build_ops`` imports this module and calls :func:`build_provider`. The tool engine has validated
the arguments (``ContainerQueryArgs``, ``ContainerInspectArgs``, ``ComposeArgs``, ``ConfigSourceArgs``) and the
capability gate (``OPS_TOOLS_BY_CAPABILITY``: ``docker`` and ``deploy`` steps); the provider checks the gate again
(defence in depth), confines file arguments to the job workspace and maps results to :class:`OpsResult`.

Containers and compose sandboxes run on the *execution host*: the ``execution_worker`` of ``hosts.yaml`` when the
runtime executes remotely (production, through the SSH runner), otherwise this machine. ``nginx_test`` and
``proxy_config_validate`` always run on the orchestrator (they only read the configuration).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.tools import CONTAINER_TOOLS, OPS_TOOLS_BY_CAPABILITY, ToolName
from hermclaw.core.config import HermclawConfig, HostsConfig
from hermclaw.core.settings import Settings
from hermclaw.infratools import InfraTools
from hermclaw.infratools.compose_sandbox import cleanup_stale_sandboxes
from hermclaw.infratools.errors import INFRA_INVALID_ARGUMENT, INFRA_PATH_INVALID, InfraToolError
from hermclaw.infratools.process import InfraContext, RemoteRunner
from hermclaw.tools.ops import OpsCall, OpsResult
from hermclaw.tools.registry import ComposeArgs, ConfigSourceArgs, ContainerInspectArgs, ContainerQueryArgs

DEFAULT_OUTPUT_CHARS = 12000
MAX_CONFIG_BYTES = 2_000_000
CAPABILITY_REFUSED = "OPS_CAPABILITY_REFUSED"


def workspace_file(root: Path, rel: str) -> Path:
    """``rel`` inside the workspace ``root`` (no absolute paths, no ``..``, symlinks must not leave the workspace)."""
    if not rel or rel.startswith(("/", "~")) or "\x00" in rel:
        raise InfraToolError(f"path '{rel}' must be relative to the repository root", code=INFRA_PATH_INVALID)
    base = root.resolve()
    path = (base / rel).resolve()
    if path != base and base not in path.parents:
        raise InfraToolError(f"path '{rel}' leaves the workspace", code=INFRA_PATH_INVALID)
    if not path.is_file():
        raise InfraToolError(f"'{rel}' is not a file in the workspace", code=INFRA_PATH_INVALID)
    return path


def default_execution_host(hosts: HostsConfig, settings: Settings, *, remote_available: bool) -> str | None:
    """The execution worker when the runtime executes remotely (same rule as ``settings.execution_mode``)."""
    mode = settings.execution_mode
    remote = mode == "remote" or (mode == "auto" and settings.env == "production")
    if not remote or not remote_available:
        return None
    workers = hosts.by_role("execution_worker")
    return workers[0].id if workers else None


class InfraOpsProvider:
    """``OpsToolProvider`` for ``container_list`` … ``proxy_config_validate``."""

    def __init__(self, tools: InfraTools, *, default_host: str | None = None, output_chars: int = DEFAULT_OUTPUT_CHARS) -> None:
        self.tools = tools
        self.default_host = default_host
        self.output_chars = output_chars

    def provides(self, tool: ToolName) -> bool:
        return tool in CONTAINER_TOOLS

    def _clip(self, text: str) -> str:
        return text if len(text) <= self.output_chars else text[: self.output_chars - 20] + "\n…[truncated]"

    async def call(self, call: OpsCall) -> OpsResult:
        if call.tool not in OPS_TOOLS_BY_CAPABILITY.get(call.capability or "", frozenset()):
            return OpsResult(False, f"{call.tool.value} is not allowed for capability '{call.capability}'", error_code=CAPABILITY_REFUSED)
        ctx = InfraContext(job_id=call.job_id, step_id=call.step_id, attempt_id=call.attempt_id, actor="agent")
        try:
            return await self._dispatch(call, ctx)
        except InfraToolError as exc:
            return OpsResult(
                False, self._clip(f"{call.tool.value} failed: {exc.message}"), data={"error": exc.to_dict()}, error_code=exc.code
            )

    async def _dispatch(self, call: OpsCall, ctx: InfraContext) -> OpsResult:
        args = call.args
        root = call.workspace.path
        if call.tool == ToolName.container_list and isinstance(args, ContainerQueryArgs):
            listed = await self.tools.container_list(
                host=args.host or self.default_host, engine=args.engine, all_containers=args.all, ctx=ctx
            )
            lines = [f"containers on {listed.host}: {len(listed.containers)}"]
            lines += [f"- {c.summary()}" for c in listed.containers]
            lines += [f"engine {s.engine}: {'ok' if s.available and not s.error else s.error}" for s in listed.engines]
            if listed.error:
                lines.append(f"error: {listed.error}")
            return OpsResult(listed.ok, self._clip("\n".join(lines)), data=listed.model_dump(mode="json"), error_code=listed.code)
        if call.tool == ToolName.container_inspect and isinstance(args, ContainerInspectArgs):
            info = await self.tools.container_inspect(args.name, host=args.host or self.default_host, engine=args.engine, ctx=ctx)
            data = info.model_dump(mode="json")
            return OpsResult(True, self._clip(info.summary() + "\n" + json.dumps(data, indent=1, sort_keys=True)), data=data)
        if call.tool == ToolName.compose_validate and isinstance(args, ComposeArgs):
            path = await asyncio.to_thread(workspace_file, root, args.path)
            report = await self.tools.compose_validate(path, ctx=ctx)
            return OpsResult(
                report.ok, self._clip(report.render()), data=report.to_dict(), error_code=None if report.ok else "INFRA_COMPOSE_INVALID"
            )
        if call.tool == ToolName.compose_up_sandbox and isinstance(args, ComposeArgs):
            path = await asyncio.to_thread(workspace_file, root, args.path)
            sandbox = await self.tools.compose_up_sandbox(path, timeout=float(args.timeout_seconds), host=self.default_host, ctx=ctx)
            return OpsResult(
                sandbox.ok, self._clip(sandbox.render()), data=sandbox.to_dict(), error_code=None if sandbox.ok else sandbox.code
            )
        if call.tool == ToolName.nginx_test and isinstance(args, ConfigSourceArgs):
            text, source = await self._config(root, args)
            tested = await self.tools.nginx.test(text, source=source, ctx=ctx)
            return OpsResult(tested.ok, self._clip(tested.render()), data=tested.to_dict(), error_code=tested.code)
        if call.tool == ToolName.proxy_config_validate and isinstance(args, ConfigSourceArgs):
            text, source = await self._config(root, args)
            validated = await self.tools.proxy_config_validate(
                text, source=source, sse_paths=("/api/events/stream", "/api/jobs/x/events/stream")
            )
            return OpsResult(
                validated.ok,
                self._clip(validated.render()),
                data=validated.to_dict(),
                error_code=None if validated.ok else "INFRA_PROXY_INVALID",
            )
        raise InfraToolError(f"unexpected arguments for {call.tool.value}", code=INFRA_INVALID_ARGUMENT)

    @staticmethod
    async def _config(root: Path, args: ConfigSourceArgs) -> tuple[str, str]:
        if args.config is not None:
            return args.config, "<config>"
        assert args.path is not None
        path = await asyncio.to_thread(workspace_file, root, args.path)

        def read() -> str:
            if path.stat().st_size > MAX_CONFIG_BYTES:
                raise InfraToolError(f"'{args.path}' is larger than {MAX_CONFIG_BYTES} bytes", code=INFRA_INVALID_ARGUMENT)
            try:
                return path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise InfraToolError(f"'{args.path}' is not UTF-8 text", code=INFRA_INVALID_ARGUMENT) from None

        return await asyncio.to_thread(read), args.path


class _ToolsWithCleanup(InfraTools):
    """:class:`InfraTools` plus the scheduler start hook ``cleanup_orphan_sandboxes`` (``hermclaw.runtime.ops``)."""

    default_host: str | None = None

    async def cleanup_orphan_sandboxes(self) -> list[str]:
        """Remove sandbox containers of crashed runs (label ``hermclaw.sandbox``) on the execution host."""
        try:
            return await cleanup_stale_sandboxes(self.executor, host=self.default_host)
        except InfraToolError:
            return []


def build_provider(
    config: HermclawConfig,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    ssh_runner: RemoteRunner | None = None,
) -> InfraOpsProvider:
    """Factory used by ``hermclaw.runtime.ops``: SSH runner for remote hosts, artifacts below ``settings.artifacts_dir``."""
    kwargs: dict[str, Any] = {
        "hosts": config.hosts,
        "remote": ssh_runner,
        "sessionmaker": sessionmaker,
        "artifacts_dir": settings.artifacts_dir,
    }
    tools = _ToolsWithCleanup.create(**kwargs)
    assert isinstance(tools, _ToolsWithCleanup)
    tools.default_host = default_execution_host(config.hosts, settings, remote_available=ssh_runner is not None)
    return InfraOpsProvider(tools, default_host=tools.default_host, output_chars=config.policies.coder.tool_output_chars)


__all__ = ["InfraOpsProvider", "build_provider", "default_execution_host", "workspace_file"]
