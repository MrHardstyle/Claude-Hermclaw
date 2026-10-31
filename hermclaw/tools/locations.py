"""Source lines named by a failed command's output (BUG-294).

Live cycle 3 M1 on 0.1.0rc61: ruff reported ``tests/test_app.py:73:9: F841 Local variable `task2` is assigned to but
never used``. For 35 minutes the coder edited two neighbouring, identically built tests instead of the one containing
line 73, and the job failed with REPLAN_LIMIT. Linters, compilers and test runners name only ``path:line``; the runtime
now appends each named line with the definition it belongs to, so the location is not left to the model's line counting.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath

# ``path:line`` (ruff, mypy, gcc, go, eslint unix, pytest, rustc ``--> path:line:col``) and ``path(line,col)`` (tsc)
_LOCATION = re.compile(
    r"(?<![\w./\-])(?P<path>/?(?:[\w.\-]+/)*[\w\-][\w.\-]*\.[A-Za-z][A-Za-z0-9]{0,7})"
    r"(?::(?P<line>\d+)(?=[:\s,]|$)|\((?P<pline>\d+),\d+\))",
    re.MULTILINE,
)
# a line that opens a definition in common languages
_DEFINITION = re.compile(
    r"^\s*(?:(?:export|public|private|static|async|pub)\s+)*(?:def|class|function|func|fn|sub|impl|interface|module)\b"
    r"|=>\s*\{?\s*$",
    re.IGNORECASE,  # PowerShell / VB write Function, Sub
)
_MAX_ABSOLUTE_STRIP = 3
_LINE_CHARS = 160

LinesOf = Callable[[str], Sequence[str] | None]


def _candidates(raw: str, cwd: str) -> list[str]:
    """Workspace-relative paths ``raw`` may mean: relative to the command's directory or the root; an absolute path
    (the sandbox mount) with its leading directories removed."""
    if raw.startswith("/"):
        parts = PurePosixPath(raw).parts[1:]
        return ["/".join(parts[i:]) for i in range(1, min(_MAX_ABSOLUTE_STRIP, len(parts) - 1) + 1)]
    out = [raw]
    if cwd not in {"", "."}:
        out.insert(0, f"{cwd.rstrip('/')}/{raw}")
    return out


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip())


def enclosing(lines: Sequence[str], index: int) -> str | None:
    """The definition line containing ``lines[index]`` – the nearest less indented line that opens a definition, else
    the nearest less indented line at all; ``None`` at top level."""
    level = _indent(lines[index])
    nearest: str | None = None
    for i in range(index - 1, -1, -1):
        text = lines[i]
        if not text.strip() or _indent(text) >= level:
            continue
        if _DEFINITION.search(text):
            return text.strip()
        nearest = nearest or text.strip()
        level = _indent(text)
        if level == 0:
            break
    return nearest


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _LINE_CHARS else text[: _LINE_CHARS - 1] + "…"


def source_locations(output: str, lines_of: LinesOf, *, cwd: str = ".", limit: int = 6) -> list[str]:
    """``path:line (in <definition>): <source>`` for up to ``limit`` distinct lines ``output`` names in workspace files.

    ``lines_of`` returns the lines of a workspace-relative file, or ``None`` when it is missing or may not be shown.
    """
    found: list[str] = []
    seen: set[tuple[str, int]] = set()
    cache: dict[str, Sequence[str] | None] = {}
    for m in _LOCATION.finditer(output):
        number = int(m.group("line") or m.group("pline"))
        for rel in _candidates(m.group("path"), cwd):
            if rel not in cache:
                cache[rel] = lines_of(rel)
            lines = cache[rel]
            if lines is None:
                continue
            if (rel, number) not in seen and 0 < number <= len(lines) and lines[number - 1].strip():
                seen.add((rel, number))
                scope = enclosing(lines, number - 1)
                where = f" (in {_clip(scope)})" if scope else ""
                found.append(f"{rel}:{number}{where}: {_clip(lines[number - 1])}")
            break
        if len(found) >= limit:
            break
    return found


# vbc: "path(line,col): error BC30183: Keyword is not valid as an identifier." names no word (BUG-391)
_VB_KEYWORD = re.compile(r"(?P<path>/?(?:[\w.\-]+/)*[\w\-][\w.\-]*\.vb)\((?P<line>\d+),(?P<col>\d+)\): error BC30183\b", re.I)
_WORD = re.compile(r"\[?([A-Za-z_]\w*)")


def keyword_hint(output: str, lines_of: LinesOf, *, cwd: str = ".") -> str | None:
    """A hint naming the Visual Basic keywords that BC30183 errors point at in workspace files, else ``None``.

    Live ladder R3-vb on rc110/rc112: ``Public Property Operator As String`` failed 13 times over five attempts; vbc
    only says "Keyword is not valid as an identifier", and the coder rewrote whole files without finding the word.
    """
    words: list[str] = []
    cache: dict[str, Sequence[str] | None] = {}
    for m in _VB_KEYWORD.finditer(output):
        number, col = int(m.group("line")), int(m.group("col"))
        for rel in _candidates(m.group("path"), cwd):
            if rel not in cache:
                cache[rel] = lines_of(rel)
            lines = cache[rel]
            if lines is None:
                continue
            word = _WORD.match(lines[number - 1][col - 1 :]) if 0 < number <= len(lines) and col > 0 else None
            if word and word.group(1).lower() not in {w.lower() for w in words}:
                words.append(word.group(1))
            break
    if not words:
        return None
    named = ", ".join(f"`{w}`" for w in words[:8])
    return (
        f"[hint] {named} is a reserved word in Visual Basic and cannot be a name (BC30183); VB ignores case, so "
        f"`{words[0].lower()}` is the keyword too. Rename it everywhere (e.g. `{words[0]}Text`, `Op`) or escape it as "
        f"`[{words[0]}]`."
    )
