"""A small nginx configuration tokenizer and parser (structure only, no semantics) – used by
``proxy_config_validate`` and the port collision check (P28.4/P28.5).

It follows the rules of ``ngx_conf_read_token``: words are separated by whitespace, ``;`` ends a directive, ``{``/``}``
open/close a block, ``#`` starts a comment at the beginning of a word, single/double quoted strings with backslash
escapes are one word, ``${name}`` inside a word is a variable (its braces are not block delimiters).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from hermclaw.infratools.errors import INFRA_PROXY_INVALID, InfraToolError

MAX_CONFIG_CHARS = 2_000_000
MAX_DEPTH = 50


class NginxParseError(InfraToolError):
    code = INFRA_PROXY_INVALID

    def __init__(self, message: str, line: int) -> None:
        super().__init__(f"{message} (line {line})", code=INFRA_PROXY_INVALID, details={"line": line})
        self.line = line


@dataclass(frozen=True)
class Token:
    value: str
    line: int
    quoted: bool = False
    start: int = 0  # offset of the token in the source text (including quotes)
    end: int = 0  # exclusive end offset

    @property
    def special(self) -> bool:
        return not self.quoted and self.value in ("{", "}", ";")


def tokenize(text: str) -> list[Token]:
    if len(text) > MAX_CONFIG_CHARS:
        raise NginxParseError(f"configuration larger than {MAX_CONFIG_CHARS} characters", 1)
    tokens: list[Token] = []
    i, n, line = 0, len(text), 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch in " \t\r\f\v":
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch in "{};":
            tokens.append(Token(ch, line, start=i, end=i + 1))
            i += 1
            continue
        if ch in "\"'":
            quote, start_line, start = ch, line, i
            i += 1
            buf: list[str] = []
            while True:
                if i >= n:
                    raise NginxParseError("unterminated quoted string", start_line)
                c = text[i]
                if c == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    # nginx keeps the backslash except for an escaped quote/backslash and \n \r \t
                    mapping = {"n": "\n", "r": "\r", "t": "\t", quote: quote, "\\": "\\"}
                    buf.append(mapping.get(nxt, "\\" + nxt))
                    if nxt == "\n":
                        line += 1
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                if c == "\n":
                    line += 1
                buf.append(c)
                i += 1
            if i < n and text[i] not in " \t\r\n;{}":
                raise NginxParseError(f"unexpected '{text[i]}' after quoted string", line)
            tokens.append(Token("".join(buf), start_line, quoted=True, start=start, end=i))
            continue
        # plain word
        start_line, start = line, i
        buf = []
        in_var = False
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n:
                buf.append(text[i : i + 2])
                i += 2
                continue
            if c == "{" and buf and buf[-1].endswith("$"):
                in_var = True
                buf.append(c)
                i += 1
                continue
            if c == "}" and in_var:
                in_var = False
                buf.append(c)
                i += 1
                continue
            if c in " \t\r\n\f\v;{}" or (c in "\"'" and not buf):
                break
            buf.append(c)
            i += 1
        if in_var:
            raise NginxParseError("unterminated ${variable}", start_line)
        tokens.append(Token("".join(buf), start_line, start=start, end=i))
    return tokens


@dataclass
class Directive:
    name: str
    args: list[str]
    line: int
    block: list[Directive] | None = None
    parent: Directive | None = field(default=None, repr=False, compare=False)
    tokens: list[Token] = field(default_factory=list, repr=False, compare=False)  # name + argument tokens

    @property
    def is_block(self) -> bool:
        return self.block is not None

    def children(self, name: str | None = None) -> list[Directive]:
        return [d for d in (self.block or []) if name is None or d.name == name]

    def first(self, name: str) -> Directive | None:
        for d in self.block or []:
            if d.name == name:
                return d
        return None

    def walk(self) -> Iterator[Directive]:
        for d in self.block or []:
            yield d
            yield from d.walk()

    def ancestors(self) -> Iterator[Directive]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def describe(self) -> str:
        return f"{self.name} {' '.join(self.args)}".strip()


@dataclass
class NginxConfig:
    root: Directive  # synthetic "main" block holding the top-level directives

    @property
    def directives(self) -> list[Directive]:
        return self.root.block or []

    def walk(self) -> Iterator[Directive]:
        return self.root.walk()

    def find(self, name: str) -> list[Directive]:
        return [d for d in self.walk() if d.name == name]

    @property
    def is_main(self) -> bool:
        """A full ``nginx.conf`` (has top-level ``events``/``http``) – otherwise a site snippet for ``http {}``."""
        return any(d.name in ("events", "http") for d in self.directives)

    def servers(self) -> list[Directive]:
        """``server`` blocks of the http context (not ``stream``/``mail``; nested ``upstream`` servers excluded)."""
        out: list[Directive] = []
        for d in self.walk():
            if d.name != "server" or not d.is_block:
                continue
            parent = d.parent
            if parent is None or parent.name in ("http", "main"):
                out.append(d)
        return out

    def upstreams(self) -> dict[str, Directive]:
        return {d.args[0]: d for d in self.walk() if d.name == "upstream" and d.is_block and d.args}


def parse(text: str) -> NginxConfig:
    tokens = tokenize(text)
    root = Directive("main", [], 0, block=[])
    stack: list[Directive] = [root]
    words: list[Token] = []
    for tok in tokens:
        if not tok.special:
            words.append(tok)
            continue
        if tok.value == ";":
            if not words:
                raise NginxParseError("unexpected ';'", tok.line)
            d = Directive(words[0].value, [w.value for w in words[1:]], words[0].line, parent=stack[-1], tokens=list(words))
            assert stack[-1].block is not None
            stack[-1].block.append(d)
            words = []
        elif tok.value == "{":
            if not words:
                raise NginxParseError("unexpected '{'", tok.line)
            if len(stack) > MAX_DEPTH:
                raise NginxParseError("blocks nested too deeply", tok.line)
            d = Directive(words[0].value, [w.value for w in words[1:]], words[0].line, block=[], parent=stack[-1], tokens=list(words))
            assert stack[-1].block is not None
            stack[-1].block.append(d)
            stack.append(d)
            words = []
        else:  # "}"
            if words:
                raise NginxParseError(f"directive '{words[0].value}' is not terminated by ';'", words[0].line)
            if len(stack) == 1:
                raise NginxParseError("unexpected '}'", tok.line)
            stack.pop()
    if words:
        raise NginxParseError(f"unexpected end of file, directive '{words[0].value}' is not terminated by ';'", words[0].line)
    if len(stack) > 1:
        raise NginxParseError(f"unexpected end of file, block '{stack[-1].name}' is not closed", stack[-1].line)
    return NginxConfig(root)


# ============================================================================================ listen
@dataclass(frozen=True)
class Listen:
    address: str  # "*" | IPv4 | IPv6 (without brackets) | hostname | "unix:/path"
    port: int | None  # None for unix sockets
    transport: str  # tcp | udp (quic)
    ssl: bool = False
    default_server: bool = False
    http2: bool = False
    line: int = 0
    raw: str = ""

    @property
    def is_unix(self) -> bool:
        return self.address.startswith("unix:")

    def key(self) -> str:
        if self.is_unix:
            return self.address
        addr = f"[{self.address}]" if ":" in self.address else self.address
        return f"{addr}:{self.port}/{self.transport}"


_LISTEN_FLAGS_SSL = {"ssl"}


def parse_listen(directive: Directive) -> Listen:
    """``listen 80;`` ``listen 127.0.0.1:8080;`` ``listen [::]:443 ssl http2;`` ``listen 443 quic reuseport;`` ``listen unix:/x;``"""
    if not directive.args:
        raise NginxParseError("listen without address", directive.line)
    spec = directive.args[0]
    flags = [a.split("=", 1)[0] for a in directive.args[1:]]
    transport = "udp" if "quic" in flags else "tcp"
    common = {
        "ssl": any(f in _LISTEN_FLAGS_SSL for f in flags),
        "default_server": "default_server" in flags or "default" in flags,
        "http2": "http2" in flags,
        "line": directive.line,
        "raw": directive.describe(),
    }
    if spec.startswith("unix:"):
        return Listen(spec, None, "tcp", **common)  # type: ignore[arg-type]
    address = "*"
    port_s = spec
    if spec.startswith("["):
        end = spec.find("]")
        if end < 0:
            raise NginxParseError(f"invalid IPv6 listen address '{spec}'", directive.line)
        address = spec[1:end]
        rest = spec[end + 1 :]
        port_s = rest[1:] if rest.startswith(":") else "80"
    elif ":" in spec:
        address, port_s = spec.rsplit(":", 1)
    elif not spec.isdigit():
        address, port_s = spec, "80"
    if not port_s.isdigit() or not 0 < int(port_s) <= 65535:
        raise NginxParseError(f"invalid port in listen '{spec}'", directive.line)
    return Listen(address or "*", int(port_s), transport, **common)  # type: ignore[arg-type]


def server_listens(server: Directive) -> list[Listen]:
    listens = [parse_listen(d) for d in server.children("listen")]
    return listens or [Listen("*", 80, "tcp", line=server.line, raw="listen 80 (default)")]


def server_names(server: Directive) -> list[str]:
    names: list[str] = []
    for d in server.children("server_name"):
        names.extend(d.args)
    return names or [""]


# ============================================================================================ helpers
_TIME_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}
_TIME_RE = re.compile(r"(\d+)(ms|s|m|h|d|w|M|y)?")


def parse_time(value: str) -> float | None:
    """nginx time value in seconds (``60``, ``60s``, ``1m30s``, ``500ms``, ``1h``); ``None`` if invalid."""
    if not value:
        return None
    pos = 0
    total = 0.0
    while pos < len(value):
        m = _TIME_RE.match(value, pos)
        if not m or m.end() == pos:
            return None
        total += int(m.group(1)) * _TIME_UNITS[m.group(2) or "s"]
        pos = m.end()
    return total


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    return True


__all__ = [
    "Directive",
    "Listen",
    "NginxConfig",
    "NginxParseError",
    "Token",
    "is_ip",
    "parse",
    "parse_listen",
    "parse_time",
    "server_listens",
    "server_names",
    "tokenize",
]
