"""What a shell command does with its output, for the repeat guards of the tool engine (BUG-344).

The coder re-ran the unchanged test suite with only another slice of its output (``| tail -80``, ``| tail -120``,
``| tail -220; true``, ``echo '=== failures ==='; …``) and re-printed a file it had just read (``cat -n app.ps1``,
``sed -n '80,161p' app.ps1``) instead of acting; the exact-match guards let every variant through (live G1 on Strata,
2026-10-25). ``core_command`` names the command without what only shapes its output; ``shell_read`` recognises a
command that only prints (part of) one file.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

# stages after the first one of a pipeline that only filter what the command printed
OUTPUT_FILTERS = frozenset(
    {"head", "tail", "grep", "egrep", "fgrep", "rg", "sed", "awk", "cut", "sort", "uniq", "wc", "cat", "less", "more", "tr", "nl", "column"}
)
# statements that only print a label or end a command list successfully
DECORATIONS = frozenset({"echo", "printf", "true", ":"})
# options that change how much a test runner prints, not what it runs
VERBOSITY = frozenset({"-v", "-vv", "-vvv", "-q", "-qq", "--verbose", "--quiet", "--no-header"})
_TB = re.compile(r"^--tb=\w+$")
_SEPARATORS = frozenset({";", "&&", "||", "&"})
_REDIRECT = re.compile(r"^\d*(>>?|>&|<|&>)$")
_SED_PRINT = re.compile(r"^(\d+)(?:,(\d+|\$))?p$")
_COUNT = re.compile(r"^-n?(\+?\d+)$")


def _tokens(command: str) -> list[str] | None:
    if "<<" in command or "`" in command or "$(" in command:
        return None  # here-documents and command substitutions are kept as written
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes
        return None


def _statements(tokens: list[str]) -> list[list[list[str]]]:
    """Statements (split at ; && || &) of pipeline stages (split at |) of words, without redirections."""
    out: list[list[list[str]]] = []
    stages: list[list[str]] = [[]]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _SEPARATORS:
            out.append(stages)
            stages = [[]]
        elif tok == "|":
            stages.append([])
        elif _REDIRECT.match(tok):
            i += 1  # skip the target
        elif re.fullmatch(r"\d+", tok) and i + 1 < len(tokens) and _REDIRECT.match(tokens[i + 1]):
            pass  # the file descriptor of '2>&1'
        else:
            stages[-1].append(tok)
        i += 1
    out.append(stages)
    return [[s for s in st if s] for st in out if any(st)]


def core_command(command: str) -> str:
    """``command`` without labels, trailing output filters, redirections and verbosity options – two commands with the
    same core run the same thing on the same files. Falls back to the stripped command when it cannot be parsed."""
    tokens = _tokens(command)
    if tokens is None:
        return command.strip()
    kept: list[str] = []
    for stages in _statements(tokens):
        if stages[0][0] in DECORATIONS:
            continue
        last = len(stages)
        while last > 1 and stages[last - 1][0] in OUTPUT_FILTERS:
            last -= 1
        words = [" ".join(w for w in stage if w not in VERBOSITY and not _TB.match(w)) for stage in stages[:last]]
        kept.append(" | ".join(words))
    return " ; ".join(kept) if kept else command.strip()


@dataclass(frozen=True)
class ShellRead:
    """A command that only prints lines of one file: ``start`` 1-based (negative: that many lines before the end),
    ``end`` inclusive or ``None`` for the end of the file."""

    path: str
    start: int = 1
    end: int | None = None


def _count(words: list[str]) -> tuple[int | None, list[str]]:
    """The line count of head/tail options (-n N, -nN, -N) and the remaining operands."""
    count: int | None = None
    rest: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w == "-n" and i + 1 < len(words):
            count = int(words[i + 1]) if words[i + 1].lstrip("+").isdigit() else None
            if words[i + 1].startswith("+") and count is not None:
                count = -count  # 'tail -n +N': from line N
            i += 2
            continue
        m = _COUNT.match(w)
        if m:
            raw = m.group(1)
            count = -int(raw[1:]) if raw.startswith("+") else int(raw)
        elif w.startswith("-"):
            return None, []
        else:
            rest.append(w)
        i += 1
    return count, rest


def _reader(words: list[str]) -> ShellRead | None:
    name, args = words[0], words[1:]
    if name in ("cat", "nl", "less", "more"):
        operands = [a for a in args if not a.startswith("-")]
        return ShellRead(operands[0]) if len(operands) == 1 else None
    if name in ("head", "tail"):
        count, operands = _count(args)
        if len(operands) != 1:
            return None
        n = 10 if count is None else count
        if name == "head":
            return ShellRead(operands[0], 1, n) if n > 0 else None
        return ShellRead(operands[0], -n) if n else None  # -N: the last N lines; +N (count -N): from line N
    if name == "sed" and len(args) == 3 and args[0] == "-n":
        m = _SED_PRINT.match(args[1])
        if m:
            start = int(m.group(1))
            end = None if m.group(2) == "$" else int(m.group(2) or start)
            return ShellRead(args[2], start, end)
    return None


def shell_read(command: str) -> ShellRead | None:
    """The file span ``command`` prints when it is one reader (cat, nl, less, more, head, tail, sed -n 'A,Bp') of one
    file, optionally narrowed by a following ``| head -n N``; ``None`` for anything else."""
    tokens = _tokens(command)
    if tokens is None or any(_REDIRECT.match(t) for t in tokens):
        return None  # 'cat a.py > b.py' writes a file
    statements = [st for st in _statements(tokens) if st[0][0] not in DECORATIONS]
    if len(statements) != 1 or not 1 <= len(statements[0]) <= 2:
        return None
    stages = statements[0]
    read = _reader(stages[0])
    if read is None or read.path.startswith("-"):
        return None
    if len(stages) == 2:
        if stages[1][0] != "head" or read.start < 1:
            return None
        count, operands = _count(stages[1][1:])
        n = 10 if count is None else count
        if operands or n <= 0:
            return None
        end = read.start + n - 1
        return ShellRead(read.path, read.start, end if read.end is None else min(end, read.end))
    return read
