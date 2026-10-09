"""Redaction of repository content before it leaves the component (snippets, embedding inputs, excerpts).

:data:`hermclaw.core.redaction.DEFAULT_REDACTOR` masks the known secret shapes (tokens, keys, ``password=...``).
Source code additionally assigns secrets to *prefixed* identifiers (``DB_PASSWORD = '...'``, ``"apiToken": "..."``,
``$client_secret => '...'``) which the word-boundary patterns of the shared redactor do not see; this module adds a
literal-assignment rule for those. Only quoted literals are masked – code such as ``password = form['password']``
stays readable.
"""

from __future__ import annotations

import re

from hermclaw.core.redaction import DEFAULT_REDACTOR, REDACTED

_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (
        [\w$.-]*?
        (?:password|passwd|passphrase|secret|token|api[_-]?key|apikey|private[_-]?key|access[_-]?key|credentials?)
        [\w$.-]*
        ["']?\s*(?:=>|:=|=|:)\s*
    )
    (["'])([^"'\r\n]{4,})(\2)
    """
)


def redact_code(text: str) -> str:
    """Shared redactor plus quoted secret literals assigned to secret-named identifiers/keys."""
    if not text:
        return text
    out = DEFAULT_REDACTOR.text(text)

    def _mask(m: re.Match[str]) -> str:
        if m.group(3) == REDACTED:
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(4)}"

    return _SECRET_ASSIGNMENT.sub(_mask, out)
