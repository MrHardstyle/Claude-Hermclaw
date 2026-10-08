"""Parsers for NUL-terminated git porcelain output (robust against spaces, quotes and newlines in paths)."""

from __future__ import annotations

import os
from dataclasses import dataclass

UNMERGED_CODES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def _decode(raw: bytes) -> str:
    return os.fsdecode(raw)


@dataclass(frozen=True, slots=True)
class PorcelainEntry:
    index: str
    worktree: str
    path: str
    orig_path: str | None = None

    @property
    def code(self) -> str:
        return self.index + self.worktree

    @property
    def untracked(self) -> bool:
        return self.code == "??"

    @property
    def conflicted(self) -> bool:
        return self.code in UNMERGED_CODES


def parse_porcelain_v1(data: bytes) -> list[PorcelainEntry]:
    """Parse ``git status --porcelain=v1 -z`` (rename entries carry ``NEW\\0ORIG\\0``)."""
    entries: list[PorcelainEntry] = []
    tokens = data.split(b"\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        if len(tok) < 4 or tok[2:3] != b" ":
            raise ValueError(f"unexpected porcelain record: {tok[:40]!r}")
        x, y = chr(tok[0]), chr(tok[1])
        path = _decode(tok[3:])
        orig: str | None = None
        if (x in "RC" or y in "RC") and i < len(tokens):
            orig = _decode(tokens[i])
            i += 1
        entries.append(PorcelainEntry(x, y, path, orig))
    return entries


@dataclass(frozen=True, slots=True)
class NameStatus:
    status: str  # A, M, D, R, C, T, U, X
    path: str
    old_path: str | None = None
    score: int | None = None


def parse_name_status(data: bytes) -> list[NameStatus]:
    """Parse ``git diff --name-status -z`` output."""
    out: list[NameStatus] = []
    tokens = data.split(b"\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        status_raw = tok.decode("ascii", "replace")
        letter = status_raw[0]
        score = int(status_raw[1:]) if len(status_raw) > 1 and status_raw[1:].isdigit() else None
        if letter in "RC":
            old = _decode(tokens[i])
            new = _decode(tokens[i + 1])
            i += 2
            out.append(NameStatus(letter, new, old, score))
        else:
            out.append(NameStatus(letter, _decode(tokens[i])))
            i += 1
    return out


@dataclass(frozen=True, slots=True)
class NumStat:
    path: str
    additions: int | None  # None for binary files
    deletions: int | None
    old_path: str | None = None

    @property
    def binary(self) -> bool:
        return self.additions is None


def parse_numstat(data: bytes) -> list[NumStat]:
    """Parse ``git diff --numstat -z`` (renames: ``A\\tD\\t\\0OLD\\0NEW\\0``)."""
    out: list[NumStat] = []
    tokens = data.split(b"\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        fields = tok.split(b"\t", 2)
        if len(fields) != 3:
            raise ValueError(f"unexpected numstat record: {tok[:40]!r}")
        add_raw, del_raw, path_raw = fields
        adds = None if add_raw == b"-" else int(add_raw)
        dels = None if del_raw == b"-" else int(del_raw)
        if path_raw:
            out.append(NumStat(_decode(path_raw), adds, dels))
        else:
            old = _decode(tokens[i])
            new = _decode(tokens[i + 1])
            i += 2
            out.append(NumStat(new, adds, dels, old))
    return out


def split_nul(data: bytes) -> list[str]:
    return [_decode(t) for t in data.split(b"\0") if t]


@dataclass(frozen=True, slots=True)
class PushRefResult:
    flag: str  # ' ' fast-forward, '+' forced, '-' deleted, '*' new, '!' rejected, '=' up to date
    source: str
    destination: str
    summary: str
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.flag != "!"


def parse_push_porcelain(text: str) -> list[PushRefResult]:
    """Parse ``git push --porcelain`` stdout (``<flag>\\t<from>:<to>\\t<summary> (<reason>)``)."""
    out: list[PushRefResult] = []
    for line in text.splitlines():
        if not line or line.startswith(("To ", "Done")) or "\t" not in line:
            continue
        flag = line[0]
        rest = line[1:].lstrip("\t")
        refs, _, summary = rest.partition("\t")
        src, _, dst = refs.partition(":")
        reason = None
        if summary.endswith(")") and "(" in summary:
            summary, _, tail = summary.rpartition("(")
            reason = tail[:-1]
            summary = summary.strip()
        out.append(PushRefResult(flag, src, dst, summary, reason))
    return out
