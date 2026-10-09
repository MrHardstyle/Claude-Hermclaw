"""Language-light text utilities shared by the research pipeline (tokens, key terms, sentences, values).

Everything here is deterministic and dependency-free so that fallback paths (query planning, claim extraction,
relevance scoring, contradiction detection) work without any model.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# English + German function words; they carry no subject information for overlap measures.
_STOPWORDS_TEXT = """
a aber about above after again against all alle allem allen aller alles als also am an and ander andere
anderem anderen anderer anderes any are aren't as at auch auf aus be because been before bei being below
between bin bis bist both but by can can't cannot could couldn't da damit dann das dass dein deine dem den der
des dessen dich did didn't die dies diese diesem diesen dieser dieses dir do doch does doesn't doing don't
dort down du durch during e.g each ein eine einem einen einer eines er es etc euer eure few for from further
für gegen get gets gewesen got hab habe haben had hadn't has hasn't hat hatte have haven't having he her here
hers herself him himself his how however hätte i i.e ich if ihr ihre im in into is isn't ist it it's its
itself ja jede jedem jeden jeder jedes jener just kann kein keine keinem keinen keiner let's man manche may me
mein meine mich might mir mit more most muss must mustn't my myself nach nicht nichts no noch nor not now nun
nur ob oder of off ohne on once only or other ought our ours ourselves out over own same sehr sein seine shall
she should shouldn't sich sie sind so solche soll sollte some sondern sonst such than that that's the their
theirs them themselves then there there's these they this those through to too um und under uns unser unter
until up upon us use used using very via vom von vor vs war waren warum was wasn't we weil welche welchem
welchen welcher welches wenn wer werde werden were weren't what what's when where which while who whom whose
why wie wieder will wir wird with within without wo won't would wouldn't wurde wurden you your yours yourself
yourselves zu zum zur über
"""
STOPWORDS: frozenset[str] = frozenset(_STOPWORDS_TEXT.split())

#: words that flip the polarity of a statement (used by contradiction detection)
_NEGATIONS_TEXT = """
aren't can't cannot couldn't didn't doesn't don't hadn't hasn't haven't isn't kein keine keinem keinen keiner
mustn't neither never nicht nie niemals no none nor not ohne shouldn't wasn't weren't without won't wouldn't
"""
NEGATIONS: frozenset[str] = frozenset(_NEGATIONS_TEXT.split())

#: antonym pairs: a claim using one side and a claim using the other side on the same subject disagree
ANTONYMS: tuple[tuple[str, str], ...] = (
    ("supported", "unsupported"),
    ("supports", "unsupported"),
    ("enabled", "disabled"),
    ("enable", "disable"),
    ("required", "optional"),
    ("mandatory", "optional"),
    ("added", "removed"),
    ("allowed", "forbidden"),
    ("allowed", "disallowed"),
    ("compatible", "incompatible"),
    ("deprecated", "recommended"),
    ("true", "false"),
    ("unterstützt", "nicht-unterstützt"),
    ("erforderlich", "optional"),
    ("aktiviert", "deaktiviert"),
)

_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ_][0-9A-Za-zÀ-ÖØ-öø-ÿ_.+#'’/-]*")
_VERSION_RE = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+)+)(?![\w]|\.\d)")
_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)(?![\w.]?\d)")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+(?=[\"'“„(\[]?[A-ZÄÖÜ0-9])")
_CITATION_ONLY_RE = re.compile(r"^(?:\[\d+(?:\s*[,;]\s*\d+)*\]\s*)+[.!?]?$")
_LEADING_CITATIONS_RE = re.compile(r"^((?:\[\d+(?:\s*[,;]\s*\d+)*\]\s*)+)(\S.*)$")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_ABBREVIATIONS: frozenset[str] = frozenset(
    [
        "e.g.",
        "i.e.",
        "z.b.",
        "bzw.",
        "vs.",
        "ca.",
        "u.a.",
        "d.h.",
        "approx.",
        "no.",
        "nr.",
        "fig.",
        "dr.",
        "mr.",
        "ms.",
        "inc.",
        "ltd.",
        "sec.",
        "vgl.",
        "resp.",
    ]
)
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def normalize_ws(text: str) -> str:
    """Collapse runs of whitespace (incl. newlines) into single spaces."""
    return " ".join(text.split())


def tokenize(text: str) -> list[str]:
    """Lower-cased word tokens; keeps dotted/hyphenated identifiers (``3.12``, ``docs.python.org``, ``x-y``)."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        tok = raw.strip(".-/'’")
        tok = tok.replace("’", "'")
        if tok:
            out.append(tok)
    return out


def is_numeric_token(token: str) -> bool:
    return bool(re.fullmatch(r"v?\d+(?:[.,]\d+)*", token))


def key_terms(text: str, *, limit: int | None = None) -> list[str]:
    """Distinct informative terms in order of first occurrence (stopwords dropped)."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokenize(text):
        if tok in STOPWORDS or tok in seen:
            continue
        if len(tok) < 2 and not tok.isdigit():
            continue
        seen.add(tok)
        out.append(tok)
        if limit is not None and len(out) >= limit:
            break
    return out


def subject_terms(text: str) -> frozenset[str]:
    """Content terms without numbers/versions and negations – the 'what is this about' part of a statement."""
    return frozenset(t for t in key_terms(text) if not is_numeric_token(t) and t not in NEGATIONS and len(t) >= 3)


def term_weight(term: str) -> float:
    """Longer and number-bearing terms are more specific, hence weigh more for relevance."""
    weight = 1.0 + min(len(term), 12) / 12.0
    if any(ch.isdigit() for ch in term):
        weight *= 1.5
    return weight


def extract_values(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """(versions, plain numbers) mentioned in ``text``; ``v3.12`` → ``3.12``; numbers exclude version parts."""
    text = _THOUSANDS_RE.sub("", text)
    versions = frozenset(m.group(1) for m in _VERSION_RE.finditer(text))
    stripped = _VERSION_RE.sub(" ", text)
    numbers = frozenset(m.group(1).replace(",", ".") for m in _NUMBER_RE.finditer(stripped))
    return versions, numbers


def is_negated(text: str) -> bool:
    toks = set(tokenize(text))
    return bool(toks & NEGATIONS) or "n't" in text.lower()


def split_sentences(text: str) -> list[str]:
    """Sentence split on terminal punctuation followed by an upper-case/digit start; paragraphs never merge.

    A trailing citation group after the full stop (``… is X. [3]``) stays attached to its sentence.
    """
    sentences: list[str] = []
    for raw_para in re.split(r"\n\s*\n|\n(?=\s*[-*•]\s)", text):
        para = normalize_ws(raw_para)
        if not para:
            continue
        start = len(sentences)
        for raw_piece in _SENTENCE_SPLIT_RE.split(para):
            piece = raw_piece.strip()
            if not piece:
                continue
            in_para = len(sentences) > start
            if in_para and _CITATION_ONLY_RE.match(piece):
                sentences[-1] = f"{sentences[-1]} {piece}"
                continue
            lead = _LEADING_CITATIONS_RE.match(piece) if in_para else None
            if lead:
                sentences[-1] = f"{sentences[-1]} {lead.group(1).strip()}"
                piece = lead.group(2).strip()
            if in_para and sentences[-1].rsplit(" ", 1)[-1].lower() in _ABBREVIATIONS:
                sentences[-1] = f"{sentences[-1]} {piece}"
                continue
            sentences.append(piece)
    return sentences


def clip(text: str, limit: int) -> str:
    text = normalize_ws(text)
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def overlap(a: Iterable[str], b: Iterable[str]) -> tuple[int, float, float]:
    """(shared count, overlap coefficient, Jaccard) of two term collections."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0, 0.0, 0.0
    shared = len(sa & sb)
    return shared, shared / min(len(sa), len(sb)), shared / len(sa | sb)


def clean_line(line: str) -> str:
    return _WS_RE.sub(" ", line).strip()
