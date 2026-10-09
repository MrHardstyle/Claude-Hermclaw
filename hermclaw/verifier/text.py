"""Text hygiene for persisted verifier output."""

from __future__ import annotations


def pg_safe(text: str) -> str:
    """PostgreSQL text/jsonb cannot hold NUL characters or lone surrogates (non-UTF-8 file names decoded with
    ``surrogateescape``): make both visible as escapes instead of failing the insert."""
    if "\x00" in text:
        text = text.replace("\x00", "\\x00")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "backslashreplace").decode("utf-8")
    return text
