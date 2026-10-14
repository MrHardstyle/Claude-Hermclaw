"""Container and reverse-proxy tools (Bauplan §31, P28).

Tools: ``container_list`` / ``container_inspect`` (:mod:`.containers`), ``compose_validate`` (:mod:`.compose`),
``compose_up_sandbox`` (:mod:`.compose_sandbox`), ``nginx_test`` / ``proxy_config_validate`` (:mod:`.nginx`), the port
collision check (:mod:`.ports`) and rollback artifacts / guarded config deployments (:mod:`.rollback`).

:class:`InfraTools` wires them to one :class:`~hermclaw.infratools.process.HostExecutor` (local subprocesses or the
SSH runner for registered hosts) and offers :meth:`InfraTools.predeploy_check` – the §31 gate "syntax, port collision,
existing service inventory" that runs before :meth:`InfraTools.deploy_config` (which adds the rollback config).
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import HostsConfig
from hermclaw.core.redaction import DEFAULT_REDACTOR, Redactor
from hermclaw.infratools.compose import ComposeEngines, ComposePolicy, ComposeValidationReport, ComposeValidator
from hermclaw.infratools.compose_sandbox import ComposeSandbox, SandboxOptions, SandboxReport
from hermclaw.infratools.containers import ContainerInfo, ContainerInventory, ContainerListResult, EngineChoice
from hermclaw.infratools.errors import INFRA_INVALID_ARGUMENT, INFRA_STORE_UNAVAILABLE, Finding, InfraToolError
from hermclaw.infratools.nginx import (
    NginxTester,
    NginxTestReport,
    ProxyConfigValidator,
    ProxyValidationReport,
    Resolver,
    UpstreamPolicy,
)
from hermclaw.infratools.ports import PortChecker, PortCheckReport, PortPlan, addresses_overlap
from hermclaw.infratools.process import CommandAudit, HostExecutor, InfraContext, RemoteRunner
from hermclaw.infratools.rollback import (
    DEFAULT_DEPLOY_ROOTS,
    ConfigDeployer,
    DeploymentReport,
    RestoreReport,
    RollbackArtifact,
    RollbackStore,
    TargetFiles,
)

PredeployKind = Literal["nginx", "compose", "systemd"]


@dataclass
class PredeployReport:
    """Result of the §31 pre-deployment gate for one configuration."""

    kind: PredeployKind
    host: str
    syntax: list[dict[str, Any]] = field(default_factory=list)  # one entry per syntax/structure check
    ports: PortCheckReport | None = None
    inventory: ContainerListResult | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        syntax_ok = all(s.get("ok") or s.get("status") == "skipped" for s in self.syntax)
        ports_ok = self.ports is None or self.ports.ok
        return syntax_ok and ports_ok and not any(f.severity == "error" for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "host": self.host,
            "syntax": self.syntax,
            "ports": self.ports.to_dict() if self.ports else None,
            "inventory": self.inventory.model_dump(mode="json") if self.inventory else None,
            "findings": [f.to_dict() for f in self.findings],
        }

    def render(self, limit: int = 12000) -> str:
        lines = [f"predeploy check ({self.kind}) for {self.host}: {'OK' if self.ok else 'BLOCKED'}"]
        for s in self.syntax:
            state = "skipped" if s.get("status") == "skipped" else ("ok" if s.get("ok") else "FAILED")
            lines.append(f"- {s.get('check')}: {state}" + (f" ({s.get('reason')})" if s.get("reason") else ""))
        if self.ports is not None:
            lines.append(self.ports.render(4000))
        if self.inventory is not None:
            running = [c.summary() for c in self.inventory.containers if c.running]
            lines.append(f"inventory: {len(running)} running container(s)" + (": " + "; ".join(running[:20]) if running else ""))
        lines.extend(f.render() for f in self.findings)
        return "\n".join(lines)[:limit]


class InfraTools:
    """Facade over the P28 tools sharing one executor (see module docstring)."""

    def __init__(
        self,
        executor: HostExecutor,
        *,
        hosts: HostsConfig | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        artifacts_dir: Path | None = None,
        deploy_roots: tuple[str, ...] = DEFAULT_DEPLOY_ROOTS,
        upstream_allowlist: tuple[str, ...] = (),
        resolver: Resolver | None = None,
        sandbox_options: SandboxOptions | None = None,
        port_plan: PortPlan | None = None,
        sudo: bool = False,
        redactor: Redactor = DEFAULT_REDACTOR,
    ) -> None:
        self.executor = executor
        self.hosts = hosts if hosts is not None else executor.hosts
        self.redactor = redactor
        self.inventory = ContainerInventory(executor, redactor=redactor)
        self.engines = ComposeEngines(executor)
        self.compose = ComposeValidator(executor, self.engines)
        self.sandbox = ComposeSandbox(executor, self.engines, options=sandbox_options, redactor=redactor)
        self.ports = PortChecker(executor, hosts=self.hosts, plan=port_plan)
        self.nginx = NginxTester(executor, redactor=redactor)
        self.proxy = ProxyConfigValidator(upstreams=UpstreamPolicy.from_hosts(self.hosts, upstream_allowlist), resolver=resolver)
        self.rollbacks: RollbackStore | None = None
        self.deployer: ConfigDeployer | None = None
        if sessionmaker is not None and artifacts_dir is not None:
            files = TargetFiles(executor, sudo=sudo)
            self.rollbacks = RollbackStore(sessionmaker, artifacts_dir, files, allowed_roots=deploy_roots, redactor=redactor)
            self.deployer = ConfigDeployer(self.rollbacks)

    @classmethod
    def create(
        cls,
        *,
        hosts: HostsConfig | None = None,
        remote: RemoteRunner | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        artifacts_dir: Path | None = None,
        audit: bool = True,
        **kwargs: Any,
    ) -> InfraTools:
        """Production wiring: ``remote`` = the SSH runner, ``artifacts_dir`` = ``settings.artifacts_dir``."""
        executor = HostExecutor(
            remote=remote, hosts=hosts, audit=CommandAudit(sessionmaker) if (audit and sessionmaker is not None) else None
        )
        return cls(executor, hosts=hosts, sessionmaker=sessionmaker, artifacts_dir=artifacts_dir, **kwargs)

    # ------------------------------------------------------------------------------------------ 28.1
    async def container_list(
        self,
        *,
        host: str | None = None,
        engine: EngineChoice = "auto",
        all_containers: bool = False,
        filters: tuple[str, ...] = (),
        ctx: InfraContext | None = None,
    ) -> ContainerListResult:
        return await self.inventory.list_containers(host=host, engine=engine, all_containers=all_containers, filters=filters, ctx=ctx)

    async def container_inspect(
        self, name: str, *, host: str | None = None, engine: EngineChoice = "auto", ctx: InfraContext | None = None
    ) -> ContainerInfo:
        return await self.inventory.inspect(name, host=host, engine=engine, ctx=ctx)

    # ------------------------------------------------------------------------------------------ 28.2 / 28.3
    async def compose_validate(
        self, path: Path, *, policy: ComposePolicy | None = None, engine: bool = True, ctx: InfraContext | None = None
    ) -> ComposeValidationReport:
        return await self.compose.validate(path, policy=policy, engine=engine, ctx=ctx)

    async def compose_up_sandbox(
        self,
        path: Path,
        *,
        timeout: float = 300.0,  # noqa: ASYNC109 – bounds the run; teardown has its own budget
        host: str | None = None,
        ctx: InfraContext | None = None,
    ) -> SandboxReport:
        return await self.sandbox.run(path, timeout=timeout, host=host, ctx=ctx)

    # ------------------------------------------------------------------------------------------ 28.4
    async def port_check(
        self,
        text: str,
        *,
        kind: Literal["auto", "compose", "nginx", "systemd"] = "auto",
        host: str | None = None,
        owner: str | None = None,
        live: bool = True,
        ctx: InfraContext | None = None,
    ) -> PortCheckReport:
        return await self.ports.check(text, kind=kind, host=host, owner=owner, live=live, ctx=ctx)

    # ------------------------------------------------------------------------------------------ 28.5
    async def nginx_test(self, config: str | None = None, *, path: Path | None = None, ctx: InfraContext | None = None) -> NginxTestReport:
        return await self.nginx.test(config, path=path, ctx=ctx)

    async def proxy_config_validate(
        self, config: str, *, source: str = "<config>", sse_paths: tuple[str, ...] = ()
    ) -> ProxyValidationReport:
        return await self.proxy.validate(config, source=source, sse_paths=sse_paths)

    # ------------------------------------------------------------------------------------------ 28.6
    def _store(self) -> RollbackStore:
        if self.rollbacks is None:
            raise InfraToolError("the rollback artifact store is not configured (database + artifacts_dir)", code=INFRA_STORE_UNAVAILABLE)
        return self.rollbacks

    async def rollback_create(
        self, target_path: str, *, host: str | None = None, kind: str = "config", ctx: InfraContext | None = None
    ) -> RollbackArtifact:
        return await self._store().create(target_path, host=host, deployment_kind=kind, ctx=ctx)

    async def rollback_restore(self, artifact_id: str, *, reload: bool = True, ctx: InfraContext | None = None) -> RestoreReport:
        return await self._store().restore(artifact_id, reload=reload, ctx=ctx)

    async def deploy_config(
        self,
        target_path: str,
        content: str | bytes,
        *,
        host: str | None = None,
        kind: str = "config",
        mode: int | None = None,
        check_argv: list[str] | None = None,
        reload_argv: list[str] | None = None,
        ctx: InfraContext | None = None,
    ) -> DeploymentReport:
        self._store()
        assert self.deployer is not None
        return await self.deployer.deploy(
            target_path, content, host=host, kind=kind, mode=mode, check_argv=check_argv, reload_argv=reload_argv, ctx=ctx
        )

    # ------------------------------------------------------------------------------------------ §31 gate
    async def predeploy_check(
        self,
        kind: PredeployKind,
        text: str,
        *,
        host: str | None = None,
        owner: str | None = None,
        live_ports: bool = True,
        inventory: bool = True,
        compose_dir: Path | None = None,
        sse_paths: tuple[str, ...] = (),
        ctx: InfraContext | None = None,
    ) -> PredeployReport:
        """Syntax (nginx -t + proxy rules | compose validation), port collisions and the host's container inventory.

        ``compose_dir`` is the project directory a compose text belongs to (relative paths are checked there);
        without it the text is validated in an empty temporary directory.
        """
        if kind not in ("nginx", "compose", "systemd"):
            raise InfraToolError(f"unknown config kind '{kind}'", code=INFRA_INVALID_ARGUMENT)
        target = self.executor.check_host(host) if live_ports or inventory else (host or "local")
        report = PredeployReport(kind, target)
        if kind == "nginx":
            proxy = await self.proxy.validate(text, sse_paths=sse_paths)
            report.syntax.append({"check": "proxy_config_validate", **proxy.to_dict()})
            tested = await self.nginx.test(text, ctx=ctx)
            report.syntax.append({"check": "nginx_test", **tested.to_dict()})
        elif kind == "compose":
            try:
                validated = await self._validate_compose_text(text, compose_dir, ctx)
                report.syntax.append({"check": "compose_validate", **validated.to_dict()})
            except InfraToolError as exc:
                report.syntax.append({"check": "compose_validate", "ok": False, "reason": exc.message, "code": exc.code})
        else:
            report.syntax.append({"check": "systemd", "ok": True, "status": "skipped", "reason": "no syntax check for unit files"})
        try:
            report.ports = await self.ports.check(text, kind=kind, host=host, owner=owner, live=live_ports, ctx=ctx)
        except InfraToolError as exc:  # unparsable config – already reported by the syntax checks
            report.findings.append(Finding("error", "PORT_CHECK_FAILED", exc.message))
        if inventory:
            report.inventory = await self.inventory.list_containers(host=host, ctx=ctx)
            if report.inventory.ok and report.ports is not None:
                report.findings.extend(_inventory_collisions(report.inventory, report.ports, owner))
        return report

    async def _validate_compose_text(self, text: str, compose_dir: Path | None, ctx: InfraContext | None) -> ComposeValidationReport:
        if compose_dir is not None:
            directory = compose_dir
            tmp: Path | None = None
        else:
            tmp = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="hermclaw-compose-"))
            directory = tmp
        name = f".hermclaw-predeploy-{uuid.uuid4().hex[:12]}.compose.yaml"
        path = directory / name
        try:
            await asyncio.to_thread(path.write_text, text, "utf-8")
            return await self.compose.validate(path, ctx=ctx)
        finally:
            await asyncio.to_thread(path.unlink, True)
            if tmp is not None:
                await asyncio.to_thread(shutil.rmtree, tmp, True)


def _inventory_collisions(inventory: ContainerListResult, ports: PortCheckReport, owner: str | None) -> list[Finding]:
    """Running containers that already publish a requested host port (another engine than the live socket check sees,
    e.g. rootless Podman behind a user namespace) – unless the container belongs to ``owner``."""
    findings: list[Finding] = []
    for container in inventory.containers:
        if not container.running:
            continue
        project = container.labels.get("com.docker.compose.project") or container.labels.get("io.podman.compose.project")
        if owner and owner in (container.name, project):
            continue
        for mapping in container.ports:
            for req in ports.requested:
                if mapping.host_port == req.port and mapping.protocol == req.protocol and addresses_overlap(mapping.host_ip, req.address):
                    findings.append(
                        Finding(
                            "error",
                            "PORT_USED_BY_CONTAINER",
                            f"host port {req.port}/{req.protocol} is published by container {container.name} ({container.engine})",
                            location=req.source,
                        )
                    )
    return findings


__all__ = [
    "InfraTools",
    "PredeployKind",
    "PredeployReport",
]
