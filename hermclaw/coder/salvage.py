"""Salvage of a ``write_file`` answer cut off at the output limit (BUG-252).

A coder answer that hits the output limit is invalid JSON and used to be discarded whole: the model had generated
8192 tokens of a new module for 5.5 minutes, and the repeated attempt was cut off again. When the cut-off answer is a
``write_file``, its complete lines are already there: they are written as the file's first part, and the coder
continues with ``"append": true`` after the last written line.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_TOOL_RE = re.compile(r'"tool"\s*:\s*"write_file"')
_PATH_RE = re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"')
_APPEND_RE = re.compile(r'"append"\s*:\s*(true|false)')
_CONTENT_RE = re.compile(r'"content"\s*:\s*"')
_PARTIAL_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{0,3}$")


@dataclass(frozen=True)
class SalvagedWrite:
    path: str
    content: str  # complete lines only, ending with a newline
    append: bool

    @property
    def lines(self) -> int:
        return self.content.count("\n")


def _raw_string(text: str) -> str:
    """The JSON string body starting at ``text`` up to its closing quote or the end of the cut-off text."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        if ch == '"':
            break
        out.append(ch)
        i += 1
    return "".join(out)


def salvage_write(partial: str, *, min_lines: int = 20) -> SalvagedWrite | None:
    """The complete lines of a cut-off ``write_file`` answer, or None when the answer is no such call or too short."""
    if not partial or not _TOOL_RE.search(partial):
        return None
    content_at = _CONTENT_RE.search(partial)
    path_at = _PATH_RE.search(partial)
    if content_at is None or path_at is None:
        return None
    raw = _raw_string(partial[content_at.end() :])
    if raw.endswith("\\") and (len(raw) - len(raw.rstrip("\\"))) % 2:
        raw = raw[:-1]
    raw = _PARTIAL_ESCAPE_RE.sub("", raw)
    try:
        content = json.loads(f'"{raw}"')
        path = json.loads(f'"{path_at.group(1)}"')
    except ValueError:
        return None
    if not isinstance(content, str) or not isinstance(path, str) or not path.strip():
        return None
    complete = content[: content.rfind("\n") + 1]
    if complete.count("\n") < min_lines:
        return None
    append = (m := _APPEND_RE.search(partial)) is not None and m.group(1) == "true"
    return SalvagedWrite(path=path, content=complete, append=append)
