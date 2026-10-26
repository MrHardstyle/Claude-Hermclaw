"""Composition root of the scheduler process (``hermclaw scheduler``, host ``.225``).

:func:`build_services` wires every runtime component from ``HermclawConfig`` + ``Settings``:

=====================  ==========================================================================================
model gateway          :class:`LiteLLMGateway` (ChatModel + EmbeddingModel; fixed roles Gemma/Qwen3-Coder/…)
git                    :class:`GitEngine` (the only git writer; GitLab client from settings)
repository context     :class:`RepoIntelligence` (inventory, lexical/symbol/vector search, reads)
scope                  :class:`ScopeEngine`, :class:`ScopeAuditor`, :class:`ScopeExpansionHandler`
planning               :class:`Planner` / :class:`Replanner` (Gemma) inside :class:`RuntimeJobDriver`
execution              remote sandbox on the execution worker ``.222`` (production) or the local sandbox
                       (development/tests) – :class:`ExecutionPool`
verification/review    :class:`VerifierAdapter`, :class:`WorkspaceReviewer` (Qwen3.8 27B)
resources              :class:`ResourceManager` (leases, budgets, preemption, crash recovery)
research               :class:`ResearchEngine` (SearXNG + SSRF-guarded fetch)
media                  :class:`MediaStepHandler` with the model/media worker ``.224``
workers                :class:`WorkerRegistry`, token store, optional Wake-on-LAN controller
=====================  ==========================================================================================

Tests inject fakes through :class:`ServiceOverrides` (scripted models, local executors); nothing else differs from
production wiring. :meth:`RuntimeServices.aclose` releases every owned client.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.coder.handler import ExecutorFactory, ImplementDeps, ImplementStepHandler
from hermclaw.coder.loop import CoderSettings
from hermclaw.contracts.step import StepContract
from hermclaw.core.config import HermclawConfig
from hermclaw.core.interfaces import CommandExecutor, RepoContextProvider, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.core.settings import Settings
from hermclaw.gitops.engine import GitEngine
from hermclaw.media.handler import MediaSettings, MediaStepHandler
from hermclaw.memory.store import MemoryStore
from hermclaw.models.protocols import ChatModel, EmbeddingModel
from hermclaw.planner.planner import Planner
from hermclaw.planner.replanner import Replanner
from hermclaw.resources.manager import ResourceManager
from hermclaw.review.reviewer import HeavyReviewer
from hermclaw.review.workspace import WorkspaceReviewer
from hermclaw.runtime.adapters import VerifierAdapter
from hermclaw.runtime.driver import DriverSettings, RuntimeJobDriver
from hermclaw.runtime.handlers import RepoInfoStepHandler, ReviewStepHandler, VerifyStepHandler, _workspace
from hermclaw.runtime.health import DiskMonitor
from hermclaw.runtime.images import ImageDefaultingExecutor, SandboxImageSelector
from hermclaw.runtime.ops import OpsBundle, WorkspaceExecutor, build_ops
from hermclaw.runtime.research_step import ResearchRunner, ResearchStepHandler
from hermclaw.runtime.scratch import ScratchWorkspaces
from hermclaw.scheduler.handlers import StepHandler, StepRunContext
from hermclaw.scope.audit import ScopeAuditor
from hermclaw.scope.engine import ScopeEngine
from hermclaw.scope.expansion import ScopeExpansionHandler
from hermclaw.stagnation.monitor import make_stagnation_factory
from hermclaw.tools.executors import LocalSandboxExecutor
from hermclaw.tools.ops import OpsToolProvider
from hermclaw.tools.remote import RemoteSandboxExecutor
from hermclaw.workers.auth import RefTokenStore, TokenStore
from hermclaw.workers.client import ExecutionWorkerClient, MediaWorkerClient, WorkerClient
from hermclaw.workers.errors import WorkerUnreachable
from hermclaw.workers.registry import WorkerRegistry

log = get_logger(__name__)

ExecutionMode = Literal["auto", "remote", "local"]
SANDBOX_CAPABILITY = "sandbox"  # base capability every execution worker daemon reports
MEDIA_CAPABILITIES = ("video", "image")


class WakePort(Protocol):
    """The part of :class:`hermclaw.wol.readiness.WakeController` the runtime needs."""

    async def ensure_worker_ready(
        self, worker_id: str, job_id: uuid.UUID | None = None, *, required_capabilities: Any = None, deadline_seconds: float | None = None
    ) -> Any: ...


@dataclass
class ServiceOverrides:
    """Test/dev seams. ``None`` means production wiring."""

    chat: ChatModel | None = None
    embedder: EmbeddingModel | None = None
    repo: RepoContextProvider | None = None
    executor_factory: ExecutorFactory | None = None
    research: ResearchRunner | None = None
    research_enabled: bool | None = None
    gitlab: Any = "auto"
    media_client_factory: Callable[[], Awaitable[Any]] | None = None
    wake: WakePort | None = None
    token_store: TokenStore | None = None
    coder_settings: CoderSettings | None = None
    driver_settings: DriverSettings | None = None
    media_settings: MediaSettings | None = None
    lease_keeper_interval: float | None = None
    worker_client_kwargs: dict[str, Any] | None = None  # e.g. an ASGI transport for in-process worker daemons
    ops: OpsToolProvider | None = None  # operations tools provider (SSH/DB/container, P26–P28)


# ----------------------------------------------------------------------------------------------- worker pools
class WorkerPool:
    """Selects a dispatchable worker for a capability (waking it via Wake-on-LAN when configured) and keeps one
    signed HTTP client per worker for the lifetime of the process."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        registry: WorkerRegistry,
        token_store: TokenStore,
        *,
        kind: str,
        client_cls: type[WorkerClient],
        wake: WakePort | None = None,
        client_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.sm, self.registry, self.token_store = sessionmaker, registry, token_store
        self.kind, self.client_cls, self.wake = kind, client_cls, wake
        self.client_kwargs = client_kwargs or {}
        self._clients: dict[str, WorkerClient] = {}

    async def _select(self, capabilities: tuple[str, ...]) -> Any:
        async with self.sm() as s:
            for cap in capabilities:
                info = await self.registry.select_worker(s, cap, kind=self.kind, include_busy=True)
                if info is not None:
                    return info
        return None

    async def client(self, capabilities: tuple[str, ...], *, job_id: uuid.UUID | None = None) -> WorkerClient:
        info = await self._select(capabilities)
        if info is None and self.wake is not None:
            async with self.sm() as s:
                candidates = []
                for cap in capabilities:
                    candidates += await self.registry.workers_for_capability(s, cap, kind=self.kind)
            for cand in dict.fromkeys(c.id for c in candidates):
                result = await self.wake.ensure_worker_ready(cand, job_id, required_capabilities=[capabilities[0]])
                if getattr(result, "ready", False):
                    info = await self._select(capabilities)
                    if info is not None:
                        break
        if info is None:
            raise WorkerUnreachable(
                f"no ready {self.kind} worker with capability {'/'.join(capabilities)}",
                details={"kind": self.kind, "capabilities": list(capabilities)},
            )
        cached = self._clients.get(info.id)
        if cached is None or cached.base_url != (info.api_url or "").rstrip("/"):
            if cached is not None:
                await cached.aclose()
            cached = self.client_cls.for_worker(info, self.token_store, **self.client_kwargs)
            self._clients[info.id] = cached
        return cached

    async def aclose(self) -> None:
        for c in self._clients.values():
            with contextlib.suppress(Exception):
                await c.aclose()
        self._clients.clear()


class ExecutionPool:
    """Executor factory: remote sandbox on an execution worker, or the local sandbox (development/tests)."""

    def __init__(self, config: HermclawConfig, settings: Settings, *, mode: ExecutionMode, workers: WorkerPool | None) -> None:
        self.config, self.settings, self.workers = config, settings, workers
        self.mode: Literal["remote", "local"] = (
            ("remote" if settings.env == "production" else "local") if mode == "auto" else mode  # type: ignore[assignment]
        )
        if self.mode == "remote" and workers is None:
            raise ValueError("remote execution needs a worker pool")

    async def executor(self, handle: WorkspaceHandle, step: StepContract | None) -> CommandExecutor:
        if self.mode == "local":
            return LocalSandboxExecutor(self.config.policies.sandbox, settings=self.settings)
        assert self.workers is not None
        client = await self.workers.client((SANDBOX_CAPABILITY,), job_id=step.job_id if step is not None else None)
        assert isinstance(client, ExecutionWorkerClient)
        return RemoteSandboxExecutor(client, self.config.policies.sandbox)


# ----------------------------------------------------------------------------------------------- the services
@dataclass
class RuntimeServices:
    config: HermclawConfig
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    chat: ChatModel
    git: GitEngine
    repo: RepoContextProvider
    resources: ResourceManager
    registry: WorkerRegistry
    token_store: TokenStore
    execution: ExecutionPool
    verifier: VerifierAdapter
    reviewer: WorkspaceReviewer
    research: ResearchRunner | None
    driver: RuntimeJobDriver
    handlers: list[StepHandler]
    holder_id: str
    wake: WakePort | None = None
    disk: DiskMonitor | None = None
    ops: OpsBundle | None = None  # operations tools (provider, SSH runner for the idle-sleep hook, SSH approvals)
    memory: MemoryStore | None = None  # Hermclaw's knowledge (D-019)
    _closers: list[Callable[[], Awaitable[Any]]] = field(default_factory=list)

    @property
    def handled_kinds(self) -> list[str]:
        return sorted({k for h in self.handlers for k in h.kinds})

    def admission_block(self) -> str | None:
        """Scheduler gate: why no new job may start preparing right now (``None`` = admit)."""
        return self.disk.admission_block() if self.disk is not None else None

    async def aclose(self) -> None:
        for close in reversed(self._closers):
            try:
                await close()
            except Exception as exc:  # pragma: no cover - best effort shutdown
                log.warning("service close failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        self._closers.clear()


class _ResearchPort:
    """``ImplementDeps.research`` adapter: the coder's ``request_research`` tool and stagnation escalation."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    async def ask(self, question: str, *, job_id: uuid.UUID, step_id: uuid.UUID) -> str:
        from hermclaw.research.engine import format_for_worker

        if hasattr(self.engine, "answer_for_worker"):
            return str(await self.engine.answer_for_worker(question, job_id=job_id, step_id=step_id))
        out = await self.engine.run_detailed(question, job_id=job_id, step_id=step_id)
        return format_for_worker(out.contract)


def resource_holder_id(settings: Settings) -> str:
    """Stable per instance (``scheduler@<instance>``): a restarted scheduler recovers its own leases and steps."""
    return f"scheduler@{settings.instance_id}"


def build_services(
    config: HermclawConfig,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    overrides: ServiceOverrides | None = None,
    execution_mode: ExecutionMode = "auto",
    holder_id: str | None = None,
    artifacts_dir: Path | None = None,
) -> RuntimeServices:
    ov = overrides or ServiceOverrides()
    closers: list[Callable[[], Awaitable[Any]]] = []
    holder = holder_id or resource_holder_id(settings)

    # models
    if ov.chat is None:
        from hermclaw.models.gateway import LiteLLMGateway

        gateway = LiteLLMGateway.from_config(config, session_factory=sessionmaker)
        closers.append(gateway.aclose)
        chat: ChatModel = gateway
        embedder: EmbeddingModel | None = ov.embedder or gateway
    else:
        chat = ov.chat
        embedder = ov.embedder

    # knowledge (D-019): recall tool, lessons in prompts, learning after jobs
    memory = MemoryStore(sessionmaker, embedder, config.policies.memory) if config.policies.memory.enabled else None

    # git + repository context
    git = GitEngine.from_config(config, settings, sessionmaker, gitlab=ov.gitlab)
    if ov.repo is None:
        from hermclaw.repo_intelligence import RepoIntelConfig, RepoIntelligence

        repo: RepoContextProvider = RepoIntelligence(sessionmaker, embedder=embedder, config=RepoIntelConfig.from_hermclaw(config))
    else:
        repo = ov.repo

    # workers, resources, execution
    registry = WorkerRegistry()
    token_store = ov.token_store or RefTokenStore(settings.worker_token_refs)
    ckw = ov.worker_client_kwargs
    exec_workers = WorkerPool(
        sessionmaker, registry, token_store, kind="execution", client_cls=ExecutionWorkerClient, wake=ov.wake, client_kwargs=ckw
    )
    media_workers = WorkerPool(
        sessionmaker, registry, token_store, kind="model", client_cls=MediaWorkerClient, wake=ov.wake, client_kwargs=ckw
    )
    closers += [exec_workers.aclose, media_workers.aclose]
    resources = ResourceManager.from_config(sessionmaker, config, holder)
    execution = ExecutionPool(config, settings, mode=execution_mode, workers=exec_workers)
    images = SandboxImageSelector(sessionmaker, config.policies.sandbox, repo)  # toolchain image per job (R-022)

    async def executor_for(ws: WorkspaceHandle, step: StepContract | None) -> CommandExecutor:
        if ov.executor_factory is not None:  # test seam: also for the step-less regression rerun
            base = await ov.executor_factory(ws, step)  # type: ignore[arg-type]
        else:
            base = await execution.executor(ws, step)
        return ImageDefaultingExecutor(base, await images.image_for(ws), images.image_for, config.policies.sandbox.images)

    async def executor_factory(ws: WorkspaceHandle, step: StepContract) -> CommandExecutor:
        return await executor_for(ws, step)

    verifier = VerifierAdapter(sessionmaker, config, git.reader(), executor_for)
    reviewer = WorkspaceReviewer(HeavyReviewer(chat, sessionmaker, config), git.reader(), repo, memory)

    # research (P12): SearXNG on .225 unless disabled by policy or override
    research: ResearchRunner | None = ov.research
    research_enabled = ov.research_enabled if ov.research_enabled is not None else config.policies.research.search_provider != "none"
    if research is None and research_enabled:
        from hermclaw.research.engine import build_research_engine

        policy = config.policies.research
        engine = build_research_engine(
            sessionmaker, chat, config, render_js=bool(policy.browser_render), chromium=policy.browser_executable or None
        )
        closers.append(engine.aclose)
        research = engine
    if not research_enabled:
        research = None

    async def workspace_for(ctx: StepRunContext) -> WorkspaceHandle | None:
        return await _workspace(git, ctx.job_id, step_baseline=True)

    scratch = ScratchWorkspaces(sessionmaker, git, Path(settings.data_dir) / "scratch")  # repository-less jobs (R-026)
    # operations tools (P26–P28): SSH (+ database/container providers when installed); tests may inject their own
    ops_bundle = (
        OpsBundle(ov.ops) if ov.ops is not None else build_ops(config, sessionmaker, settings, executor=WorkspaceExecutor(executor_for))
    )
    artifacts = artifacts_dir or Path(settings.artifacts_dir)

    deps = ImplementDeps(
        git=git,
        scope_engine=ScopeEngine(sessionmaker, config, repo),
        scope_auditor=ScopeAuditor(sessionmaker, config),
        expansion=ScopeExpansionHandler(sessionmaker, config, repo),
        chat=chat,
        repo=repo,
        executor_factory=executor_factory,
        verifier=verifier,
        reviewer=reviewer,
        research=_ResearchPort(research) if research is not None else None,
        stagnation_factory=make_stagnation_factory(
            git=git.reader(), workspace_for=workspace_for, research_available=research is not None, heavy_review_available=True
        ),
        coder_settings=ov.coder_settings or CoderSettings(),
        resources=resources,
        scratch=scratch,
        ops=ops_bundle.provider,
        artifacts_dir=artifacts,
        memory=memory,
    )
    if ov.lease_keeper_interval is not None:
        deps = dataclasses.replace(deps, lease_keeper_interval=ov.lease_keeper_interval)

    async def media_client() -> Any:
        if ov.media_client_factory is not None:
            return await ov.media_client_factory()
        return await media_workers.client(MEDIA_CAPABILITIES)

    handlers: list[StepHandler] = [
        ImplementStepHandler(sessionmaker, config, deps),
        VerifyStepHandler(sessionmaker, config, git=git, verifier=verifier, scratch=scratch),
        ReviewStepHandler(sessionmaker, config, git=git, verifier=verifier, reviewer=reviewer, resources=resources),
        RepoInfoStepHandler(sessionmaker, config, git=git, repo=repo),
        MediaStepHandler(
            sessionmaker,
            config,
            chat=chat,
            resources=resources,
            client_factory=media_client,
            git=git,
            settings=ov.media_settings or MediaSettings(),
            artifacts_dir=artifacts,
        ),
    ]
    if research is not None:
        handlers.append(ResearchStepHandler(sessionmaker, config, engine=research))

    driver = RuntimeJobDriver(
        sessionmaker,
        config,
        planner=Planner(chat, sessionmaker, config),
        replanner=Replanner(chat, sessionmaker, config),
        git=git,
        repo_intel=repo,
        chat=chat,
        regression=verifier,
        settings=ov.driver_settings or DriverSettings(artifacts_dir=artifacts),
        scratch=scratch,
        resources=resources,
        memory=memory,
    )
    disk = DiskMonitor(
        sessionmaker,
        [Path(settings.workspaces_dir), Path(settings.repos_cache_dir), artifacts],
        warn_free_mb=settings.disk_warn_free_mb,
        critical_free_mb=settings.disk_critical_free_mb,
        interval_seconds=settings.disk_check_seconds,
        source_id=holder,
    )
    return RuntimeServices(
        config=config,
        settings=settings,
        sessionmaker=sessionmaker,
        chat=chat,
        git=git,
        repo=repo,
        resources=resources,
        registry=registry,
        token_store=token_store,
        execution=execution,
        verifier=verifier,
        reviewer=reviewer,
        research=research,
        driver=driver,
        handlers=handlers,
        holder_id=holder,
        wake=ov.wake,
        disk=disk,
        ops=ops_bundle,
        memory=memory,
        _closers=closers,
    )


__all__ = [
    "MEDIA_CAPABILITIES",
    "SANDBOX_CAPABILITY",
    "ExecutionMode",
    "ExecutionPool",
    "RuntimeServices",
    "ServiceOverrides",
    "WakePort",
    "WorkerPool",
    "build_services",
    "resource_holder_id",
]
