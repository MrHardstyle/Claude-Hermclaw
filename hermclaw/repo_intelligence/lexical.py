"""Lexical search (Bauplan §14 Phase B; P11 11.4).

ripgrep (``rg --json``) with fixed-string or regex patterns, include/exclude globs, per-file and total result
limits and a hard timeout; patterns are passed as separate argv entries (``-e``), never through a shell. Secret
files (``sensitive_globs``) and ``.git`` are always excluded – an exclusion glob is appended *after* user globs so it
wins. Without ripgrep a Python implementation with identical semantics is used. Also: file-name search.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import json
import os
import re
import stat
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from hermclaw.core.errors import ValidationFailed
from hermclaw.core.logging import get_logger
from hermclaw.repo_intelligence import _proc
from hermclaw.repo_intelligence.config import RepoIntelConfig
from hermclaw.repo_intelligence.fileio import read_bytes
from hermclaw.repo_intelligence.languages import is_binary_name, looks_binary
from hermclaw.repo_intelligence.paths import matches_any, normalize_rel
from hermclaw.repo_intelligence.schemas import FileMatch, LexicalHit, LexicalResult
from hermclaw.repo_intelligence.sources import list_worktree_files

log = get_logger(__name__)
Mode = Literal["fixed", "regex"]
_MAX_PATTERN_CHARS = 2_000
_MAX_EXPLICIT_PATHS = 2_000


def _check_patterns(patterns: Sequence[str]) -> list[str]:
    pats = [p for p in patterns if isinstance(p, str) and p != ""]
    if not pats:
        raise ValidationFailed("empty search pattern", code="REPO_PATTERN_INVALID")
    for p in pats:
        if len(p) > _MAX_PATTERN_CHARS:
            raise ValidationFailed("search pattern too long", code="REPO_PATTERN_INVALID")
        if "\0" in p or "\n" in p:
            raise ValidationFailed("search pattern must be a single line", code="REPO_PATTERN_INVALID")
    return pats


def _smart_case(patterns: Sequence[str], case_sensitive: bool | None) -> bool:
    if case_sensitive is not None:
        return case_sensitive
    return any(c.isupper() for p in patterns for c in p)


@dataclass(frozen=True)
class _Spec:
    patterns: tuple[str, ...]
    mode: Mode
    globs: tuple[str, ...]
    exclude_globs: tuple[str, ...]
    paths: tuple[str, ...] | None
    case_sensitive: bool
    word: bool
    limit: int
    per_file: int
    timeout_s: float


class LexicalSearcher:
    def __init__(self, cfg: RepoIntelConfig | None = None) -> None:
        self.cfg = cfg or RepoIntelConfig()

    @property
    def has_ripgrep(self) -> bool:
        return _proc.which(self.cfg.rg_binary) is not None

    async def search(
        self,
        root: Path,
        patterns: str | Sequence[str],
        *,
        mode: Mode = "fixed",
        globs: Sequence[str] = (),
        exclude_globs: Sequence[str] = (),
        paths: Sequence[str] | None = None,
        case_sensitive: bool | None = None,
        word: bool = False,
        max_results: int | None = None,
        max_per_file: int | None = None,
        timeout_s: float | None = None,
        engine: Literal["auto", "ripgrep", "python"] = "auto",
    ) -> LexicalResult:
        pats = _check_patterns([patterns] if isinstance(patterns, str) else list(patterns))
        explicit = [normalize_rel(p) for p in paths] if paths is not None else None
        if explicit is not None:
            explicit = await asyncio.to_thread(_contained, root, [p for p in explicit if not matches_any(p, self.cfg.sensitive_globs)])
            if not explicit:
                return LexicalResult(engine="ripgrep" if self.has_ripgrep else "python")
        spec = _Spec(
            patterns=tuple(pats),
            mode=mode,
            globs=tuple(g.strip() for g in globs if g and g.strip()),
            exclude_globs=tuple(g.strip().lstrip("!") for g in exclude_globs if g and g.strip()),
            paths=tuple(explicit) if explicit is not None else None,
            case_sensitive=_smart_case(pats, case_sensitive),
            word=word,
            limit=max(1, max_results or self.cfg.lexical_max_results),
            per_file=max(1, max_per_file or self.cfg.lexical_max_per_file),
            timeout_s=timeout_s or self.cfg.rg_timeout_seconds,
        )
        use_rg = engine == "ripgrep" or (engine == "auto" and self.has_ripgrep)
        started = time.monotonic()
        if use_rg:
            res = await self._ripgrep(root, spec)
        else:
            if mode == "regex":
                for p in pats:
                    try:
                        re.compile(p)
                    except re.error as exc:
                        raise ValidationFailed(f"invalid regular expression: {exc}", code="REPO_PATTERN_INVALID") from exc
            files = await self._fallback_files(root, spec.paths)
            if mode == "regex":
                # Python's re holds the GIL while backtracking: a pathological pattern could freeze the event loop, so
                # caller-supplied regexes run in a short-lived child interpreter that is killed at the deadline
                res = await self._python_isolated(root, spec, files)
            else:
                res = await asyncio.to_thread(self._python, root, spec, files)
        res.elapsed_ms = int((time.monotonic() - started) * 1000)
        return res

    async def _fallback_files(self, root: Path, explicit: Sequence[str] | None) -> list[str]:
        if explicit is not None and await asyncio.to_thread(lambda: all(os.path.isfile(os.path.join(root, p)) for p in explicit)):
            return list(explicit)
        listing = (await list_worktree_files(root, self.cfg)).paths
        if explicit is None:
            return listing
        wanted = set(explicit)
        prefixes = tuple(p.rstrip("/") + "/" for p in explicit)
        return [f for f in listing if f in wanted or f.startswith(prefixes)]

    # ------------------------------------------------------------------------------------------- ripgrep
    def rg_argv(self, spec: _Spec) -> list[str]:
        cfg = self.cfg
        argv = [
            cfg.rg_binary,
            "--json",
            "--no-config",
            "--hidden",
            "--no-require-git",
            "--no-follow",
            "--line-number",
            "--column",
            "--max-columns",
            str(cfg.lexical_max_columns),
            "--max-columns-preview",
            "--max-filesize",
            str(cfg.max_read_file_bytes),
            "--max-count",
            str(spec.per_file),
            "--case-sensitive" if spec.case_sensitive else "--ignore-case",
        ]
        if cfg.lexical_sort_paths:
            argv += ["--sort", "path"]
        if spec.mode == "fixed":
            argv.append("--fixed-strings")
        if spec.word:
            argv.append("--word-regexp")
        for g in spec.globs:
            argv += ["--glob", g]
        for g in spec.exclude_globs:
            argv += ["--glob", "!" + g]
        for g in (".git", *cfg.sensitive_globs):  # last: these exclusions win over any user glob
            argv += ["--glob", "!" + g]
        for p in spec.patterns:
            argv += ["--regexp", p]
        argv.append("--")
        argv += list(spec.paths[:_MAX_EXPLICIT_PATHS]) if spec.paths is not None else ["."]
        return argv

    async def _ripgrep(self, root: Path, spec: _Spec) -> LexicalResult:
        argv = self.rg_argv(spec)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_proc._env(),
            limit=4 * 1024 * 1024,
            start_new_session=True,
        )
        assert proc.stdout is not None and proc.stderr is not None
        hits: list[LexicalHit] = []
        truncated = False
        deadline = time.monotonic() + spec.timeout_s
        stderr_task = asyncio.create_task(proc.stderr.read(64_000))
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    truncated = True
                    break
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), remaining)
                except TimeoutError:
                    truncated = True
                    break
                except ValueError:  # a single JSON line above the stream limit: skip it
                    continue
                if not line:
                    break
                hit = _parse_rg_line(line, self.cfg.lexical_max_columns)
                if hit is None:
                    continue
                hits.append(hit)
                if len(hits) >= spec.limit:
                    truncated = True
                    break
        finally:
            if truncated:
                await _proc.terminate(proc, drain=False)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
            if proc.returncode is None:
                await _proc.terminate(proc, drain=False)
            with contextlib.suppress(Exception):  # unread output: drain so the pipe transport closes now
                await asyncio.wait_for(proc.stdout.read(), 2)
        stderr = b""
        with contextlib.suppress(Exception):
            stderr = await asyncio.wait_for(stderr_task, 2)
        if not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await stderr_task
        if not truncated and proc.returncode == 2 and not hits:
            msg = stderr.decode("utf-8", errors="replace").strip()
            if "regex" in msg.lower() or "parse error" in msg.lower():
                raise ValidationFailed(f"invalid search pattern: {msg[:300]}", code="REPO_PATTERN_INVALID")
            if msg:
                log.debug("ripgrep reported errors: %s", msg[:300])
        hits.sort(key=lambda h: (h.path, h.line, h.column))
        return LexicalResult(hits=hits, truncated=truncated, engine="ripgrep")

    # ------------------------------------------------------------------------------------------- python fallback
    async def _python_isolated(self, root: Path, spec: _Spec, files: Sequence[str]) -> LexicalResult:
        payload = json.dumps(
            {
                "spec": dataclasses.asdict(spec),
                "files": list(files),
                "sensitive_globs": list(self.cfg.sensitive_globs),
                "max_read_file_bytes": self.cfg.max_read_file_bytes,
                "max_columns": self.cfg.lexical_max_columns,
            }
        ).encode()
        argv = [sys.executable, "-I", "-c", "from hermclaw.repo_intelligence.lexical import _child_main; _child_main()"]
        try:
            proc = await _proc.run(argv, cwd=root, timeout_s=spec.timeout_s + 2.0, stdin=payload, max_output=self.cfg.max_output_bytes)
        except _proc.RepoCommandTimeout:
            return LexicalResult(truncated=True, engine="python")
        if not proc.ok:
            log.warning("python search worker failed (rc=%s)", proc.returncode)
            return LexicalResult(truncated=True, engine="python")
        try:
            return LexicalResult.model_validate_json(proc.stdout)
        except ValueError:
            return LexicalResult(truncated=True, engine="python")

    def _python(self, root: Path, spec: _Spec, files: Sequence[str]) -> LexicalResult:
        return _python_search(
            root,
            spec,
            files,
            sensitive_globs=self.cfg.sensitive_globs,
            max_read_file_bytes=self.cfg.max_read_file_bytes,
            max_columns=self.cfg.lexical_max_columns,
        )

    # ------------------------------------------------------------------------------------------- filenames
    @staticmethod
    def rank_filenames(files: Sequence[str], query: str, *, limit: int = 50) -> list[FileMatch]:
        q = query.strip().lower().replace("\\", "/")
        if not q:
            return []
        out: list[FileMatch] = []
        for p in files:
            lp = p.lower()
            name = lp.rsplit("/", 1)[-1]
            stem = name.split(".", 1)[0]
            score = 0.0
            if lp == q or lp.endswith("/" + q):
                score = 1.0
            elif q in (name, stem):
                score = 0.95
            elif name.startswith(q):
                score = 0.85
            elif q in name:
                score = 0.7
            elif q in lp:
                score = 0.55
            else:
                it = iter(name)
                if all(ch in it for ch in q):
                    score = 0.3 * min(1.0, len(q) / max(1, len(name)))
            if score > 0:
                out.append(FileMatch(path=p, score=round(score, 4)))
        out.sort(key=lambda m: (-m.score, len(m.path), m.path))
        return out[:limit]

    async def find_files(self, root: Path, query: str, *, limit: int = 50, files: Sequence[str] | None = None) -> list[FileMatch]:
        if files is None:
            files = (await list_worktree_files(root, self.cfg)).paths
        visible = [f for f in files if not matches_any(f, self.cfg.sensitive_globs)]
        return self.rank_filenames(visible, query, limit=limit)


def _python_search(
    root: Path,
    spec: _Spec,
    files: Sequence[str],
    *,
    sensitive_globs: Sequence[str],
    max_read_file_bytes: int,
    max_columns: int,
) -> LexicalResult:
    """Pure-Python search with ripgrep semantics (used when ripgrep is unavailable)."""
    deadline = time.monotonic() + spec.timeout_s
    body = "|".join(re.escape(p) if spec.mode == "fixed" else f"(?:{p})" for p in spec.patterns)
    if spec.word:
        body = rf"(?<!\w)(?:{body})(?!\w)"
    rx = re.compile(body, 0 if spec.case_sensitive else re.IGNORECASE)
    include = [g for g in spec.globs if not g.startswith("!")]
    exclude = [*spec.exclude_globs, *(g.lstrip("!") for g in spec.globs if g.startswith("!"))]
    hits: list[LexicalHit] = []
    truncated = False
    for path in sorted(files):
        if time.monotonic() > deadline:
            truncated = True
            break
        if matches_any(path, sensitive_globs) or path.startswith(".git/"):
            continue
        if include and not matches_any(path, include):
            continue
        if exclude and matches_any(path, exclude):
            continue
        if is_binary_name(path):
            continue
        data = read_bytes(root, path, max_read_file_bytes + 1)
        if data is None or len(data) > max_read_file_bytes or looks_binary(data[:8192]):
            continue
        n = 0
        for lineno, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
            found = list(rx.finditer(line))
            if not found:
                continue
            hits.append(
                LexicalHit(
                    path=path,
                    line=lineno,
                    column=found[0].start() + 1,
                    text=line[:max_columns],
                    matches=[m.group(0) for m in found[:20]],
                )
            )
            n += 1
            if len(hits) >= spec.limit:
                truncated = True
                break
            if n >= spec.per_file:
                break
        if truncated:
            break
    return LexicalResult(hits=hits, truncated=truncated, engine="python")


def _child_main() -> None:  # pragma: no cover - runs in the isolated child interpreter (exercised by tests)
    """Entry point of the isolated regex search: JSON request on stdin, ``LexicalResult`` JSON on stdout."""
    req = json.loads(sys.stdin.buffer.read())
    spec_d = req["spec"]
    spec = _Spec(**{**spec_d, "patterns": tuple(spec_d["patterns"]), "globs": tuple(spec_d["globs"]),
                    "exclude_globs": tuple(spec_d["exclude_globs"]),
                    "paths": tuple(spec_d["paths"]) if spec_d["paths"] is not None else None})  # fmt: skip
    res = _python_search(
        Path.cwd(),
        spec,
        req["files"],
        sensitive_globs=req["sensitive_globs"],
        max_read_file_bytes=int(req["max_read_file_bytes"]),
        max_columns=int(req["max_columns"]),
    )
    sys.stdout.write(res.model_dump_json())
    sys.stdout.flush()


def _contained(root: Path, paths: Sequence[str]) -> list[str]:
    """Explicit search paths that are real files/directories inside ``root`` (no symlink at any level, no ``.git``).

    ripgrep follows symlinks named on its command line even with ``--no-follow``, so they are filtered here.
    """
    root_real = os.path.realpath(root)
    out: list[str] = []
    for p in dict.fromkeys(paths):
        if p == ".git" or p.startswith(".git/"):
            continue
        full = os.path.join(root_real, p)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            continue
        if os.path.realpath(full) != os.path.normpath(full):  # a symlinked parent directory
            continue
        out.append(p)
    return out


def _parse_rg_line(line: bytes, max_cols: int) -> LexicalHit | None:
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    if msg.get("type") != "match":
        return None
    data = msg.get("data") or {}
    path = _rg_text(data.get("path"))
    text = _rg_text(data.get("lines"))
    if path is None or text is None:
        return None
    path = path.removeprefix("./")
    subs = data.get("submatches") or []
    matches = [m for m in (_rg_text(s.get("match")) for s in subs) if m is not None][:20]
    col = int(subs[0].get("start", 0)) + 1 if subs else 1
    return LexicalHit(path=path, line=int(data.get("line_number") or 0), column=col, text=text.rstrip("\r\n")[:max_cols], matches=matches)


def _rg_text(obj: object) -> str | None:
    if not isinstance(obj, dict):
        return None
    if "text" in obj:
        return str(obj["text"])
    if "bytes" in obj:
        try:
            return base64.b64decode(obj["bytes"]).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return None
    return None
