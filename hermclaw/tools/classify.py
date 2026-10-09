"""Command classification for ``run_command`` / ``run_test`` (P17 17.5, Bauplan §27 "command classification").

Classes, from most to least severe: ``forbidden`` (always refused), ``destructive`` (refused unless the step
capability explicitly allows it), ``mutate`` / ``read`` / ``unknown`` (executed in the sandbox; the post-command
scope audit decides which file changes survive).

Patterns come from ``policies.commands`` and are matched case-insensitively against several views of the command
so trivial obfuscation does not bypass them: the raw text, a de-quoted/de-escaped view, a view where quotes and
sub-shell markers become command separators (``bash -c "sudo x"`` -> ``bash -c ; sudo x``) and a canonical view in
which every segment starts with its real command word (``env A=1 nohup /usr/bin/sudo x`` -> ``sudo x``).

Independently of the configuration, git subcommands that change repository state are always forbidden: Git
mutations are runtime-controlled (gitops). Only an allow-list of read-only subcommands may run.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Literal

from hermclaw.core.config import CommandPolicy
from hermclaw.core.errors import ConfigError

CommandKind = Literal["forbidden", "destructive", "mutate", "read", "unknown"]

_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;&|\n]")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_NUMERIC = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_WRAPPERS = frozenset({"env", "nohup", "exec", "time", "command", "builtin", "setsid", "stdbuf", "ionice", "nice", "timeout", "xargs", "chronic"})

GIT_READ_SUBCOMMANDS = frozenset(
    {
        "status", "diff", "log", "show", "grep", "ls-files", "ls-tree", "blame", "annotate", "rev-parse", "rev-list",
        "describe", "shortlog", "cat-file", "diff-tree", "diff-files", "diff-index", "for-each-ref", "name-rev",
        "show-ref", "check-ignore", "check-attr", "count-objects", "var", "version", "help", "whatchanged",
        "merge-base", "cherry", "range-diff", "verify-commit", "verify-tag",
    }
)  # fmt: skip
_GIT_OPTS_WITH_ARG = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"})
_GIT_BRANCH_MUTATING_OPTS = frozenset(
    {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "--delete", "--move", "--copy", "--force", "-u", "--set-upstream-to",
     "--unset-upstream", "--edit-description", "--track", "--no-track", "--create-reflog"}
)  # fmt: skip


@dataclass(frozen=True)
class CommandClassification:
    kind: CommandKind
    reason: str
    pattern: str | None = None

    @property
    def refused_always(self) -> bool:
        return self.kind == "forbidden"


def _compile(patterns: list[str], section: str) -> list[tuple[str, re.Pattern[str]]]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for pat in patterns:
        try:
            out.append((pat, re.compile(pat, re.IGNORECASE)))
        except re.error as exc:
            raise ConfigError(f"invalid regex in policies.commands.{section}: {pat!r}: {exc}") from exc
    return out


def _dequoted(command: str) -> str:
    text = re.sub(r"\\(.)", r"\1", command)
    return re.sub(r"[\"'`]", "", text)


def _segmented(command: str) -> str:
    text = re.sub(r"\\(.)", r"\1", command)
    text = text.replace("$(", " ; ").replace("<(", " ; ").replace(">(", " ; ")
    return re.sub(r"[\"'`(){}]", " ; ", text)


def _segment_words(segment: str) -> list[str]:
    words = segment.split()
    i = 0
    while i < len(words):
        word = words[i]
        base = posixpath.basename(word)
        if _ASSIGNMENT.match(word):
            i += 1
            continue
        if base in _WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or _NUMERIC.match(words[i]) or _ASSIGNMENT.match(words[i])):
                i += 1
            continue
        break
    rest = words[i:]
    if rest:
        rest[0] = posixpath.basename(rest[0]) or rest[0]
    return rest


def command_segments(command: str) -> list[list[str]]:
    """Canonical word lists of every simple command inside ``command`` (including quoted sub-commands)."""
    segments: list[list[str]] = []
    for part in _SEGMENT_SPLIT.split(_segmented(command)):
        words = _segment_words(part)
        if words:
            segments.append(words)
    return segments


def _canonical(command: str) -> str:
    return " ; ".join(" ".join(words) for words in command_segments(command))


def git_mutation(words: list[str]) -> str | None:
    """Return the offending git subcommand if ``words`` (a canonical segment) changes repository state."""
    if not words or words[0] != "git":
        return None
    i = 1
    while i < len(words) and words[i].startswith("-"):
        opt = words[i]
        if opt in ("--version", "--help", "-h"):
            return None
        i += 2 if opt in _GIT_OPTS_WITH_ARG else 1
    if i >= len(words):
        return None
    sub, args = words[i], words[i + 1 :]
    positional = [a for a in args if not a.startswith("-")]
    if sub in GIT_READ_SUBCOMMANDS:
        return None
    read_only = False
    if sub == "branch":
        read_only = not positional and not any(a.split("=", 1)[0] in _GIT_BRANCH_MUTATING_OPTS for a in args)
    elif sub == "stash":
        read_only = bool(args) and args[0] in ("list", "show")
    elif sub == "config":
        read_only = bool(args) and args[0] in ("--get", "--get-all", "--get-regexp", "--list", "-l")
    elif sub == "remote":
        read_only = not args or args == ["-v"] or args[0] in ("get-url", "show")
    elif sub == "apply":
        read_only = bool({"--check", "--stat", "--numstat", "--summary"} & set(args)) and "--apply" not in args
    elif sub in ("worktree", "notes"):
        read_only = bool(args) and args[0] in ("list", "show")
    elif sub == "tag":
        read_only = not args or args[0] in ("-l", "--list")
    elif sub == "submodule":
        read_only = bool(args) and args[0] == "status"
    elif sub == "reflog":
        read_only = not args or args[0] == "show"
    elif sub == "symbolic-ref":
        read_only = len(positional) <= 1 and not ({"-d", "--delete"} & set(args))
    return None if read_only else sub


class CommandClassifier:
    def __init__(self, policy: CommandPolicy) -> None:
        self.forbidden = _compile(policy.forbidden_patterns, "forbidden_patterns")
        self.destructive = _compile(policy.destructive_patterns, "destructive_patterns")
        self.mutate = _compile(policy.mutate_patterns, "mutate_patterns")
        self.read = _compile(policy.read_patterns, "read_patterns")

    @staticmethod
    def views(command: str) -> list[str]:
        out: list[str] = []
        for view in (command, _dequoted(command), _segmented(command), _canonical(command)):
            if view not in out:
                out.append(view)
        return out

    def classify(self, command: str) -> CommandClassification:
        if not command.strip():
            return CommandClassification("forbidden", "empty command")
        views = self.views(command)
        for words in command_segments(command):
            sub = git_mutation(words)
            if sub is not None:
                return CommandClassification(
                    "forbidden",
                    f"'git {sub}' changes repository state; git mutations are runtime-controlled "
                    "(edit files with write_file/replace_text/apply_patch, the runtime commits)",
                    "builtin:git-mutation",
                )
        for kind, patterns in (("forbidden", self.forbidden), ("destructive", self.destructive), ("mutate", self.mutate)):
            for raw, rx in patterns:
                if any(rx.search(v) for v in views):
                    return CommandClassification(kind, f"matches {kind} pattern", raw)  # type: ignore[arg-type]
        for raw, rx in self.read:
            if rx.search(command):
                return CommandClassification("read", "matches read pattern", raw)
        return CommandClassification("unknown", "no pattern matched")
