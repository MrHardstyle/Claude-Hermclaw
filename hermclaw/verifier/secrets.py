"""Secret scan over the lines a change *adds* (P21 21.7).

Sources of findings (every finding is blocking):

1. the secret shapes of :mod:`hermclaw.core.redaction` (tokens, ``key = value`` assignments, bearer headers, URL
   credentials) – the same patterns that mask logs/events, so anything the runtime would redact may not be committed;
2. literal secrets registered at runtime in ``DEFAULT_REDACTOR`` (e.g. the database password);
3. additional well-known credential shapes (private key headers, Slack/Google/Stripe/npm tokens, JWTs);
4. a high-entropy token heuristic for string literals and config values.

To keep the scan useful for real code, assignment-style matches are only reported when the value is a *literal*
(quoted in code files; any value in config/shell files) and not an obvious placeholder (``${VAR}``, ``<token>``,
``changeme``, ``example`` …). Findings never carry the secret itself: only path, line, rule and a masked preview.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from hermclaw.core import redaction as _redaction
from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED, Redactor
from hermclaw.verifier.languages import CODE_LANGUAGES, detect_language, is_lock_or_data_file
from hermclaw.verifier.types import AddedLine

# the redaction module's patterns (public alias if a later version exports one)
REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(getattr(_redaction, "SECRET_PATTERNS", None) or _redaction._PATTERNS)

_HEADER = "-----BEGIN "
EXTRA_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(re.escape(_HEADER) + r"(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{20,}\b")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
)

# ``DB_PASSWORD=…`` / ``AWS_SECRET_ACCESS_KEY: …`` / ``spring.datasource.password=…``: the redaction pattern only matches
# the bare keyword (``\bpassword\b``); configuration keys usually carry a prefix.
PREFIXED_ASSIGNMENT = re.compile(
    r"(?i)(\b[A-Za-z0-9_.-]{0,64}?(?:api[_-]?key|access[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token"
    r"|private[_-]?key|client[_-]?secret|token|secret|password|passwd)\b[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{4,})"
)
_ORDERED_PATTERNS: tuple[re.Pattern[str], ...] = (
    *(p for p in REDACTION_PATTERNS if p.groups == 0),
    *(p for p in REDACTION_PATTERNS if p.groups > 0),
    PREFIXED_ASSIGNMENT,
)

_TOKEN_PREFIX_RULES = (
    ("ghp_", "github_token"),
    ("gho_", "github_token"),
    ("ghu_", "github_token"),
    ("ghs_", "github_token"),
    ("ghr_", "github_token"),
    ("glpat-", "gitlab_token"),
    ("sk-", "api_key"),
    ("AKIA", "aws_access_key"),
)

_PLACEHOLDER_START = tuple("${<%!@&*([#")
_PLACEHOLDER_WORDS = frozenset(
    {
        "changeme",
        "change_me",
        "change-me",
        "password",
        "passwd",
        "secret",
        "token",
        "none",
        "null",
        "nil",
        "true",
        "false",
        "undefined",
        "empty",
        "required",
        "optional",
        "string",
        "example",
        "dummy",
        "test",
        "placeholder",
        "redacted",
        "todo",
        "fixme",
        "notset",
        "unset",
        "default",
        "hidden",
        "masked",
    }
)
_PLACEHOLDER_PARTS = (
    "example",
    "placeholder",
    "redacted",
    "dummy",
    "changeme",
    "change_me",
    "your_",
    "your-",
    "xxxx",
    "****",
    "....",
    "${",
    "{{",
)

_CANDIDATE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_HEX = re.compile(r"^[0-9a-fA-F-]+$")
_WORDY = re.compile(r"^[A-Za-z_]+[0-9]*$")
_HASH_CONTEXT = re.compile(r"(?i)(?:sha(?:1|224|256|384|512)[-:]|md5[-:]|base64,|integrity)\W*$")
_KEYWORD_CONTEXT = re.compile(r"(?i)(?:key|secret|token|passw|auth|credential|bearer)")
_CONFIG_LANGS = frozenset({"config", "yaml", "toml", "json", "shell", None})


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line: int
    rule: str
    preview: str

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "rule": self.rule, "preview": self.preview}


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def is_placeholder(value: str) -> bool:
    v = value.strip().strip("\"'`")
    if not v:
        return True
    low = v.lower()
    if v.startswith(_PLACEHOLDER_START) or low in _PLACEHOLDER_WORDS:
        return True
    if any(part in low for part in _PLACEHOLDER_PARTS):
        return True
    return len(set(v)) <= 2 or v.isdigit()


def _mask(line: str, start: int, end: int) -> str:
    masked = line[:start] + REDACTED + line[end:]
    out = DEFAULT_REDACTOR.text(masked).strip()
    return out if len(out) <= 200 else out[:200] + "…"


def _token_rule(value: str) -> str:
    for prefix, rule in _TOKEN_PREFIX_RULES:
        if value.startswith(prefix):
            return rule
    return "secret_token"


class SecretScanner:
    """Scans added lines; stateless apart from the language-aware filters."""

    def __init__(self, *, entropy_threshold: float = 4.3, hex_entropy_threshold: float = 3.0, min_token_length: int = 32) -> None:
        self.entropy_threshold = entropy_threshold
        self.hex_entropy_threshold = hex_entropy_threshold
        self.min_token_length = min_token_length
        self._plain = Redactor()  # patterns only: a difference to DEFAULT_REDACTOR means a registered literal secret

    def scan_file(self, path: str, lines: list[AddedLine]) -> list[SecretFinding]:
        language = detect_language(path)
        noisy = is_lock_or_data_file(path)
        findings: list[SecretFinding] = []
        for added in lines:
            text = added.text
            if len(text) > 20_000:  # minified blobs: scan the head only (bounded work per line)
                text = text[:20_000]
            findings.extend(self.scan_line(path, added.line, text, language=language, entropy=not noisy))
        return findings

    def scan_line(self, path: str, line_no: int, line: str, *, language: str | None = None, entropy: bool = True) -> list[SecretFinding]:
        hits: list[SecretFinding] = []
        seen: set[tuple[int, int]] = set()

        def add(rule: str, start: int, end: int) -> None:
            if any(s <= start < e or start <= s < end for s, e in seen):
                return
            seen.add((start, end))
            hits.append(SecretFinding(path, line_no, rule, _mask(line, start, end)))

        for name, pattern in EXTRA_RULES:
            for m in pattern.finditer(line):
                if name != "private_key" and is_placeholder(m.group(0)):
                    continue
                add(name, m.start(), m.end())
        for pattern in _ORDERED_PATTERNS:  # specific token shapes first, then key/value shapes
            for m in pattern.finditer(line):
                found = self._classify(pattern, m, line, language)
                if found is not None:
                    add(*found)
        if REDACTED in line:
            # e.g. a diff that already went through the redactor (GitReader fallback): something secret was there
            idx = line.index(REDACTED)
            add("redacted_secret", idx, idx + len(REDACTED))
        if getattr(DEFAULT_REDACTOR, "_literals", True):
            masked = DEFAULT_REDACTOR.text(line)
            if masked != self._plain.text(line):  # a literal secret registered with the runtime redactor
                hits.append(SecretFinding(path, line_no, "known_secret", masked.strip()[:200]))
        if entropy:
            for start, end in self._entropy_hits(line, language):
                add("high_entropy", start, end)
        return hits

    @staticmethod
    def _classify(pattern: re.Pattern[str], m: re.Match[str], line: str, language: str | None) -> tuple[str, int, int] | None:
        if pattern.groups >= 2:
            prefix, value, start = m.group(1), m.group(2), m.start(2)
            if "://" in prefix:  # URL credential: user:password@host
                user = prefix.rsplit("://", 1)[1].rstrip(":")
                if is_placeholder(value) or value == user:
                    return None
                return "url_credential", start, m.end(2)
            quoted = prefix.rstrip().endswith(("'", '"'))
            if language in CODE_LANGUAGES and not quoted:
                return None  # ``password = get_password()`` – an expression, not a literal
            if is_placeholder(value):
                return None
            return "secret_assignment", start, m.end(2)
        if pattern.groups == 1:
            value, start = line[m.end(1) : m.end()], m.end(1)
            if is_placeholder(value):
                return None
            return "bearer_token", start, m.end()
        value = m.group(0)
        if is_placeholder(value):
            return None
        return _token_rule(value), m.start(), m.end()

    def _entropy_hits(self, line: str, language: str | None) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for m in _CANDIDATE.finditer(line):
            token = m.group(0).strip("=")
            if len(token) < self.min_token_length:
                continue
            before = line[: m.start()]
            if _HASH_CONTEXT.search(before[-24:]):
                continue
            context_ok = before.rstrip().endswith(("'", '"', "`", "=", ":")) or language in _CONFIG_LANGS
            if not context_ok or token.count("/") >= 2 or is_placeholder(token):
                continue
            if _HEX.match(token):
                if not _KEYWORD_CONTEXT.search(line) or shannon_entropy(token) < self.hex_entropy_threshold:
                    continue
                out.append((m.start(), m.end()))
                continue
            digits = sum(ch.isdigit() for ch in token)
            letters = sum(ch.isalpha() for ch in token)
            mixed = any(ch.islower() for ch in token) and any(ch.isupper() for ch in token)
            if digits < 2 or letters < 8 or not mixed or _WORDY.match(token):
                continue
            if shannon_entropy(token) >= self.entropy_threshold:
                out.append((m.start(), m.end()))
        return out
