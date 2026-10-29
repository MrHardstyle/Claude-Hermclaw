"""Reference implementation of ladder stage R3 (rechner), only to validate the hidden check script."""

import re
import sys
from pathlib import Path

MIN, MAX = -(2**63), 2**63 - 1
NAME = re.compile(r"[a-z_][a-z0-9_]*")
KEYWORDS = {"let", "print"}


class SyntaxErr(Exception):
    def __init__(self, col, msg):
        super().__init__(msg)
        self.col = col


class RunErr(Exception):
    pass


def tokens(line):
    out, i = [], 0
    while i < len(line):
        c = line[i]
        if c in " \t":
            i += 1
        elif c.isascii() and c.isdigit():
            j = i
            while j < len(line) and line[j].isascii() and line[j].isdigit():
                j += 1
            out.append(("num", line[i:j], i + 1))
            i = j
        elif NAME.match(c):
            m = NAME.match(line, i)
            out.append(("name", m.group(0), i + 1))
            i = m.end()
        elif c in "+-*/%^()=":
            out.append((c, c, i + 1))
            i += 1
        else:
            raise SyntaxErr(i + 1, f"unerwartetes Zeichen '{c}'")
    out.append(("end", "", len(line) + 1))
    return out


class Parser:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self):
        return self.t[self.i]

    def take(self, kind):
        tok = self.peek()
        if tok[0] != kind:
            raise SyntaxErr(tok[2], f"'{kind}' erwartet")
        self.i += 1
        return tok

    def ausdruck(self):
        node = self.term()
        while self.peek()[0] in ("+", "-"):
            op = self.take(self.peek()[0])[0]
            node = (op, node, self.term())
        return node

    def term(self):
        node = self.faktor()
        while self.peek()[0] in ("*", "/", "%"):
            op = self.take(self.peek()[0])[0]
            node = (op, node, self.faktor())
        return node

    def faktor(self):
        if self.peek()[0] == "-":
            self.take("-")
            return ("neg", self.faktor())
        return self.potenz()

    def potenz(self):
        base = self.basis()
        if self.peek()[0] == "^":
            self.take("^")
            return ("^", base, self.faktor())
        return base

    def basis(self):
        tok = self.peek()
        if tok[0] == "num":
            self.i += 1
            if int(tok[1]) > MAX:
                raise SyntaxErr(tok[2], "Zahl zu groß")
            return ("num", int(tok[1]))
        if tok[0] == "name" and tok[1] not in KEYWORDS:
            self.i += 1
            return ("var", tok[1])
        if tok[0] == "(":
            self.take("(")
            node = self.ausdruck()
            self.take(")")
            return node
        raise SyntaxErr(tok[2], "Ausdruck erwartet")


def parse_line(line):
    if not line.strip(" \t") or line.lstrip(" \t").startswith("#"):
        return None
    p = Parser(tokens(line))
    head = p.peek()
    if head[:2] == ("name", "let"):
        p.take("name")
        name = p.peek()
        if name[0] != "name" or name[1] in KEYWORDS:
            raise SyntaxErr(name[2], "Variablenname erwartet")
        p.take("name")
        p.take("=")
        stmt = ("let", name[1], p.ausdruck())
    elif head[:2] == ("name", "print"):
        p.take("name")
        stmt = ("print", p.ausdruck())
    else:
        raise SyntaxErr(head[2], "'let' oder 'print' erwartet")
    p.take("end")
    return stmt


def checked(v):
    if not MIN <= v <= MAX:
        raise RunErr("Überlauf")
    return v


def ev(node, env):
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "var":
        if node[1] not in env:
            raise RunErr(f"unbekannte Variable '{node[1]}'")
        return env[node[1]]
    if kind == "neg":
        return checked(-ev(node[1], env))
    a, b = ev(node[1], env), ev(node[2], env)
    if kind == "+":
        return checked(a + b)
    if kind == "-":
        return checked(a - b)
    if kind == "*":
        return checked(a * b)
    if kind in "/%":
        if b == 0:
            raise RunErr("Division durch null")
        q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
        return checked(q) if kind == "/" else a - b * q
    if b < 0:
        raise RunErr("negativer Exponent")
    if b == 0:
        return 1
    if a in (0, 1):
        return a
    if a == -1:
        return -1 if b % 2 else 1
    r = 1
    for _ in range(b):  # |a| >= 2: overflows after at most 63 factors
        r = checked(r * a)
    return r


def main(argv):
    if len(argv) != 1:
        print("Aufruf: rechner DATEI", file=sys.stderr)
        return 4
    try:
        text = Path(argv[0]).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"{argv[0]}: {exc}", file=sys.stderr)
        return 1
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    program = []
    for n, raw in enumerate(lines, 1):
        try:
            stmt = parse_line(raw.removesuffix("\r"))
        except SyntaxErr as exc:
            print(f"Zeile {n}, Spalte {exc.col}: {exc}", file=sys.stderr)
            return 2
        if stmt is not None:
            program.append((n, stmt))
    env = {}
    for n, stmt in program:
        try:
            if stmt[0] == "let":
                env[stmt[1]] = ev(stmt[2], env)
            else:
                print(ev(stmt[1], env), flush=True)
        except RunErr as exc:
            print(f"Zeile {n}: {exc}", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
