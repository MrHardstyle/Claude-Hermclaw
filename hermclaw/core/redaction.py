"""Secret redaction for logs, events, prompts and artifacts (Bauplan §35)."""

from __future__ import annotations

import keyword
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
        r"private[_-]?token|client[_-]?secret)\b[\"']?[ \t]*[:=][ \t]*[\"']?)([^\s\"',;]{4,})"
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
# ``if username and not password:`` + ``raise ValueError(…)`` on the next line: the key's colon ends a block, and the
# word after it is code. The separator never crosses a line, and an unquoted keyword is no value (live G1 rc119: the
# review quoted the check, the coder saw ``***REDACTED*** ValueError``, copied it, and two attempts were refused, BUG-398)
_CODE_KEYWORDS = frozenset({*keyword.kwlist, "null", "nil", "true", "false", "undefined", "throw", "then", "do"})
# ``api_key = config["x"]``, ``token = self.token``, ``password = getpass.getpass()``: code that *reads* a secret. Masking
# it showed the coder broken code (``api_key = ***REDACTED***"x"]``), which it copied into new files (BUG-225).
_CODE_REFERENCE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\(\)?|\[)?[)\]}]*$")
_CODE_SIGNS = frozenset(".([)]}")
# ``password=_secret_value(entry, …)``, ``"has_api_key": bool(self.api_key)``, ``token = secrets.token_urlsafe(32)``,
# ``{"password": server.password}).encode(…)``: a call or a subscript is code that computes a value. Masked, the coder saw
# broken lines, copied them into its rewrite, and every such write was refused (live G1 rc90, 4x, BUG-355).
_CODE_EXPRESSION = re.compile(r"^[a-z_]\w*(?:\.[A-Za-z_]\w*)*[(\[)\]}][\w.()\[\]{}]*$")
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
        "value", "missing", "no", "empty", "uuid", "guid",
        # German projects name their stand-ins in German: "geheimer-token", "neues-passwort" (live G1 rc127, BUG-410)
        "geheim", "geheime", "geheimer", "geheimes", "passwort", "kennwort", "schluessel", "schlüssel", "zugang",
        "neu", "neue", "neuer", "neues", "alt", "alte", "alter", "altes", "falsch", "falsche", "falscher", "falsches",
        "gueltig", "gültig", "gueltiger", "gültiger", "ungueltig", "ungültig", "ungueltiger", "ungültiger", "mein",
        "meine", "dein", "deine", "anderer", "anderes", "benutzer",
        # status shown instead of a secret: "gesetzt", "nicht gesetzt", "set" (live G1 rc132, BUG-416)
        "set", "unset", "gesetzt", "nicht", "leer", "masked", "hidden",
    }
)  # fmt: skip
_WORD_SPLIT = re.compile(r"[-_.\s]+")


def _role_word(word: str) -> bool:
    """A placeholder vocabulary word, optionally numbered (``token2``), a short number of its own, or a single letter
    that tells stand-ins apart (``value-a`` … ``value-g``, live G1 rc132, BUG-416)."""
    stem = word.rstrip("0123456789")
    return len(word) - len(stem) <= 3 and (stem in _PLACEHOLDER_VOCABULARY or not stem or (len(stem) == 1 and stem.isalpha()))


def is_role_value(value: str) -> bool:
    """A value made only of role words (``test-token-123``, ``wrong-token``): a test's or a document's stand-in."""
    words = [w for w in _WORD_SPLIT.split(value.lower()) if w]
    return bool(words) and all(_role_word(w) for w in words) and not all(w.isdigit() for w in words)


# live G1 (rc87): the README's ``PVEAPIToken=USER@REALM!TOKENID=UUID`` and a test's ``api_key = "UNRAID_API_KEY"`` failed
# the secret check and showed as the marker, and the coder deleted the documented token format instead (BUG-348)
# ``{name}``, ``{{ name }}``, ``${NAME}`` and an f-string/format interpolation ``{_esc(srv.password)}``: code writes
# the value there (live G1 rc136: ``f'password = "{_esc(srv.password)}"'`` showed as the marker, the coder took its own
# TOML writer for a masked secret and spent three turns on refused writes, BUG-419)
_EMBEDDED_PLACEHOLDER = re.compile(r"<[A-Za-z][\w .-]*>|\{\{?\s*[A-Za-z_][^{}\"'`]*\}\}?|\$\{[^}]+\}")
_NOTATION_SPLIT = re.compile(r"[@!=.:/+_-]+")
_NOTATION_WORD = re.compile(r"[A-Z]{2,12}")


def is_notation_value(value: str) -> bool:
    """A format or a name, not a value: ``USER@REALM!TOKENID=UUID``, ``UNRAID_API_KEY`` (capital words without
    digits, a single one at most 8 letters) or a value with a ``<placeholder>``/``{name}`` in it."""
    v = value.strip("\"'`")
    if _EMBEDDED_PLACEHOLDER.search(v):
        return True
    v = v.rstrip(".,;:)").rstrip("\"'`")  # ``…TOKENID=UUID`.`` at the end of a sentence (live G1 rc130, BUG-412)
    parts = [p for p in _NOTATION_SPLIT.split(v) if p]
    if not parts or not all(_NOTATION_WORD.fullmatch(p) for p in parts):
        return False
    return len(parts) > 1 or len(parts[0]) <= 8


# ``viewer@pam!tokenid=uuid``, ``automation!AutomationToken=00000000-0000-0000-0000-000000000000``: a Proxmox-style token
# is an id and a secret; the id is a name, only the secret part tells a stand-in from a credential (live G1 rc130, BUG-412)
_TOKEN_PAIR = re.compile(r"^[\w.-]*[@!][\w.@!-]*=([^=\s]+)$")


def is_repeated_groups(value: str) -> bool:
    """``11111111-2222-3333-4444-555555555555``, ``aaaa-bbbb``: every group one character repeated, a made-up value in
    the shape of an id (live G1 rc138: a Proxmox token example in the docs, BUG-422)."""
    groups = [g for g in re.split(r"[-_.:]", value.strip("\"'`")) if g]
    return len(groups) >= 2 and all(len(set(g)) == 1 for g in groups)


def is_placeholder_token_pair(value: str) -> bool:
    """An ``id!name=secret`` token whose secret part is a stand-in (a role word like ``uuid``, zeros, ``x``)."""
    m = _TOKEN_PAIR.match(value.strip("\"'`").rstrip(".,;:)").rstrip("\"'`"))
    if m is None:
        return False
    secret = m.group(1)
    return is_role_value(secret) or is_notation_value(secret) or is_repeated_groups(secret) or not set(secret.lower()) - set("0x*-.")


def is_label_value(value: str) -> bool:
    """``API token: `Authorization: PVEAPIToken=…` ``: the word after the key ends with a colon, it names the next key
    or header, it is no value (live G1 rc132, BUG-415)."""
    return value.rstrip("\"'`").endswith(":")


# ``token = "PVEAPIToken=" + token``, ``"Bearer %s" % key``, ``"PVEAPIToken={}".format(t)``: a short literal the code
# joins with a variable is the fixed part of the value, the secret comes from the variable (live G1 rc143: the reviewed
# prefix fix was flagged, the attempt fell back and its seven review fixes were lost, BUG-427)
_BUILDS_ON = re.compile(r"""["'`][ \t]*(?:\+[ \t]*(?!["'`])[\w(]|%[ \t]*[\w(]|\.(?:join|format)\()""")


_TEMPLATE_SLOT = re.compile(r"%[sd]|\{\w*\}")


def is_value_part(line: str, start: int, end: int) -> bool:
    """The quoted literal ``line[start:end]`` is a part the code builds a value from, not the value itself: short, ending
    in a separator (``PVEAPIToken=``) or holding a format slot (``%s``, ``{}``), joined with code. A literal joined
    with a variable that does not look like a prefix (``"abc123def456" + suffix``) stays a finding."""
    part = line[start:end]
    shaped = part[-1:] in "=:/_-. " or _TEMPLATE_SLOT.search(part) is not None
    return len(part) <= 40 and shaped and _BUILDS_ON.match(line, end) is not None


def is_benign_value(value: str, *, quoted: bool) -> bool:
    """A ``key = value`` match that is no secret: an obvious placeholder, or (unquoted) a short code reference."""
    if is_label_value(value):
        return True
    low = value.lower()
    if low.startswith(_PLACEHOLDER_START) or any(part in low for part in _PLACEHOLDER_PARTS) or not set(low) - set("x*-."):
        return True
    if is_notation_value(value) or is_placeholder_token_pair(value) or is_repeated_groups(value):
        return True
    # cycle 2 M3 on rc60: ``token="test-token-123"`` and ``token="wrong-token"`` both showed as the marker, and the review
    # could not tell the valid-token test from the wrong-token test (BUG-293). Unquoted, such a value is a variable:
    # ``{"NOTES_API_TOKEN": token}`` showed as the marker exactly on the line whose bug the coder had to find (BUG-318)
    if is_role_value(value):
        return True
    if quoted:
        return False
    if value in _CODE_KEYWORDS:
        return True
    if len(value) <= 80 and _CODE_EXPRESSION.match(value):
        return True
    return len(value) <= 60 and bool(_CODE_SIGNS & set(value)) and bool(_CODE_REFERENCE.match(value))


_SENSITIVE_KEYS = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|private[_-]?key|credential)")
# numeric telemetry under "sensitive-looking" keys (prompt_tokens, max_tokens, token_count …) is not a secret
_COUNTER_KEYS = re.compile(r"(?i)(tokens|_count|_ms|_seconds|_bytes|_chars)$|^(max|min|num|total)_")


def _mask_key_value(m: re.Match[str]) -> str:
    quoted = m.group(1).endswith(("'", '"'))
    if is_benign_value(m.group(2), quoted=quoted) or (quoted and is_value_part(m.string, m.start(2), m.end(2))):
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
