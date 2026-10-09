"""Deterministic fingerprints for stagnation detection (Bauplan §20, P20 20.1/20.2).

Every function here is pure: the same input always yields the same fingerprint, and inputs that differ only in
volatile noise (timestamps, durations, memory addresses, temp paths, random seeds, ANSI colours, optionally line and
column numbers) yield the same fingerprint. Fingerprints are short SHA-256 prefixes; human-readable labels and
signatures are redacted and clipped so they can be persisted, shown in the UI and injected into prompts safely.

Nothing in this module knows about a particular project or benchmark – the parsers only recognise the generic
output shapes of common test runners and compilers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hermclaw.contracts.scope import normalise_path
from hermclaw.core.redaction import DEFAULT_REDACTOR

DIGEST_CHARS = 16
LABEL_CHARS = 120
SIGNATURE_CHARS = 160
MAX_KEY_LINES = 20
MAX_TEST_IDS = 200
MAX_TEST_ID_CHARS = 300

# argument keys whose values are repository paths (normalised so './a.py' and 'a.py' fingerprint alike)
_PATH_KEYS = frozenset({"path", "paths", "cwd", "file", "files", "dir", "directory", "target", "targets"})
# argument keys used for a short human-readable action label, in order of preference
_LABEL_KEYS = ("path", "command", "query", "pattern", "name", "question", "paths", "reason")


def digest(text: str, *, chars: int = DIGEST_CHARS) -> str:
    """Short, stable SHA-256 hex digest."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:chars]


def canonical_json(value: Any) -> str:
    """Stable JSON (sorted keys, no whitespace); unknown objects are stringified."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def clip(text: str, limit: int) -> str:
    """Collapse whitespace and clip to ``limit`` characters (with an ellipsis)."""
    t = " ".join(text.split())
    return t if len(t) <= limit else t[: max(0, limit - 1)] + "…"


def safe_label(text: str, limit: int = LABEL_CHARS) -> str:
    """Redacted, clipped single-line label – the only form in which tool/model text leaves this package."""
    return clip(DEFAULT_REDACTOR.text(text), limit)


# =============================================================================================== normalisation
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_ISO_TS = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2}|\s?UTC)?\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b|\b\d{2}:\d{2}\.\d+\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ADDR = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
_TMP_PATH = re.compile(
    r"(?:/private)?/(?:tmp|var/tmp|var/folders|dev/shm|run/user/\d+)/[^\s'\"`:,;()\[\]]*"
    r"|[A-Za-z]:\\(?:Users\\[^\\\s]+\\AppData\\Local\\Temp|Windows\\Temp)\\[^\s'\"`:,;()\[\]]*"
    r"|\bpytest-of-[^/\s]+/pytest-\d+(?:/[^\s'\"`:,;()\[\]]*)?"
    r"|\btmp[a-z0-9_]{6,}\b"
)
_DURATION = re.compile(
    r"(?<![\w.])\d+(?:\.\d+)?\s?(?:ms|msec|s|sec|secs|seconds?|µs|us|ns|min|mins|minutes?)(?![\w])",
    re.IGNORECASE,
)
_MEMORY = re.compile(r"\b(?:Memory|RSS|Heap)\s*:?\s*\d+(?:\.\d+)?\s?(?:[KMGT]i?B|bytes)\b", re.IGNORECASE)
_SEED = re.compile(r"(?i)\b((?:random(?:ly)?[-_ ])?seed)(\s*[=:]?\s*)\d+")
_PID = re.compile(r"(?i)\b(pid|process|thread)(\s*[=:#]?\s*)\d+")
_LONG_HEX = re.compile(r"\b[0-9a-f]{12,}\b")
_PERCENT = re.compile(r"\[\s*\d{1,3}%\s*\]")
_LINE_WORD = re.compile(r"(?i)\b(line|lineno|column|col)(\s*[=:]?\s*)\d+")
_FILE_LINE = re.compile(r"(\.[A-Za-z][A-Za-z0-9_]{0,7})(?::\d+){1,2}(?![\w.])|(\.[A-Za-z][A-Za-z0-9_]{0,7})\(\d+(?:,\d+)?\)")
_HSPACE = re.compile(r"[ \t\f\v]+")


def _file_line_repl(m: re.Match[str]) -> str:
    return f"{m.group(1)}:<n>" if m.group(1) is not None else f"{m.group(2)}(<n>)"


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def normalise_text(text: str, *, strip_line_numbers: bool = True) -> str:
    """Remove volatile noise so that two runs of the same failure produce the same text.

    Strips ANSI codes, timestamps, dates, clock times, durations, memory figures, UUIDs, memory addresses, temp paths,
    random seeds, PIDs, long hex ids and progress percentages; optionally line/column numbers. Horizontal whitespace
    is collapsed, lines are stripped and empty lines dropped. The result is *not* redacted (see :func:`safe_label`).
    """
    t = strip_ansi(text).replace("\r\n", "\n").replace("\r", "\n")
    t = _ISO_TS.sub("<ts>", t)
    t = _DATE.sub("<date>", t)
    t = _CLOCK.sub("<time>", t)
    t = _UUID.sub("<uuid>", t)
    t = _ADDR.sub("0x<addr>", t)
    t = _TMP_PATH.sub("<tmp>", t)
    t = _MEMORY.sub("<mem>", t)
    t = _DURATION.sub("<dur>", t)
    t = _SEED.sub(r"\1\2<n>", t)
    t = _PID.sub(r"\1\2<n>", t)
    t = _LONG_HEX.sub("<hex>", t)
    t = _PERCENT.sub("[<n>%]", t)
    if strip_line_numbers:
        t = _LINE_WORD.sub(r"\1\2<n>", t)
        t = _FILE_LINE.sub(_file_line_repl, t)
    lines = (_HSPACE.sub(" ", line).strip() for line in t.split("\n"))
    return "\n".join(line for line in lines if line)


# ================================================================================================ 20.1 actions
def _normalise_path_value(value: str) -> str:
    s = value.strip()
    if s in ("", ".", "./"):
        return "."
    try:
        q = normalise_path(s)
    except ValueError:
        return s  # absolute or traversing paths are kept verbatim (the tool layer refuses them anyway)
    return q.rstrip("/") or "."


def _normalise_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        collapsed = " ".join(value.split())
        return _normalise_path_value(collapsed) if key in _PATH_KEYS else collapsed
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _normalise_value(str(k), v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list | tuple | set | frozenset):
        items = [_normalise_value(key, v) for v in value]
        if key in _PATH_KEYS or isinstance(value, set | frozenset):
            items = sorted(items, key=canonical_json)
        return items
    return str(value)


def normalise_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical argument mapping: whitespace collapsed, paths normalised, path lists sorted, keys sorted."""
    return {str(k): _normalise_value(str(k), v) for k, v in sorted(args.items(), key=lambda kv: str(kv[0]))}


def action_fingerprint(tool: str, args: Mapping[str, Any]) -> str:
    """``<tool>:<digest of normalised args>`` – equal for semantically identical calls."""
    return f"{tool}:{digest(canonical_json(normalise_args(args)))}"


def action_label(tool: str, args: Mapping[str, Any]) -> str:
    """Short, redacted description of an action, e.g. ``read_file app/main.py`` (never file contents)."""
    norm = normalise_args(args)
    for key in _LABEL_KEYS:
        value = norm.get(key)
        if isinstance(value, str) and value:
            return safe_label(f"{tool} {value}", 100)
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return safe_label(f"{tool} {', '.join(value[:5])}", 100)
    return tool


def tool_sequence(actions: Sequence[str], length: int) -> str | None:
    """The n-gram of the last ``length`` action fingerprints (``None`` if fewer are known)."""
    if length < 2 or len(actions) < length:
        return None
    return ">".join(actions[-length:])


def sequence_label(actions: Sequence[str], length: int) -> str:
    """Readable form of :func:`tool_sequence`: the tool names only."""
    return ">".join(a.split(":", 1)[0] for a in actions[-length:])


_DECISION_JUNK = re.compile(r"[^a-z0-9]+")


def decision_label(decision: str) -> str | None:
    """Normalised ``CoderAction.decision`` label (redacted, lowercase, dashes); ``None`` when empty."""
    label = _DECISION_JUNK.sub("-", DEFAULT_REDACTOR.text(decision).strip().lower()).strip("-")
    return label[:LABEL_CHARS] or None


# ================================================================================================= 20.2 errors
@dataclass(frozen=True)
class ErrorSignature:
    """Fingerprint of a failure: ``digest`` identifies it, ``signature`` is a short redacted human-readable form."""

    digest: str
    signature: str
    error_code: str
    tool: str


_KEY_LINE = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception|Failure|Fault|Interrupt|Exit)\b"
    r"|^E\s"
    r"|^(?:FAILED|ERROR|FAIL)\b"
    r"|\b(?:error|fatal)(?:\[[A-Za-z0-9]+\])?:"
    r"|\bpanic(?:ked)?\b"
    r"|npm ERR!"
    r"|command not found|No such file or directory|Permission denied|not permitted|refused"
    r"|\bassert(?:ion)?\b"
    r"|^\s*[✕×✗●]"
    r"|\b(?:cannot|could not|unable to|failed to)\b",
)


def key_error_lines(normalised: str) -> list[str]:
    """Lines that carry the error itself (exception lines, assertion details, compiler errors), deduplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for line in normalised.split("\n"):
        if _KEY_LINE.search(line) and line not in seen:
            seen.add(line)
            out.append(line)
            if len(out) >= MAX_KEY_LINES:
                break
    return out


def error_signature(tool: str, error_code: str | None, output: str, *, strip_line_numbers: bool = True) -> ErrorSignature:
    """Signature of a failed tool result.

    The digest covers the tool, the stable error code and the *key error lines* of the normalised output (falling back
    to its last lines). Tracebacks, source excerpts and progress output therefore do not make two occurrences of the
    same error look different, while a different exception or assertion does.
    """
    code = (error_code or "ERROR").strip() or "ERROR"
    norm = normalise_text(output, strip_line_numbers=strip_line_numbers)
    lines = key_error_lines(norm)
    basis = lines or norm.split("\n")[-MAX_KEY_LINES:]
    body = "\n".join(line for line in basis if line)
    fp = digest(canonical_json([tool, code, DEFAULT_REDACTOR.text(body)]))
    headline = lines[0] if lines else (basis[0] if basis and basis[0] else code)
    return ErrorSignature(digest=fp, signature=safe_label(headline, SIGNATURE_CHARS), error_code=code, tool=tool)


# --------------------------------------------------------------------------------------------- failing tests
_PYTEST_SUMMARY = re.compile(r"^(?:FAILED|ERROR)\s+(?!\()(\S+?)(?:\s+-\s.*)?$")
_PYTEST_VERBOSE = re.compile(r"^(\S+::\S+)\s+(?:FAILED|ERROR)\b")
_UNITTEST = re.compile(r"^(?:FAIL|ERROR):\s+(\w+)\s+\(([\w.]+)\)")
_GO_TEST = re.compile(r"^\s*--- FAIL:\s+(\S+)")
_CARGO = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+FAILED\b")
_JEST_MARK = re.compile(r"^\s*[✕×✗]\s+(.+?)\s*$")
_JEST_BULLET = re.compile(r"^\s*●\s+(.+?)\s*$")
_FILE_FAIL = re.compile(r"^\s*FAIL\s+(\S.*?)\s*$")
_NUMBERED_SECTION = re.compile(r"^\s*\d+\s+failing\b|^There (?:was|were) \d+ (?:failure|error)s?:")
_NUMBERED_ITEM = re.compile(r"^\s*\d+\)\s+(\S.*?):?\s*$")
_SIMPLE_TEST_PATTERNS = (_PYTEST_SUMMARY, _PYTEST_VERBOSE, _GO_TEST, _CARGO, _JEST_MARK, _JEST_BULLET, _FILE_FAIL)


_PAREN_DURATION = re.compile(r"\(\s*" + _DURATION.pattern + r"\s*\)", re.IGNORECASE)


def _test_id(raw: str) -> str:
    t = _ANSI.sub("", raw)
    t = _DURATION.sub("", _PAREN_DURATION.sub("", t))
    return clip(t, MAX_TEST_ID_CHARS).rstrip(" :")


def extract_failing_tests(output: str) -> tuple[str, ...]:
    """Failing test ids reported by common runners (pytest, unittest, go, cargo, jest/vitest, mocha, phpunit).

    Returns a sorted, de-duplicated tuple (at most :data:`MAX_TEST_IDS`). Only the runners' generic report formats
    are recognised; unknown formats yield ``()`` and stagnation then relies on the error signature.
    """
    found: set[str] = set()
    in_numbered = False
    for raw_line in strip_ansi(output).replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        candidate: str | None = None
        if m := _UNITTEST.match(line):
            name, where = m.group(1), m.group(2)
            candidate = where if where.endswith(f".{name}") else f"{where}.{name}"
        else:
            candidate = next((found_m.group(1) for pat in _SIMPLE_TEST_PATTERNS if (found_m := pat.match(line))), None)
        if candidate is None:
            if _NUMBERED_SECTION.match(line):
                in_numbered = True
                continue
            if in_numbered and (m := _NUMBERED_ITEM.match(line)):
                candidate = m.group(1)
        if candidate:
            tid = _test_id(candidate)
            if tid:
                found.add(DEFAULT_REDACTOR.text(tid))
    return tuple(sorted(found)[:MAX_TEST_IDS])


def failing_tests_fingerprint(test_ids: Iterable[str]) -> str | None:
    """Digest of the *set* of failing tests (order-insensitive); ``None`` for an empty set."""
    ids = sorted({t for t in test_ids if t})
    return digest(canonical_json(ids)) if ids else None


def changed_files_fingerprint(paths: Iterable[str]) -> str | None:
    """Digest of the set of changed paths (normalised, order-insensitive); ``None`` for no paths."""
    norm = sorted({_normalise_path_value(p) for p in paths if p and p.strip()})
    return digest(canonical_json(norm)) if norm else None


_DIFF_INDEX = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+(?: \d+)?$")
_DIFF_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
EMPTY_DIFF_HASH = digest("")


def diff_hash(diff_text: str) -> str:
    """Digest of a unified diff that ignores blob ids, hunk offsets and trailing whitespace.

    Two workspaces with the same changes hash alike even if an unrelated edit shifted line offsets; an empty diff
    hashes to :data:`EMPTY_DIFF_HASH`.
    """
    lines: list[str] = []
    for raw in diff_text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line or _DIFF_INDEX.match(line):
            continue
        lines.append(_DIFF_HUNK.sub("@@", line))
    return digest("\n".join(lines))
