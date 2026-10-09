"""Targeted reads of line ranges (P11 11.11).

The returned text is the *raw* file content of whole lines ``start..end`` (no line-number prefixes, original line
endings) so callers can treat it line-accurately; :meth:`ReadResult.numbered` renders a numbered view for humans.
A character budget cuts at line boundaries (a single over-long first line is clipped). Paths are confined to the
workspace (no ``..``, no symlink escapes, no ``.git``, no secret files); binary and oversized files are refused.
Redaction is the caller's job (tool output / prompt building) because exact content is needed for patches.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from hermclaw.core.errors import ValidationFailed
from hermclaw.repo_intelligence.config import RepoIntelConfig
from hermclaw.repo_intelligence.fileio import decode_text, read_bytes
from hermclaw.repo_intelligence.languages import is_binary_name, looks_binary
from hermclaw.repo_intelligence.paths import RepoPathError, resolve_in_root


@dataclass(frozen=True)
class ReadResult:
    path: str
    start_line: int
    end_line: int  # last line actually returned (start-1 when nothing was returned)
    total_lines: int
    text: str
    truncated: bool  # budget or range cut the requested range short
    size: int

    def numbered(self) -> str:
        width = len(str(max(self.end_line, 1)))
        return "".join(
            f"{self.start_line + i:>{width}} | {line}" + ("" if line.endswith("\n") else "\n")
            for i, line in enumerate(self.text.splitlines(keepends=True))
        )


class FileReader:
    def __init__(self, cfg: RepoIntelConfig | None = None) -> None:
        self.cfg = cfg or RepoIntelConfig()

    def read_sync(self, root: Path, path: str, start: int = 1, end: int | None = None, *, max_chars: int | None = None) -> ReadResult:
        norm, real = resolve_in_root(root, path, sensitive_globs=self.cfg.sensitive_globs)
        if not real.is_file():
            raise ValidationFailed(f"not a regular file: {norm}", code="REPO_NOT_A_FILE")
        size = os.path.getsize(real)
        if size > self.cfg.max_read_file_bytes:
            raise ValidationFailed(
                f"file too large for a targeted read: {norm} ({size} bytes > {self.cfg.max_read_file_bytes})",
                code="REPO_FILE_TOO_LARGE",
                details={"size": size},
            )
        rel_real = real.relative_to(Path(os.path.realpath(root))).as_posix()
        data = read_bytes(Path(os.path.realpath(root)), rel_real, size + 1)
        if data is None:
            raise RepoPathError(f"file is not readable: {norm}", code="REPO_FILE_UNREADABLE")
        if is_binary_name(norm) or looks_binary(data[:8192]):
            raise ValidationFailed(f"binary file: {norm}", code="REPO_FILE_BINARY")
        budget = self.cfg.read_default_max_chars if max_chars is None else max(1, int(max_chars))
        lines = decode_text(data).splitlines(keepends=True)
        total = len(lines)
        s = max(1, int(start or 1))
        e = total if end is None else min(total, int(end))
        if e < s or s > total:
            return ReadResult(norm, s, s - 1, total, "", truncated=False, size=size)
        out: list[str] = []
        used = 0
        truncated = False
        last = s - 1
        for i in range(s, e + 1):
            line = lines[i - 1]
            if used + len(line) > budget:
                if not out:  # an over-long first line is clipped rather than dropped
                    out.append(line[:budget])
                    last = i
                truncated = True
                break
            out.append(line)
            used += len(line)
            last = i
        return ReadResult(norm, s, last, total, "".join(out), truncated=truncated, size=size)

    async def read(self, root: Path, path: str, start: int = 1, end: int | None = None, *, max_chars: int | None = None) -> ReadResult:
        return await asyncio.to_thread(self.read_sync, root, path, start, end, max_chars=max_chars)
