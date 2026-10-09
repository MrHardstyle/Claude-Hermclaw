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

_SENSITIVE_KEYS = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|private[_-]?key|credential)")
# numeric telemetry under "sensitive-looking" keys (prompt_tokens, max_tokens, token_count …) is not a secret
_COUNTER_KEYS = re.compile(r"(?i)(tokens|_count|_ms|_seconds|_bytes|_chars)$|^(max|min|num|total)_")


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
            if pat.groups >= 2:
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
