"""Secret redaction for logs, events, prompts and artifacts (Bauplan §35)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "***REDACTED***"

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE|SECRET)[A-Z0-9 ]*-----.*?-----END [A-Z0-9 ]*-----", re.S),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        # optional identifier prefix (DB_PASSWORD, smtp-password, apiToken, X_API_KEY) – never a plural/suffix (max_tokens)
        r"(\b(?:[A-Za-z0-9]+[_-]|[a-z0-9]+(?=[A-Z]))*(?i:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd|"
        r"private[_-]?token|client[_-]?secret)\b[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{4,})"
    ),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),  # GitLab personal access token
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub tokens
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),  # OpenAI-style keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)([^@\s]+)(@)"),
]

#: public, read-only view of the secret shapes (used by the verifier secret scan)
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = tuple(_PATTERNS)

_KEY_VALUE = _PATTERNS[2]
# ``api_key = config["x"]``, ``token = self.token``, ``password = getpass.getpass()``: code that *reads* a secret. Masking
# it showed the coder broken code (``api_key = ***REDACTED***"x"]``), which it copied into new files (BUG-225).
_CODE_REFERENCE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\(\)?|\[)?[)\]}]*$")
_CODE_SIGNS = frozenset(".([)]}")
_PLACEHOLDER_START = ("<", "${", "$", "{{", "%")
_PLACEHOLDER_PARTS = ("your-", "your_", "example", "placeholder", "changeme", "change-me", "change_me", "dummy", "-here", "_here")


# a value made only of these words names a role, not a secret: tests of a token-protected API use "valid_token" and
# "wrong-password", and docs "my-secret-key"; a real credential always carries a part that is no such word (BUG-279).
# The verifier and the redactor share the rule, so a model sees the values a test compares (BUG-293).
_PLACEHOLDER_VOCABULARY = frozenset(
    {
        "valid", "invalid", "wrong", "bad", "good", "correct", "incorrect", "expired", "revoked", "unknown", "other",
        "another", "right", "my", "your", "the", "a", "test", "testing", "tests", "secret", "token", "key", "api",
        "password", "pass", "passwd", "pwd", "user", "admin", "auth", "bearer", "access", "refresh", "session",
        "client", "demo", "local", "dev", "new", "old", "first", "second", "foo", "bar", "baz", "abc", "some", "super",
        "value", "missing", "no", "empty",
    }
)  # fmt: skip
_WORD_SPLIT = re.compile(r"[-_.\s]+")


def _role_word(word: str) -> bool:
    """A placeholder vocabulary word, optionally numbered (``token2``), or a short number of its own."""
    stem = word.rstrip("0123456789")
    return len(word) - len(stem) <= 3 and (stem in _PLACEHOLDER_VOCABULARY or not stem)


def is_role_value(value: str) -> bool:
    """A value made only of role words (``test-token-123``, ``wrong-token``): a test's or a document's stand-in."""
    words = [w for w in _WORD_SPLIT.split(value.lower()) if w]
    return bool(words) and all(_role_word(w) for w in words) and not all(w.isdigit() for w in words)


def is_benign_value(value: str, *, quoted: bool) -> bool:
    """A ``key = value`` match that is no secret: an obvious placeholder, or (unquoted) a short code reference."""
    low = value.lower()
    if low.startswith(_PLACEHOLDER_START) or any(part in low for part in _PLACEHOLDER_PARTS) or not set(low) - set("x*-."):
        return True
    # cycle 2 M3 on rc60: ``token="test-token-123"`` and ``token="wrong-token"`` both showed as the marker, and the review
    # could not tell the valid-token test from the wrong-token test (BUG-293). Unquoted, such a value is a variable:
    # ``{"NOTES_API_TOKEN": token}`` showed as the marker exactly on the line whose bug the coder had to find (BUG-318)
    if is_role_value(value):
        return True
    return not quoted and len(value) <= 60 and bool(_CODE_SIGNS & set(value)) and bool(_CODE_REFERENCE.match(value))


_SENSITIVE_KEYS = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|private[_-]?key|credential)")
# numeric telemetry under "sensitive-looking" keys (prompt_tokens, max_tokens, token_count …) is not a secret
_COUNTER_KEYS = re.compile(r"(?i)(tokens|_count|_ms|_seconds|_bytes|_chars)$|^(max|min|num|total)_")


def _mask_key_value(m: re.Match[str]) -> str:
    if is_benign_value(m.group(2), quoted=m.group(1).endswith(("'", '"'))):
        return m.group(0)
    return m.group(1) + REDACTED


class Redactor:
    """Masks known secret shapes plus explicitly registered literal secrets."""

    def __init__(self, literals: Iterable[str] = ()) -> None:
        self._literals = sorted({s for s in literals if s and len(s) >= 4}, key=len, reverse=True)

    def add_literal(self, value: str) -> None:
        if value and len(value) >= 4 and value not in self._literals:
            self._literals.append(value)
            self._literals.sort(key=len, reverse=True)

    def text(self, value: str) -> str:
        out = value
        for lit in self._literals:
            out = out.replace(lit, REDACTED)
        for pat in _PATTERNS:
            if pat is _KEY_VALUE:
                out = pat.sub(_mask_key_value, out)
            elif pat.groups >= 2:
                out = pat.sub(lambda m: m.group(1) + REDACTED + (m.group(3) if m.re.groups >= 3 else ""), out)
            else:
                out = pat.sub(REDACTED, out)
        return out

    def obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            red: dict[Any, Any] = {}
            for k, v in value.items():
                numeric = isinstance(v, int | float) and not isinstance(v, bool)
                secret_shaped = isinstance(v, str) or (numeric and isinstance(k, str) and not _COUNTER_KEYS.search(k))
                if isinstance(k, str) and _SENSITIVE_KEYS.search(k) and v != "" and secret_shaped:
                    red[k] = REDACTED
                else:
                    red[k] = self.obj(v)
            return red
        if isinstance(value, list | tuple):
            return [self.obj(v) for v in value]
        return value


DEFAULT_REDACTOR = Redactor()


def redact(value: Any) -> Any:
    return DEFAULT_REDACTOR.obj(value)
