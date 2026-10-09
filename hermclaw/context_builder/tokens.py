"""Token estimation for the context builder (DECISIONS D-008, step 16.2).

No tokenizer is downloaded: the estimate is a conservative 3.2 characters per token. To stay conservative for
non-ASCII text (umlauts, CJK, emoji usually tokenise to one or more tokens per code point) every non-ASCII code
point is costed as four characters, i.e. more than one token. Exact counts come back from Ollama as
``prompt_eval_count`` and are used as telemetry feedback only.

All budget arithmetic uses integer *cost* (character equivalents) so that it is exact and deterministic:
``tokens = ceil(cost / 3.2) = ceil(cost * 5 / 16)``.
"""

from __future__ import annotations

CHARS_PER_TOKEN = 3.2
NON_ASCII_COST = 4  # character equivalents per non-ASCII code point (> 3.2, so >= 1 token each)
_NUM, _DEN = 5, 16  # 1 / 3.2 == 5 / 16 exactly


def char_cost(text: str) -> int:
    """Cost of ``text`` in character equivalents (ASCII = 1, non-ASCII = :data:`NON_ASCII_COST`)."""
    if text.isascii():
        return len(text)
    extra = sum(1 for ch in text if ord(ch) > 127)
    return len(text) + (NON_ASCII_COST - 1) * extra


def tokens_for_cost(cost: int) -> int:
    """``ceil(cost / 3.2)`` without floating point error."""
    if cost <= 0:
        return 0
    return (cost * _NUM + _DEN - 1) // _DEN


def cost_for_tokens(tokens: int) -> int:
    """Largest cost whose estimate does not exceed ``tokens`` (``floor(tokens * 3.2)``)."""
    if tokens <= 0:
        return 0
    return tokens * _DEN // _NUM


def estimate_tokens(text: str) -> int:
    """Conservative token estimate of ``text`` (3.2 chars/token, non-ASCII costed as one token or more)."""
    return tokens_for_cost(char_cost(text))


def clip_to_cost(text: str, max_cost: int) -> str:
    """Longest prefix of ``text`` whose :func:`char_cost` is ``<= max_cost``."""
    if max_cost <= 0:
        return ""
    if text.isascii():
        return text[:max_cost]
    used = 0
    for i, ch in enumerate(text):
        used += 1 if ord(ch) <= 127 else NON_ASCII_COST
        if used > max_cost:
            return text[:i]
    return text


def clip_tail_to_cost(text: str, max_cost: int) -> str:
    """Longest suffix of ``text`` whose :func:`char_cost` is ``<= max_cost``."""
    if max_cost <= 0:
        return ""
    if text.isascii():
        return text[-max_cost:] if len(text) > max_cost else text
    used = 0
    for i in range(len(text) - 1, -1, -1):
        used += 1 if ord(text[i]) <= 127 else NON_ASCII_COST
        if used > max_cost:
            return text[i + 1 :]
    return text
