"""Query analysis: identifiers, sub-terms, phrases and route paths from free text (German/English)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

_STOP_WORDS = """
    a an the and or not no of to in on at by for from with without into onto over under is are was were be been being
    it its this that these those there here where when what which who whom how why do does did done can could should
    would will shall may might must have has had i we you they he she them our your their my me us all any some each
    every more most less least than then also just only very via per use used using make makes made get gets set sets
    new add adds added fix fixes fixed change changes changed update updates updated implement implements implemented
    please need needs want wants should file files code function functions method methods class classes test tests
    der die das den dem des ein eine einen einem einer eines und oder nicht kein keine ist sind war waren sein wird
    werden wurde wurden im in am an auf aus bei mit nach von vor zu zum zur für über unter durch ohne um wie was wo
    wann warum welche welcher welches wer bitte soll sollte muss müssen kann können neue neuen neues neu hinzufügen
    ändern ändere anpassen implementieren datei dateien funktion funktionen klasse klassen auch nur noch schon dass
    """
_STOP = frozenset(_STOP_WORDS.split())
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*(?:(?:\.|::|->|\\)[A-Za-z_$][\w$]*)*")
_ROUTE_RE = re.compile(r"(?<![\w.])/[\w\-{}:.<>\[\]]+(?:/[\w\-{}:.<>\[\]]*)*")
_PHRASE_RE = re.compile(r"`([^`]{2,200})`|\"([^\"]{2,200})\"|'([^']{3,200})'")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|[A-Z]+|\d+")


def split_identifier(name: str) -> list[str]:
    """``getUserById`` / ``get_user_by_id`` / ``App\\Models\\User`` -> lower-case sub-terms."""
    out: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", name):
        if not part:
            continue
        out.extend(m.group(0).lower() for m in _CAMEL_RE.finditer(part))
    return [t for t in out if t]


def normalize_identifier(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def stem(term: str) -> str:
    """Tiny, language-neutral plural folding (users -> user, classes -> class, entries -> entry)."""
    t = term.lower()
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith(("ses", "xes", "zes", "ches", "shes")):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


@dataclass
class QueryTerms:
    raw: str
    identifiers: list[str] = field(default_factory=list)  # code-like tokens as written (search literally)
    terms: list[str] = field(default_factory=list)  # lower-case words/sub-terms (stems), stopwords removed
    phrases: list[str] = field(default_factory=list)  # quoted/backticked literals
    routes: list[str] = field(default_factory=list)  # URL paths mentioned

    @property
    def lexical_patterns(self) -> list[str]:
        """Patterns for one ripgrep call: phrases and identifiers literally, plain words as terms."""
        seen: dict[str, None] = {}
        for p in [*self.phrases, *self.identifiers, *self.routes, *self.terms]:
            key = p.lower()
            if key not in seen and len(p) >= 2:
                seen[key] = None
        return list(seen)

    def is_empty(self) -> bool:
        return not (self.identifiers or self.terms or self.phrases or self.routes)


def _code_like(tok: str) -> bool:
    return bool(re.search(r"[a-z][A-Z]|_|\$|\.|::|->|\\", tok)) or (tok.isupper() and len(tok) > 2) or bool(re.search(r"\d", tok))


def analyze(query: str, *, max_terms: int = 12) -> QueryTerms:
    q = QueryTerms(raw=query)
    text = query or ""
    for m in _PHRASE_RE.finditer(text):
        ph = next(g for g in m.groups() if g)
        if ph.strip() and ph not in q.phrases:
            q.phrases.append(ph.strip())
    for m in _ROUTE_RE.finditer(text):
        r = m.group(0).rstrip(".,;:")
        if len(r) > 1 and r not in q.routes and "/" in r[1:] + "/":
            q.routes.append(r)
    seen_terms: set[str] = set()
    for m in _IDENT_RE.finditer(text):
        tok = m.group(0).strip(".")
        if not tok:
            continue
        low = tok.lower()
        if _code_like(tok) and len(tok) >= 3 and low not in _STOP and tok not in q.identifiers:
            q.identifiers.append(tok)
        for sub in split_identifier(tok):
            if len(sub) < 3 or sub in _STOP or sub.isdigit():
                continue
            st = stem(sub)
            if st not in seen_terms:
                seen_terms.add(st)
                q.terms.append(st)
    if q.is_empty():  # nothing but stop words: fall back to the plain tokens
        for m in _IDENT_RE.finditer(text):
            for sub in split_identifier(m.group(0)):
                if len(sub) >= 2 and not sub.isdigit() and sub not in seen_terms:
                    seen_terms.add(sub)
                    q.terms.append(sub)
    q.identifiers = q.identifiers[:max_terms]
    q.terms = q.terms[:max_terms]
    q.phrases = q.phrases[:4]
    q.routes = q.routes[:4]
    return q


def idf(df: int, n_docs: int) -> float:
    return math.log(1.0 + (max(n_docs, 1) - df + 0.5) / (df + 0.5))
