"""Command policy for SSH/admin commands (P26 26.3, Bauplan §29, research 20261008-021).

Every remote command is classified as ``read`` | ``mutate`` | ``destructive`` | ``forbidden``:

1. Malformed input is ``forbidden``: empty, control characters/new lines (one command line only),
   unbalanced quotes, longer than :data:`MAX_COMMAND_CHARS`.
2. ``sudo``/``su``/``doas``/``pkexec``/``run0`` are ``forbidden`` unless the whole command is exactly
   ``sudo [-n] <entry>`` for an entry of the host's ``ssh.allow_sudo_commands``; the entry is then
   classified on its own (a forbidden entry stays forbidden).
3. The generic patterns of ``policies.commands`` (shared with the sandbox tool engine,
   :class:`hermclaw.tools.classify.CommandClassifier`, which also sees quoted sub-commands) decide
   ``forbidden``/``destructive``/``mutate``.
4. ``read`` additionally requires that *every* part of a pipeline/list matches a read pattern, that no
   part writes a file (output redirection other than ``/dev/null``), contains a command substitution or
   uses a flag that turns a read tool into an executor/writer (``find -exec``, ``rg --pre``,
   ``journalctl --vacuum-*``, ``ip … add``…). Anything else – including unclassified commands – is at
   least ``mutate``: on an admin host nothing is read-only by default.

Decision on top of the class: ``forbidden`` is always refused; ``mutate``/``destructive`` commands that chain
(``;`` ``&&`` ``||`` ``|`` ``&``, sub-shells, `` ` `` ``$(``, process substitution, here-documents) are refused
unless they full-match an explicitly allow-listed pattern; ``destructive`` always needs an approval;
``mutate`` needs one when ``policies.ssh.require_approval_for_mutations`` is set. An approval is identified
by a token derived from host, command and step (see :func:`approval_token`).
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from hermclaw.core.config import CommandPolicy, SshPolicy
from hermclaw.core.errors import ConfigError
from hermclaw.sshtools.errors import (
    SSH_APPROVAL_REQUIRED,
    SSH_COMMAND_FORBIDDEN,
    SSH_COMMAND_INVALID,
    SSH_COMPOUND_NOT_ALLOWED,
    SSH_NOT_READ_ONLY,
    SSH_SUDO_NOT_ALLOWED,
)
from hermclaw.sshtools.registry import SshHost
from hermclaw.tools.classify import CommandClassifier, command_segments

SshCommandClass = Literal["read", "mutate", "destructive", "forbidden"]
MAX_COMMAND_CHARS = 8000
PRIVILEGE_WORDS = frozenset({"sudo", "su", "doas", "pkexec", "run0", "sudoedit"})
APPROVAL_TOKEN_RE = re.compile(r"^ssh-[0-9a-f]{48}$")
#: connecting onward from an allow-listed host would bypass the host allow-list (and the audit)
REMOTE_HOP_WORDS = frozenset({"ssh", "scp", "sftp", "slogin", "rsh", "rlogin", "rcp", "telnet"})
#: interpreters whose ``-c`` payload / ``eval`` argument is a nested command line (counts as chaining)
NESTED_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "mksh", "fish", "busybox"})
_SHORT_C_OPTION = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
_SUDO_PREFIX = re.compile(r"sudo(?:[ \t]+-n)?[ \t]+(\S.*)", re.S)

_HARMLESS_TARGETS = frozenset({"/dev/null"})
# read tools whose options turn them into executors or writers (generic hardening, independent of config)
_READ_ESCALATING_OPTIONS: dict[str, frozenset[str]] = {
    "find": frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls", "-fprint", "-fprint0", "-fprintf"}),
    "journalctl": frozenset(
        {
            "--rotate",
            "--flush",
            "--sync",
            "--relinquish-var",
            "--smart-relinquish-var",
            "--setup-keys",
            "--update-catalog",
            "--cursor-file",  # writes the cursor of the last entry to FILE
        }
    ),
    "ss": frozenset({"-K", "--kill", "-D", "--diag"}),  # --diag=FILE dumps raw socket data into a file
    "nginx": frozenset({"-s", "-g", "-c", "-p", "-e"}),  # signals, foreign config/prefix/error log (creates files, loads modules)
    "systemctl": frozenset({"-H", "--host", "-M", "--machine"}),  # -H connects onward over ssh (bypasses the allow-list)
    "rg": frozenset({"--pre", "--pre-glob", "--hostname-bin"}),
}
_READ_ESCALATING_PREFIXES: dict[str, tuple[str, ...]] = {"journalctl": ("--vacuum",), "rg": ("--pre=", "--pre-glob=")}
# flag letters inside short-option clusters (ss -tK, systemctl -qH host, nginx -tc file)
_READ_ESCALATING_SHORT: dict[str, str] = {"ss": "KD", "systemctl": "HM", "nginx": "sgcpe"}
_READ_ESCALATING_WORDS: dict[str, frozenset[str]] = {
    "ip": frozenset(
        {"add", "del", "delete", "change", "replace", "set", "flush", "append", "prepend", "exec", "attach", "detach", "save", "restore"}
    ),
}
#: ``ip [OPTIONS] OBJECT [COMMAND …]`` is only read-only with one of these commands spelled out (iproute2 accepts
#: abbreviations such as ``ip a a`` = ``ip address add``, so mutating verbs cannot be recognised by a deny-list)
_IP_READ_COMMANDS = frozenset({"show", "list", "lst", "ls", "get"})
#: the command word of a read part: a bare tool name – no ``VAR=value`` prefix, no path, no shell syntax
_PLAIN_TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


# ============================================================================================ shell scanning
@dataclass
class ShellScan:
    """Top-level structure of one POSIX-shell command line (quote aware)."""

    segments: list[str] = field(default_factory=list)  # raw text of each simple command
    operators: list[str] = field(default_factory=list)  # ; && || | & ( ) { } newline
    substitutions: list[str] = field(default_factory=list)  # $( ` <( >(
    write_targets: list[str] = field(default_factory=list)  # output redirection targets (files)
    heredoc: bool = False
    balanced: bool = True

    @property
    def compound(self) -> bool:
        return bool(self.operators or self.substitutions or self.heredoc)


def _word_start(text: str, i: int) -> bool:
    return i == 0 or text[i - 1] in " \t;&|()"


def _read_word(text: str, i: int) -> tuple[str, int]:
    """The redirection target starting at ``i`` (leading blanks skipped); quotes are kept verbatim."""
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    start = i
    quote: str | None = None
    while i < n:
        c = text[i]
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif c in " \t;&|()<>":
            break
        i += 1
    return text[start:i], i


def scan_shell(command: str) -> ShellScan:
    scan = ShellScan()
    cur: list[str] = []

    def flush() -> None:
        part = "".join(cur).strip()
        if part:
            scan.segments.append(part)
        cur.clear()

    s, n, i = command, len(command), 0
    state: str | None = None
    while i < n:
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        if state == "'":
            cur.append(c)
            if c == "'":
                state = None
            i += 1
            continue
        if state == '"':
            cur.append(c)
            if c == "\\" and nxt:
                cur.append(nxt)
                i += 2
                continue
            if c == '"':
                state = None
            elif c == "`":
                scan.substitutions.append("`")
            elif c == "$" and nxt == "(":
                scan.substitutions.append("$(")
            i += 1
            continue
        # ---- unquoted
        if c == "\\":
            cur.append(s[i : i + 2])
            i += 2
            continue
        if c in "'\"":
            state = c
            cur.append(c)
            i += 1
            continue
        if c == "`":
            scan.substitutions.append("`")
            cur.append(c)
            i += 1
            continue
        if c == "$" and nxt == "(":
            scan.substitutions.append("$(")
            cur.append("$(")
            i += 2
            continue
        if c in "<>" and nxt == "(":
            scan.substitutions.append(c + "(")
            cur.append(c + "(")
            i += 2
            continue
        if s.startswith(("&&", "||"), i):
            scan.operators.append(s[i : i + 2])
            flush()
            i += 2
            continue
        if c == "&" and nxt == ">":  # bash: &> file / &>> file
            j = i + 3 if s.startswith("&>>", i) else i + 2
            target, j = _read_word(s, j)
            if target not in _HARMLESS_TARGETS:
                scan.write_targets.append(target or "?")
            cur.append(s[i:j])
            i = j
            continue
        if c in ";|&\n":
            scan.operators.append(c)
            flush()
            i += 1
            continue
        if c in "()":
            scan.operators.append(c)
            flush()
            i += 1
            continue
        if c == "{" and _word_start(s, i) and nxt in (" ", "\t", ""):
            scan.operators.append(c)
            flush()
            i += 1
            continue
        if c == "}" and _word_start(s, i) and nxt in (" ", "\t", ";", ""):
            scan.operators.append(c)
            flush()
            i += 1
            continue
        if c == ">":
            j = i + 1
            if nxt in (">", "|"):
                j += 1
            elif nxt == "&":  # >&N, >&- duplicate a descriptor
                k = i + 2
                if k < n and (s[k].isdigit() or s[k] == "-"):
                    while k < n and (s[k].isdigit() or s[k] == "-"):
                        k += 1
                    cur.append(s[i:k])
                    i = k
                    continue
                j += 1  # bash: >&file
            target, j = _read_word(s, j)
            if target not in _HARMLESS_TARGETS:
                scan.write_targets.append(target or "?")
            cur.append(s[i:j])
            i = j
            continue
        if c == "<":
            if s.startswith("<<<", i):
                cur.append("<<<")
                i += 3
                continue
            if nxt == "<":
                scan.heredoc = True
                cur.append("<<")
                i += 2
                continue
            if nxt == ">":  # <> opens read-write
                target, j = _read_word(s, i + 2)
                scan.write_targets.append(target or "?")
                cur.append(s[i:j])
                i = j
                continue
            cur.append(c)
            i += 1
            continue
        cur.append(c)
        i += 1
    if state is not None:
        scan.balanced = False
    flush()
    return scan


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _nested_shell(scan: ShellScan) -> bool:
    """``sh -c '…'``, ``bash -lc …``, ``eval …`` (also behind wrappers): a nested command line hides chaining."""
    for segment in scan.segments:
        words = _words(segment)
        for i, word in enumerate(words):
            tool = posixpath.basename(word)
            if tool == "eval":
                return True
            if tool in NESTED_SHELLS and any(_SHORT_C_OPTION.match(w) for w in words[i + 1 :]):
                return True
    return False


def _ip_not_read(words: list[str]) -> str | None:
    """``ip`` is read-only for ``ip [OPTIONS] OBJECT`` or ``ip [OPTIONS] OBJECT show|list|get …`` without ``-batch``."""
    for w in words[1:]:
        if w.startswith("-") and len(w) >= 2 and "-batch".startswith(w.split("=", 1)[0]):
            return "'ip -batch' runs commands from a file"
    plain = [w for w in words[1:] if not w.startswith("-")]
    if len(plain) >= 2 and plain[1] not in _IP_READ_COMMANDS:
        return f"'ip {plain[0]} {plain[1]}' is not a spelled-out show/list/get command"
    return None


# ============================================================================================ classification
@dataclass(frozen=True)
class SshClassification:
    kind: SshCommandClass
    reason: str
    pattern: str | None = None
    code: str | None = None  # stable code when the class alone refuses the command
    sudo: bool = False  # exact allow-listed sudo command
    compound: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "reason": self.reason, "pattern": self.pattern, "sudo": self.sudo, "compound": self.compound}


class Verdict(StrEnum):
    allow = "allow"
    approval_required = "approval_required"
    deny = "deny"


@dataclass(frozen=True)
class SshDecision:
    verdict: Verdict
    classification: SshClassification
    reason: str
    code: str | None = None
    approval_token: str | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict == Verdict.allow


def approval_token(host_id: str, subject: str, step_id: uuid.UUID | str | None) -> str:
    """Deterministic approval id for one (host, exact command/upload, step) triple.

    It identifies – it does not authorise: only an operator entry in the job's ``ssh_approvals`` does.
    """
    material = f"hermclaw-ssh-approval/v1\x00{host_id}\x00{subject.strip()}\x00{step_id or '-'}"
    return "ssh-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:48]


def _compile(patterns: Iterable[str], what: str) -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    for pat in patterns:
        try:
            out.append(re.compile(pat))
        except re.error as exc:
            raise ConfigError(f"invalid regex in {what}: {pat!r}: {exc}") from exc
    return out


class SshCommandPolicy:
    """Classifies remote commands and decides allow / approval / deny."""

    def __init__(
        self,
        command_policy: CommandPolicy,
        ssh_policy: SshPolicy,
        *,
        compound_allowlist: Iterable[str] = (),
    ) -> None:
        self.ssh_policy = ssh_policy
        self._classifier = CommandClassifier(command_policy)
        self._read = _compile(command_policy.read_patterns, "policies.commands.read_patterns")
        self._compound_allow = _compile(compound_allowlist, "ssh compound allow-list")

    @property
    def require_approval_for_mutations(self) -> bool:
        return self.ssh_policy.require_approval_for_mutations

    # ---------------------------------------------------------------------------------------- classify
    def classify(self, host: SshHost, command: str) -> SshClassification:
        text = command.strip()
        if not text:
            return SshClassification("forbidden", "empty command", code=SSH_COMMAND_INVALID)
        if len(text) > MAX_COMMAND_CHARS:
            return SshClassification("forbidden", f"command longer than {MAX_COMMAND_CHARS} characters", code=SSH_COMMAND_INVALID)
        if any((ord(ch) < 32 and ch != "\t") or ord(ch) == 127 for ch in text):
            return SshClassification("forbidden", "control characters / multiple lines are not allowed", code=SSH_COMMAND_INVALID)
        scan = scan_shell(text)
        if not scan.balanced:
            return SshClassification("forbidden", "unbalanced quotes", code=SSH_COMMAND_INVALID)

        inner, sudo = text, False
        allowed_sudo = self._sudo_entry(host, text)
        if allowed_sudo is not None:
            inner, sudo = allowed_sudo, True
        elif any(words and posixpath.basename(words[0]) in PRIVILEGE_WORDS for words in command_segments(text)):
            return SshClassification(
                "forbidden",
                f"sudo/su is only allowed for the exact allow_sudo_commands entries of host '{host.id}'",
                code=SSH_SUDO_NOT_ALLOWED,
            )
        if sudo:
            scan = scan_shell(inner)

        base = self._classifier.classify(inner)
        compound = scan.compound or _nested_shell(scan)
        if base.kind == "forbidden":
            return SshClassification("forbidden", base.reason, base.pattern, code=SSH_COMMAND_FORBIDDEN, sudo=sudo, compound=compound)
        if base.kind in ("destructive", "mutate"):
            kind: SshCommandClass = "destructive" if base.kind == "destructive" else "mutate"
            cls = SshClassification(kind, base.reason, base.pattern, sudo=sudo, compound=compound)
        else:
            not_read = self._not_read_reason(scan)
            if not_read is None:
                # every top-level part starts with a read tool and nothing is substituted: no hop / escalation possible
                return SshClassification("read", "every part matches a read pattern", sudo=sudo, compound=compound)
            cls = SshClassification("mutate", not_read, sudo=sudo, compound=compound)
        # non-read commands: look into quoted/sub-shell parts as well (conservative – they need approval anyway)
        hops = sorted({w[0] for w in command_segments(inner) if w and w[0] in REMOTE_HOP_WORDS})
        if hops:
            return SshClassification(
                "forbidden",
                f"'{hops[0]}' from the target host would bypass the host allow-list; use the SSH tool for that host instead",
                code=SSH_COMMAND_FORBIDDEN,
                sudo=sudo,
                compound=compound,
            )
        if not sudo:
            for segment in scan.segments:
                if any(posixpath.basename(w) in PRIVILEGE_WORDS for w in _words(segment)):
                    return SshClassification(
                        "forbidden", "privilege escalation inside a non-read command", code=SSH_SUDO_NOT_ALLOWED, compound=compound
                    )
        return cls

    @staticmethod
    def _sudo_entry(host: SshHost, text: str) -> str | None:
        """The allow-listed command if ``text`` is exactly ``sudo [-n] <entry>`` (byte-exact after the prefix)."""
        match = _SUDO_PREFIX.fullmatch(text)
        if match is None:
            return None
        rest = match.group(1)
        for entry in host.allow_sudo_commands:
            entry_match = _SUDO_PREFIX.fullmatch(entry)
            inner = entry_match.group(1) if entry_match else entry
            if rest == inner:
                return inner
        return None

    def _not_read_reason(self, scan: ShellScan) -> str | None:
        if scan.substitutions:
            return "command substitution is not a read command"
        if scan.heredoc:
            return "here-documents are not read commands"
        if scan.write_targets:
            return f"output redirection writes {scan.write_targets[0]!r}"
        if "&" in scan.operators:
            return "background jobs keep running on the host after the session"
        if not scan.segments:
            return "no command"
        for segment in scan.segments:
            words = _words(segment)
            if not words:
                return "no command"
            if not _PLAIN_TOOL_RE.match(words[0]):
                # 'cat=1 python3 …' runs python3, '/tmp/ls' is not ls: only a bare tool name can be a read tool
                return "a read part must start with a plain tool name (no VAR=value prefix, path or shell syntax)"
            if not self._matches_read(words):
                return "unclassified command (treated as mutation on remote hosts)"
            tool = words[0]
            if tool == "ip":
                ip_problem = _ip_not_read(words)
                if ip_problem is not None:
                    return ip_problem
            opts = _READ_ESCALATING_OPTIONS.get(tool, frozenset())
            prefixes = _READ_ESCALATING_PREFIXES.get(tool, ())
            letters = _READ_ESCALATING_SHORT.get(tool, "")
            verbs = _READ_ESCALATING_WORDS.get(tool, frozenset())
            for w in words[1:]:
                flag = w.split("=", 1)[0]
                if flag in opts or (prefixes and w.startswith(prefixes)) or w in verbs:
                    return f"'{tool} {flag}' can execute or write"
                if letters and w.startswith("-") and not w.startswith("--") and any(ch in w[1:] for ch in letters):
                    return f"'{tool} {w}' can execute or write"
        return None

    def _matches_read(self, words: list[str]) -> bool:
        """A read pattern matches the part from its first character and ends on a word boundary of the shell words.

        ``cat.sh``/``ls-x`` satisfy ``^(ls|cat)\\b`` on the raw text but are different programs; matching the
        re-joined shell words and requiring the match to end at a word end closes that gap for any configured pattern.
        """
        canonical = " ".join(words)
        for rx in self._read:
            match = rx.match(canonical)
            if match is None:
                continue
            end = match.end()
            if end == len(canonical) or canonical[end] == " " or (end > 0 and canonical[end - 1] == " "):
                return True
        return False

    # ---------------------------------------------------------------------------------------- decide
    def decide(
        self,
        host: SshHost,
        command: str,
        *,
        step_id: uuid.UUID | str | None,
        read_only: bool = False,
    ) -> SshDecision:
        cls = self.classify(host, command)
        if cls.kind == "forbidden":
            return SshDecision(Verdict.deny, cls, f"forbidden: {cls.reason}", cls.code or SSH_COMMAND_FORBIDDEN)
        if read_only and cls.kind != "read":
            return SshDecision(
                Verdict.deny,
                cls,
                f"ssh_read only runs read commands; this is {cls.kind} ({cls.reason}) – use ssh_command",
                SSH_NOT_READ_ONLY,
            )
        if cls.kind == "read":
            return SshDecision(Verdict.allow, cls, "read command")
        if cls.compound and not cls.sudo and not self._compound_allowed(command):
            return SshDecision(
                Verdict.deny,
                cls,
                "chained/compound mutating commands (; && || | & $( ` …) need an explicit allow-list entry; "
                "run the parts as separate commands",
                SSH_COMPOUND_NOT_ALLOWED,
            )
        return self._approval(host, cls, command, step_id)

    def decide_builtin(
        self,
        host: SshHost,
        kind: SshCommandClass,
        subject: str,
        *,
        step_id: uuid.UUID | str | None,
        reason: str,
    ) -> SshDecision:
        """Decision for a runtime-built command (upload, service status) whose class is known by construction."""
        cls = SshClassification(kind, reason)
        if kind == "forbidden":
            return SshDecision(Verdict.deny, cls, reason, SSH_COMMAND_FORBIDDEN)
        if kind == "read":
            return SshDecision(Verdict.allow, cls, reason)
        return self._approval(host, cls, subject, step_id)

    def _approval(self, host: SshHost, cls: SshClassification, subject: str, step_id: uuid.UUID | str | None) -> SshDecision:
        needs = cls.kind == "destructive" or (cls.kind == "mutate" and self.ssh_policy.require_approval_for_mutations)
        if not needs:
            return SshDecision(Verdict.allow, cls, f"{cls.kind} command (approval not required by policy)")
        token = approval_token(host.id, subject, step_id)
        return SshDecision(
            Verdict.approval_required,
            cls,
            f"{cls.kind} command on '{host.id}' needs an operator approval ({token}): {cls.reason}",
            SSH_APPROVAL_REQUIRED,
            token,
        )

    def _compound_allowed(self, command: str) -> bool:
        text = command.strip()
        return any(rx.fullmatch(text) for rx in self._compound_allow)


__all__ = [
    "APPROVAL_TOKEN_RE",
    "MAX_COMMAND_CHARS",
    "NESTED_SHELLS",
    "PRIVILEGE_WORDS",
    "REMOTE_HOP_WORDS",
    "ShellScan",
    "SshClassification",
    "SshCommandClass",
    "SshCommandPolicy",
    "SshDecision",
    "Verdict",
    "approval_token",
    "scan_shell",
]
