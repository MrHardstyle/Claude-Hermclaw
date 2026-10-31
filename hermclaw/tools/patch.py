"""Unified-diff parsing for ``apply_patch`` (P17 17.4).

The engine never trusts ``git apply`` to decide *which* files a patch touches: the patch headers are parsed here,
cross-checked against ``git apply --numstat`` and every target path is validated against the workspace and the
scope contract *before* ``git apply --check`` and ``git apply`` run (working tree only, never the index, never a
commit). Symlink entries (mode 120000) and git-internal paths are refused.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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


def diff_section_path(line: str) -> str | None:
    """Repository path of a ``diff --git a/x b/x`` section header (``b/`` side, prefix stripped), else ``None``."""
    m = _DIFF_GIT.match(line)
    if not m:
        return None
    path = unquote_git_path(m.group("b"))
    return path[2:] if path.startswith("b/") else path


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
            elif line.startswith(("old mode ", "new mode ")) or (line.startswith("index ") and " " in line[6:]):
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


_COUNTED_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")


def recount_hunks(text: str) -> str:
    """Set the line counts of every numbered hunk header from its body (BUG-390).

    ``git apply --recount`` counts a hunk's lines up to the next ``@@`` or ``diff`` line, so in a plain unified diff
    of several files the next section's ``--- a/path`` header became a removed line ("new file x depends on old
    contents"; all 9 such apply_patch calls since 21.10. failed). The engine recounts instead: a body ends at the next
    hunk, the next ``---``/``+++`` pair or ``diff --git`` line, and empty lines that end a body are separators, not
    context (they are dropped)."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _COUNTED_HUNK.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not _ends_hunk(lines, j):
            j += 1
        body = lines[i + 1 : j]
        while body and body[-1] == "":
            body.pop()
        old = sum(1 for b in body if b[:1] in " -" or b == "")
        new = sum(1 for b in body if b[:1] in " +" or b == "")
        out += [f"@@ -{m.group(1)},{old} +{m.group(2)},{new} @@{m.group(3)}", *body]
        i = j
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _ends_hunk(lines: list[str], j: int) -> bool:
    line = lines[j]
    if line.startswith("@@") or _DIFF_GIT.match(line):
        return True
    return line.startswith("--- ") and j + 1 < len(lines) and lines[j + 1].startswith("+++ ")


def prefix_added_lines(text: str) -> tuple[str, int]:
    """Give a hunk line that lacks its diff prefix the ``+`` it lost (BUG-389).

    Live ladder R3 (5 calls since 27.10., R3-vb on rc112 among them): the coder copied the task's grammar into a new
    file and the continuation line ``term := faktor ...`` came without its ``+``; git refused the whole patch
    ("recount: unexpected line"). A line inside a hunk that starts with none of `` ``, ``+``, ``-``, ``\\`` is added
    content when the section creates its file or the line follows an added line; anywhere else it stays as written
    and git judges it. Blank lines are left to the other repairs. Returns the patch and the number of lines fixed."""
    lines = text.split("\n")
    in_hunk, creating, prev, fixed = False, False, "", 0
    for i, line in enumerate(lines):
        if _DIFF_GIT.match(line) or (line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ ")):
            in_hunk, creating = False, _header_path(line) == DEV_NULL if line.startswith("--- ") else False
            continue
        if line.startswith("@@"):
            in_hunk, prev = True, ""
            continue
        if not in_hunk or not line.strip():
            continue
        if line[0] in " +-\\":
            prev = line[0]
        elif creating or prev == "+":
            lines[i], prev, fixed = "+" + line, "+", fixed + 1
    return ("\n".join(lines), fixed) if fixed else (text, 0)


def rebuild_new_files(text: str, is_new: Callable[[str], bool]) -> tuple[str, list[str]]:
    """Rebuild the sections of a plain unified diff that create a file as a clean new-file patch (BUG-364).

    Live G1 on rc91, S007: the coder created ``collector.py`` with a bare ``@@`` under ``--- a/collector.py`` and
    ``api.py`` under ``--- /dev/null`` with blank lines lacking their ``+``; git refused both ("No valid patches in
    input", "new file api.py depends on old contents") and the coder fell back to shell writes, which are refused too.
    A section is rebuilt only when ``is_new`` says its file does not exist and its body holds nothing but hunk
    headers, ``+`` lines and blank lines – a ``-`` or context line keeps the patch as it is. Returns the patch and the
    rebuilt paths."""
    if any(_DIFF_GIT.match(line) for line in text.splitlines()):
        return text, []
    lines = text.splitlines()
    starts = [i for i in range(len(lines) - 1) if lines[i].startswith("--- ") and lines[i + 1].startswith("+++ ")]
    if not starts:
        return text, []
    out = lines[: starts[0]]
    rebuilt: list[str] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        section = lines[start:end]
        new = _header_path(section[1])
        body = section[2:]
        path = strip_prefix(new, 1 if new.startswith("b/") else 0)
        content = _created_lines(body)
        if path is None or content is None or not _safe_is_new(is_new, path):
            out += section
            continue
        out += ["--- /dev/null", section[1], f"@@ -0,0 +1,{len(content)} @@", *(f"+{c}" for c in content)]
        rebuilt.append(path)
    return ("\n".join(out) + "\n", rebuilt) if rebuilt else (text, [])


_NUMBERED_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_GIT_HEADER = ("diff --git ", "index ", "new file mode ", "deleted file mode ", "old mode ", "new mode ", "similarity ")


def locate_bare_hunks(text: str, read: Callable[[str], str | None]) -> tuple[str, list[str]]:
    """Give the bare ``@@`` hunks of a patch against an existing file their line numbers (BUG-387).

    Live ladder R3-vb on rc110: the coder wrote hunks headed by a bare ``@@`` (no ``-a,b +c,d``), which git never
    applies ("No valid patches in input"); 14 of 40 failed apply_patch calls since 21.10. had them. Each such hunk is
    looked up in the file ``read`` returns: its context and ``-`` lines must occur there as one block (after the
    previous hunk; trailing whitespace may differ, the file's text is then used), a blank body line counts as an
    empty context line. A hunk that is not found raises ``PATCH_INVALID`` naming it. Returns the patch and the paths
    whose hunks were located."""
    lines = text.split("\n")
    starts = [i for i in range(len(lines) - 1) if lines[i].startswith("--- ") and lines[i + 1].startswith("+++ ")]
    if not any(ln.startswith("@@") and not _NUMBERED_HUNK.match(ln) for ln in lines):
        return text, []
    out = lines[: starts[0]] if starts else lines
    located: list[str] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        section = lines[start:end]
        tail: list[str] = []
        while len(section) > 2 and (section[-1].startswith(_GIT_HEADER) or (n + 1 < len(starts) and not section[-1].strip())):
            tail.insert(0, section.pop())
        new = _header_path(section[1])
        path = strip_prefix(new, 1 if new.startswith("b/") else 0)
        bare = any(ln.startswith("@@") and not _NUMBERED_HUNK.match(ln) for ln in section[2:])
        content = read(path) if path is not None and bare and _header_path(section[0]) != DEV_NULL else None
        if content is None:
            out += section + tail
            continue
        out += section[:2] + _locate_section(path or "", section[2:], content.split("\n")) + tail
        located.append(path or "")
    return ("\n".join(out), located) if located else (text, [])


def _locate_section(path: str, body: list[str], file_lines: list[str]) -> list[str]:
    out: list[str] = []
    hunks: list[list[str]] = []
    for line in body:
        if line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            out.append(line)
    pos = 0
    items: list[list[str] | tuple[int, int, int, list[str]]] = []  # numbered hunks as they are, located ones as (at, old, new, rows)
    for k, hunk in enumerate(hunks, 1):
        if _NUMBERED_HUNK.match(hunk[0]):
            items.append(hunk)
            continue
        rows = hunk[1:]
        while rows and not rows[-1].strip():
            rows.pop()
        old = [r[1:] if r[:1] in " -" else "" for r in rows if r[:1] in " -" or not r.strip()]
        at = _find_block(file_lines, old, pos)
        if at is None:
            where = f" after line {pos}" if pos else ""
            raise ToolError(
                E.PATCH_INVALID,
                f"hunk {k} for '{path}' has a bare '@@' header and its context and '-' lines do not occur together in the "
                f"file{where}; re-read the lines and copy them exactly, or change them with replace_text",
            )
        i, fixed = at, []
        for r in rows:
            if r[:1] in " -" or not r.strip():
                fixed.append(("-" if r[:1] == "-" else " ") + file_lines[i])
                i += 1
            else:
                fixed.append(r)
        new_count = sum(1 for r in fixed if r[:1] in " +")
        prev = items[-1] if items else None
        if isinstance(prev, tuple) and prev[0] + prev[1] == at:  # adjacent hunks: one hunk, git refuses overlaps
            items[-1] = (prev[0], prev[1] + len(old), prev[2] + new_count, prev[3] + fixed)
        else:
            items.append((at, len(old), new_count, fixed))
        pos = at + len(old)
    real = len(file_lines) - (1 if file_lines and file_lines[-1] == "" else 0)
    delta = 0
    for item in items:
        if not isinstance(item, tuple):
            out += item
            continue
        at, old_n, new_n, rows = item
        # git matches a hunk without trailing context only at the end of the file: give it the next line
        extra = 0 if rows[-1][:1] == " " or at + old_n >= real else 1
        out.append(f"@@ -{at + 1},{old_n + extra} +{at + 1 + delta},{new_n + extra} @@")
        out += rows + [" " + file_lines[at + old_n]] * extra
        delta += new_n - old_n
    return out


def _same(line: str) -> str:
    return line


def _find_block(file_lines: list[str], old: list[str], pos: int) -> int | None:
    """First index at or after ``pos`` where ``old`` occurs (exactly, else up to trailing whitespace)."""
    if not old or not any(o.strip() for o in old):
        return None
    for norm in (_same, str.rstrip):
        want = [norm(o) for o in old]
        for at in range(pos, len(file_lines) - len(old) + 1):
            if all(norm(file_lines[at + j]) == w for j, w in enumerate(want)):
                return at
    return None


def _created_lines(body: list[str]) -> list[str] | None:
    """The file content of a creation hunk body, ``None`` if the body changes or keeps existing lines. A blank line
    is an empty line of the file unless it trails the last ``+`` line."""
    if not any(line.startswith("@@") for line in body):
        return None
    content: list[str] = []
    blanks = 0
    for line in body:
        if line.startswith(("@@", "\\")):
            continue
        if line.startswith("+"):
            content += [""] * blanks + [line[1:]]
            blanks = 0
        elif not line.strip():
            blanks += 1
        else:
            return None
    return content or None


def _safe_is_new(is_new: Callable[[str], bool], path: str) -> bool:
    try:
        return is_new(path)
    except (ToolError, OSError, ValueError):
        return False


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
