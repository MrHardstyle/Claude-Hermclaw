"""Read-only git access for LLM tools (implements :class:`hermclaw.core.interfaces.GitReader`).

Nothing here mutates refs, the index or the working tree: status runs with ``GIT_OPTIONAL_LOCKS=0`` and the
diff uses a scratch index for untracked files. Output is redacted and size-capped before it reaches a prompt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from hermclaw.core.errors import PolicyViolation, ValidationFailed
from hermclaw.core.interfaces import GitStatusEntry, WorkspaceHandle
from hermclaw.core.redaction import DEFAULT_REDACTOR
from hermclaw.gitops import _fs, ops
from hermclaw.gitops.errors import WorkspacePathViolation, WorkspaceStateError, WorkspaceTamperedError
from hermclaw.gitops.runner import GitRunner
from hermclaw.gitops.scope_guard import first_match, gitattributes_lines, normalise_repo_path

MAX_LOG_ENTRIES = 200


class WorkspaceGitReader:
    def __init__(
        self,
        runner: GitRunner,
        *,
        workspaces_root: Path | None = None,
        max_patch_bytes: int = ops.DEFAULT_MAX_PATCH_BYTES,
        forbidden_globs: Sequence[str] = (),
    ) -> None:
        self.runner = runner
        self.workspaces_root = workspaces_root
        self.max_patch_bytes = max_patch_bytes
        self.forbidden_globs = list(forbidden_globs)
        self._attribute_lines = frozenset(gitattributes_lines(self.forbidden_globs))

    async def _path(self, workspace: WorkspaceHandle) -> Path:
        if not ops.is_sha(workspace.base_sha):
            raise ValidationFailed("workspace base_sha is not a commit SHA", details={"workspace_id": str(workspace.id)})
        path = Path(workspace.path)
        if self.workspaces_root is not None and not await asyncio.to_thread(_fs.is_within, path, self.workspaces_root, min_depth=2):
            raise WorkspacePathViolation("workspace path is outside the workspace root", details={"workspace_id": str(workspace.id)})
        if not await ops.is_work_tree(self.runner, path):
            raise WorkspaceStateError("workspace directory is missing or not a git work tree", details={"workspace_id": str(workspace.id)})
        problems = await ops.integrity_problems(self.runner, path)
        if self._attribute_lines:
            present = set((await asyncio.to_thread(_fs.read_text, path / ".git" / "info" / "attributes") or "").splitlines())
            if not self._attribute_lines <= present:
                problems.append("info/attributes no longer hides always_forbidden files")
        if problems:
            raise WorkspaceTamperedError(
                "workspace git metadata was modified outside the runtime; refusing to read it",
                details={"workspace_id": str(workspace.id), "problems": problems[:20]},
            )
        return path

    @staticmethod
    def _paths(paths: list[str] | None) -> list[str] | None:
        if not paths:
            return None
        out: list[str] = []
        for p in paths:
            norm = normalise_repo_path(p)
            if norm is None or ".git" in norm.split("/"):
                raise ValidationFailed(f"invalid repository path {p!r}", details={"path": p})
            out.append(norm)
        return out

    async def status(self, workspace: WorkspaceHandle) -> list[GitStatusEntry]:
        path = await self._path(workspace)
        entries = await ops.read_status(self.runner, path, renames=True)
        return [GitStatusEntry(path=e.path, status=e.index + e.worktree, orig_path=e.orig_path) for e in entries]

    async def diff(self, workspace: WorkspaceHandle, paths: list[str] | None = None, *, max_bytes: int = 200_000) -> str:
        """Unified diff against ``base_sha`` incl. commits, staged, unstaged and untracked files (redacted)."""
        path = await self._path(workspace)
        limit = max(0, min(int(max_bytes), self.max_patch_bytes))
        result = await ops.read_diff(self.runner, path, workspace.base_sha, paths=self._paths(paths), max_patch_bytes=limit)
        text = result.patch
        if result.patch_truncated:
            text += f"\n…[diff truncated at {limit} bytes; {len(result.files)} file(s) changed]\n"
        return text

    async def changed_files(self, workspace: WorkspaceHandle) -> list[str]:
        path = await self._path(workspace)
        return await ops.changed_files(self.runner, path, workspace.base_sha)

    async def log(self, workspace: WorkspaceHandle, *, max_count: int = 20) -> list[dict[str, str]]:
        """Commits on the job branch since ``base_sha`` (newest first): sha, author, date, subject."""
        path = await self._path(workspace)
        count = max(1, min(int(max_count), MAX_LOG_ENTRIES))
        res = await self.runner.run(
            ["log", f"--max-count={count}", "--format=%H%x1f%an%x1f%aI%x1f%s%x1e", f"{workspace.base_sha}..HEAD"], cwd=path
        )
        out: list[dict[str, str]] = []
        for record in res.text.split("\x1e"):
            fields = record.strip("\n").split("\x1f")
            if len(fields) == 4:
                out.append({"sha": fields[0], "author": fields[1], "date": fields[2], "subject": DEFAULT_REDACTOR.text(fields[3])})
        return out

    async def show_base_file(self, workspace: WorkspaceHandle, file_path: str, *, max_bytes: int = 200_000) -> str | None:
        """Content of ``file_path`` at ``base_sha`` (``None`` if it did not exist there), redacted and capped."""
        path = await self._path(workspace)
        rel = self._paths([file_path])
        assert rel is not None
        hit = first_match(rel[0], self.forbidden_globs)
        if hit:
            raise PolicyViolation(f"reading '{rel[0]}' is forbidden by policy", details={"path": rel[0], "pattern": hit})
        # cat-file prints the raw blob: no textconv/filters, whatever attributes say
        res = await self.runner.run(
            ["cat-file", "blob", f"{workspace.base_sha}:{rel[0]}"], cwd=path, check=False, max_stdout=max(0, int(max_bytes))
        )
        if res.returncode != 0:
            return None
        text = DEFAULT_REDACTOR.text(res.stdout.decode("utf-8", "replace"))
        return text + ("\n…[truncated]\n" if res.stdout_truncated else "")
