"""Sandbox image per job (R-022): the toolchain a step's commands run with.

Order of precedence for a workspace:

1. the repository's ``metadata.sandbox_image`` (``hermclaw repo-add … --sandbox-image ALIAS``) – a project image with
   the project's dependencies baked in (``scripts/ops/hermclaw-build-sandbox-images --project …``), so tests run
   offline in the network-less sandbox;
2. the repository's primary language mapped to a toolchain alias of ``policies.sandbox.images``
   (python → ``python``, typescript/javascript → ``node``, php → ``php``), when that alias is configured;
   a new repository without recognised code is recognised by its dependency manifest (``requirements.txt`` …);
3. ``None`` – the sandbox default image (``policies.sandbox.image``); asked again on the next command, because a
   step may create the project's first files (BUG-155).

Only aliases/images allowed by ``policies.sandbox`` are ever used; the sandbox re-checks the allow-list itself.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.contracts.worker import CommandResult
from hermclaw.core.config import SandboxPolicy
from hermclaw.core.interfaces import CommandExecutor, ExecutionRequest, RepoContextProvider, WorkspaceHandle
from hermclaw.core.logging import get_logger
from hermclaw.persistence.models import Repository, Workspace

log = get_logger(__name__)

#: dependency manifests that identify a project's toolchain before it has recognised source files
MANIFEST_IMAGE_ALIASES: tuple[tuple[str, str], ...] = (
    ("requirements.txt", "python"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("package.json", "node"),
    ("composer.json", "php"),
)

LANGUAGE_IMAGE_ALIASES: dict[str, str] = {
    "python": "python",
    "typescript": "node",
    "javascript": "node",
    "tsx": "node",
    "php": "php",
}


class SandboxImageSelector:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], policy: SandboxPolicy, repo: RepoContextProvider | None) -> None:
        self.sm, self.policy, self.repo = sessionmaker, policy, repo
        self._cache: dict[uuid.UUID, str | None] = {}

    def _allowed(self, image: str) -> bool:
        return image == self.policy.image or image in self.policy.images or image in self.policy.images.values()

    async def _repository_image(self, workspace_id: uuid.UUID) -> str | None:
        async with self.sm() as s:
            row = (
                await s.execute(
                    select(Repository.metadata_)
                    .join(Workspace, Workspace.repository_id == Repository.id)
                    .where(Workspace.id == workspace_id)
                )
            ).scalar_one_or_none()
        image = (row or {}).get("sandbox_image")
        return str(image) if image else None

    async def _language_alias(self, handle: WorkspaceHandle) -> str | None:
        if self.repo is None:
            return None
        try:
            summary: dict[str, Any] = await self.repo.inventory_summary(handle)
        except Exception as exc:  # inventory problems must not block execution: fall back to the default image
            log.warning("sandbox image: inventory unavailable", extra={"error": f"{type(exc).__name__}: {exc}"})
            return None
        for lang in summary.get("primary_languages") or []:
            alias = LANGUAGE_IMAGE_ALIASES.get(str(lang).lower())
            if alias and alias in self.policy.images:
                return alias
        return None

    def _manifest_alias(self, handle: WorkspaceHandle) -> str | None:
        root = Path(handle.path)
        for name, alias in MANIFEST_IMAGE_ALIASES:
            if alias in self.policy.images and (root / name).is_file():
                return alias
        return None

    async def image_for(self, handle: WorkspaceHandle) -> str | None:
        if handle.id in self._cache:
            return self._cache[handle.id]
        image = await self._repository_image(handle.id)
        if image is not None and not self._allowed(image):
            log.warning("sandbox image of the repository is not allowed by policies.sandbox; using the default", extra={"image": image})
            image = None
        if image is None:
            image = await self._language_alias(handle)
        if image is None:
            image = self._manifest_alias(handle)
        # A workspace without a recognised language (a new, empty repository) is asked again next time: the first
        # step may create the project's first Python file, and later steps then need the toolchain image (BUG-155).
        if image is not None:
            self._cache[handle.id] = image
        return image


class ImageDefaultingExecutor:
    """Executor wrapper: requests without an explicit image run with the job's selected image.

    With a ``resolve`` callback and no selection yet, every command asks again (a step that creates the project's
    first ``requirements.txt`` gets the Python toolchain for its next command, BUG-155)."""

    def __init__(
        self, inner: CommandExecutor, image: str | None, resolve: Callable[[WorkspaceHandle], Awaitable[str | None]] | None = None
    ) -> None:
        self.inner, self.image, self.resolve = inner, image, resolve

    async def run(self, workspace: WorkspaceHandle, req: ExecutionRequest) -> CommandResult:
        if self.image is None and self.resolve is not None:
            self.image = await self.resolve(workspace)
        if req.image is None and self.image is not None:
            req = dataclasses.replace(req, image=self.image)
        return await self.inner.run(workspace, req)

    def __getattr__(self, name: str) -> Any:  # sync_up/sync_down/drop/client of the wrapped executor
        return getattr(self.inner, name)


__all__ = ["LANGUAGE_IMAGE_ALIASES", "ImageDefaultingExecutor", "SandboxImageSelector"]
