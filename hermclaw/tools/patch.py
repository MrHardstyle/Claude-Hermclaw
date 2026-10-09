"""Unified-diff parsing for ``apply_patch`` (P17 17.4).

The engine never trusts ``git apply`` to decide *which* files a patch touches: the patch headers are parsed here,
cross-checked against ``git apply --numstat`` and every target path is validated against the workspace and the
scope contract *before* ``git apply --check`` and ``git apply`` run (working tree only, never the index, never a
commit). Symlink entries (mode 120000) and git-internal paths are refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from hermclaw.contracts.scope import Operation
from hermclaw.tools import errors as E
from hermclaw.tools.errors import ToolError

DEV_NULL = "/dev/null"
_DIFF_GIT = re.compile(r'^diff --git (?P<a>"(?:[^"\\]|\\.)*"|\S+) (?P<b>"(?:[^"\\]|\\.)*"|\S+)\s*$')
_OCTAL = re.compile(r"\\([0-7]{3})")
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "a": "\a", "b": "\b", "f": "\f", "r": "\r", "v": "\v"}


def unquote_git_path(raw: str) -> str:
    """Undo git's C-style quoting of paths with special characters."""
    raw = raw.strip()
    if not (len(raw) >= 2 and raw.startswith('"') and raw.endswith('"')):
        return raw
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            m = _OCTAL.match(body, i)
            if m:
                out.append(int(m.group(1), 8))
                i += 4
                continue
            out.extend(_ESCAPES.get(body[i + 1], body[i + 1]).encode())
            i += 2
            continue
        out.extend(ch.encode())
        i += 1
    return out.decode("utf-8", errors="replace")


def _header_path(line: str) -> str:
    value = line[4:]
    if value.startswith('"'):
        end = value.find('"', 1)
        while end != -1 and value[end - 1] == "\\":
            end = value.find('"', end + 1)
        return unquote_git_path(value[: end + 1] if end != -1 else value)
    return value.split("\t", 1)[0].rstrip()


@dataclass
class PatchFile:
    old_path: str | None = None  # raw (prefix not yet stripped); None = /dev/null
    new_path: str | None = None
    new_file: bool = False
    deleted: bool = False
    rename: bool = False
    copy: bool = False
    modes: list[str] = field(default_factory=list)
    hunks: int = 0
    binary: bool = False
    source: str | None = None  # rename/copy source (already without a/ b/ prefix)
    destination: str | None = None


@dataclass(frozen=True)
class PatchTarget:
    path: str
    operation: Operation


@dataclass
class ParsedPatch:
    files: list[PatchFile]
    strip: int

    def targets(self) -> list[PatchTarget]:
        out: list[PatchTarget] = []
        for f in self.files:
            old = strip_prefix(f.old_path, self.strip)
            new = strip_prefix(f.new_path, self.strip)
            if f.rename and f.source and f.destination:
                out += [PatchTarget(f.source, "delete"), PatchTarget(f.destination, "create")]
            elif f.copy and f.destination:
                out.append(PatchTarget(f.destination, "create"))
            elif f.new_file or (old is None and new is not None):
                out.append(PatchTarget(new or "", "create"))
            elif f.deleted or (new is None and old is not None):
                out.append(PatchTarget(old or "", "delete"))
            else:
                out.append(PatchTarget(new or old or "", "modify"))
        dedup: dict[tuple[str, str], PatchTarget] = {}
        for t in out:
            dedup.setdefault((t.path, t.operation), t)
        return list(dedup.values())


def strip_prefix(path: str | None, strip: int) -> str | None:
    if path is None or path == DEV_NULL:
        return None
    parts = path.split("/")
    if strip and len(parts) > strip:
        parts = parts[strip:]
    return "/".join(parts)


def parse_patch(text: str) -> ParsedPatch:
    """Parse git-style and plain unified diffs. Raises ``ToolError(PATCH_INVALID)`` for anything else."""
    if "\x00" in text:
        raise ToolError(E.PATCH_INVALID, "patch contains NUL bytes")
    files: list[PatchFile] = []
    cur: PatchFile | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _DIFF_GIT.match(line)
        if m:
            cur = PatchFile(old_path=unquote_git_path(m.group("a")), new_path=unquote_git_path(m.group("b")))
            files.append(cur)
        elif (
            line.startswith("--- ")
            and i + 1 < len(lines)
            and lines[i + 1].startswith("+++ ")
            and (cur is None or not cur.hunks or (i + 2 < len(lines) and lines[i + 2].startswith("@@")))
        ):
            old, new = _header_path(line), _header_path(lines[i + 1])
            if cur is None or cur.hunks:
                cur = PatchFile()
                files.append(cur)
            cur.old_path = None if old == DEV_NULL else old
            cur.new_path = None if new == DEV_NULL else new
            i += 2
            continue
        elif cur is not None:
            if line.startswith("@@"):
                cur.hunks += 1
            elif line.startswith("new file mode "):
                cur.new_file = True
                cur.modes.append(line.rsplit(" ", 1)[-1])
            elif line.startswith("deleted file mode "):
                cur.deleted = True
                cur.modes.append(line.rsplit(" ", 1)[-1])
            elif line.startswith(("old mode ", "new mode ")):
                cur.modes.append(line.rsplit(" ", 1)[-1])
            elif line.startswith("index ") and " " in line[6:]:
                cur.modes.append(line.rsplit(" ", 1)[-1])
            elif line.startswith("rename from "):
                cur.rename, cur.source = True, unquote_git_path(line[12:])
            elif line.startswith("rename to "):
                cur.rename, cur.destination = True, unquote_git_path(line[10:])
            elif line.startswith("copy from "):
                cur.copy, cur.source = True, unquote_git_path(line[10:])
            elif line.startswith("copy to "):
                cur.copy, cur.destination = True, unquote_git_path(line[8:])
            elif line.startswith(("GIT binary patch", "Binary files ")):
                cur.binary = True
        i += 1
    files = [f for f in files if f.old_path or f.new_path]
    if not files:
        raise ToolError(E.PATCH_INVALID, "no file headers found; expected a unified diff ('--- a/path' / '+++ b/path' and '@@' hunks)")
    for f in files:
        if not (f.hunks or f.binary or f.rename or f.copy or f.new_file or f.deleted or f.modes):
            raise ToolError(E.PATCH_INVALID, f"file section for '{f.new_path or f.old_path}' has no hunks")
        if "120000" in f.modes:
            raise ToolError(E.PATCH_SYMLINK_REFUSED, "patches that create or modify symlinks are not allowed")
    return ParsedPatch(files, _strip_level(files))


def _strip_level(files: list[PatchFile]) -> int:
    """``-p1`` when every header uses git's ``a/`` / ``b/`` prefixes, otherwise ``-p0``."""
    for f in files:
        if f.old_path is not None and not f.old_path.startswith("a/"):
            return 0
        if f.new_path is not None and not f.new_path.startswith("b/"):
            return 0
    return 1


def parse_numstat(raw: bytes) -> set[str]:
    """Paths from ``git apply --numstat -z`` (renames contribute both names)."""
    out: set[str] = set()
    parts = raw.split(b"\0")
    i = 0
    while i < len(parts):
        rec = parts[i]
        if not rec:
            i += 1
            continue
        fields = rec.split(b"\t", 2)
        if len(fields) == 3 and fields[2]:
            out.add(fields[2].decode("utf-8", errors="replace"))
            i += 1
        elif len(fields) == 3:  # rename/copy: "a\td\t" NUL old NUL new
            for j in (i + 1, i + 2):
                if j < len(parts) and parts[j]:
                    out.add(parts[j].decode("utf-8", errors="replace"))
            i += 3
        else:
            i += 1
    return out
