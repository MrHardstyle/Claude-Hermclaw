"""Source records (P12 12.4), freshness (12.8) and authority/relevance scoring (12.9).

Authority is derived generically from the URL: configured primary domains (``policies.research.primary_domains``,
optionally with a path prefix such as ``github.com/org/repo``), standards bodies, documentation host/path patterns
(``docs.*``, ``*.readthedocs.io``, ``/docs/``), source repositories (``github.com/<org>/<repo>``), package
registries, Q&A/forum sites and general secondary sites. Relevance is a BM25-style saturated term-frequency score
of the question's key terms against title and text. No project-specific rules live here.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from hermclaw.contracts.research import SourceType
from hermclaw.research.text import key_terms, term_weight, tokenize

AUTHORITY: dict[str, float] = {
    "official_docs": 0.9,
    "standard": 0.9,
    "official_repo": 0.75,
    "vendor": 0.65,
    "secondary": 0.45,
    "forum": 0.3,
    "unknown": 0.25,
}
STANDARD_HOSTS: tuple[str, ...] = (
    "ietf.org",
    "rfc-editor.org",
    "w3.org",
    "whatwg.org",
    "iso.org",
    "ecma-international.org",
    "unicode.org",
    "peps.python.org",
    "json-schema.org",
    "spec.openapis.org",
    "semver.org",
    "iana.org",
    "nist.gov",
    "opengroup.org",
)
REPO_HOSTS: tuple[str, ...] = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org", "sr.ht", "git.sr.ht")
REGISTRY_HOSTS: tuple[str, ...] = (
    "pypi.org",
    "npmjs.com",
    "crates.io",
    "docs.rs",
    "pkg.go.dev",
    "rubygems.org",
    "packagist.org",
    "hub.docker.com",
    "artifacthub.io",
    "hex.pm",
    "nuget.org",
    "central.sonatype.com",
)
DOC_HOSTS: tuple[str, ...] = ("developer.mozilla.org", "learn.microsoft.com", "man7.org", "manpages.debian.org", "devdocs.io")
FORUM_HOSTS: tuple[str, ...] = (
    "stackoverflow.com",
    "stackexchange.com",
    "superuser.com",
    "serverfault.com",
    "askubuntu.com",
    "reddit.com",
    "news.ycombinator.com",
    "quora.com",
    "lobste.rs",
    "discord.com",
)
FORUM_PREFIXES: tuple[str, ...] = ("forum.", "forums.", "community.", "discuss.", "discourse.", "answers.", "talk.", "groups.")
SECONDARY_HOSTS: tuple[str, ...] = (
    "wikipedia.org",
    "medium.com",
    "dev.to",
    "hashnode.dev",
    "substack.com",
    "blogspot.com",
    "wordpress.com",
    "geeksforgeeks.org",
    "w3schools.com",
    "tutorialspoint.com",
    "towardsdatascience.com",
    "freecodecamp.org",
    "baeldung.com",
)
_REPO_DISCUSSION_PATHS: frozenset[str] = frozenset({"issues", "discussions", "pull", "pulls", "merge_requests", "-"})
_SECOND_LEVEL: frozenset[str] = frozenset({"co", "com", "org", "net", "ac", "gov", "edu", "or", "ne", "go"})

FreshnessStatus = Literal["fresh", "aging", "stale", "unknown"]


@dataclass(frozen=True)
class SourceClassification:
    source_type: SourceType
    authority_score: float
    reason: str


@dataclass(frozen=True)
class Freshness:
    status: FreshnessStatus
    age_days: int | None
    factor: float  # confidence multiplier


def host_of(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def registrable_domain(host: str) -> str:
    """Approximate eTLD+1 (``a.b.example.co.uk`` → ``example.co.uk``) without a public-suffix download."""
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _primary_match(host: str, path: str, primary: Sequence[str]) -> str | None:
    for raw in primary:
        entry = raw.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
        if not entry:
            continue
        dom, _, prefix = entry.partition("/")
        if not _matches(host, dom):
            continue
        if prefix and not (path.lower().rstrip("/") + "/").startswith("/" + prefix + "/"):
            continue
        return entry
    return None


def classify_source(url: str, primary_domains: Sequence[str] = ()) -> SourceClassification:
    host = host_of(url)
    if not host:
        return SourceClassification("unknown", AUTHORITY["unknown"], "unparseable url")
    try:
        path = urlsplit(url).path or "/"
    except ValueError:
        path = "/"
    segments = [s for s in path.split("/") if s]
    bare = host.removeprefix("www.")
    is_repo_host = any(_matches(bare, h) for h in REPO_HOSTS)
    primary = _primary_match(bare, path, primary_domains)
    if primary:
        if is_repo_host or any(_matches(bare, h) for h in REGISTRY_HOSTS):
            return SourceClassification("official_repo", 0.9, f"primary source {primary}")
        if any(_matches(bare, h) for h in STANDARD_HOSTS):
            return SourceClassification("standard", 0.95, f"primary source {primary}")
        return SourceClassification("official_docs", 0.95, f"primary source {primary}")
    if any(_matches(bare, h) for h in STANDARD_HOSTS):
        return SourceClassification("standard", AUTHORITY["standard"], "standards body")
    if any(_matches(bare, h) for h in FORUM_HOSTS) or bare.startswith(FORUM_PREFIXES):
        return SourceClassification("forum", AUTHORITY["forum"], "forum / Q&A site")
    if is_repo_host:
        if bare.startswith("gist."):
            return SourceClassification("secondary", 0.4, "code snippet (gist)")
        if len(segments) >= 2:
            if len(segments) >= 3 and segments[2].lower() in _REPO_DISCUSSION_PATHS:
                if len(segments) >= 4 and segments[2] == "-" and segments[3].lower() not in _REPO_DISCUSSION_PATHS:
                    return SourceClassification("official_repo", AUTHORITY["official_repo"], "source repository")
                return SourceClassification("forum", 0.4, "repository issue/discussion thread")
            return SourceClassification("official_repo", AUTHORITY["official_repo"], "source repository")
        return SourceClassification("secondary", 0.4, "repository host profile page")
    if any(_matches(bare, h) for h in REGISTRY_HOSTS):
        return SourceClassification("official_repo", 0.7, "package registry")
    if (
        bare.startswith(("docs.", "doc.", "developer.", "developers.", "dev-docs.", "api-docs.", "manual."))
        or ".docs." in bare
        or bare.endswith((".readthedocs.io", ".readthedocs.org"))
        or any(_matches(bare, h) for h in DOC_HOSTS)
    ):
        return SourceClassification("official_docs", 0.85, "documentation host")
    if any(_matches(bare, h) for h in SECONDARY_HOSTS):
        return SourceClassification("secondary", 0.5 if "wikipedia" in bare else AUTHORITY["secondary"], "secondary publication")
    if segments and segments[0].lower() in {"docs", "documentation", "manual", "reference", "api"}:
        return SourceClassification("official_docs", 0.75, "documentation path")
    reg = registrable_domain(bare)
    for entry in primary_domains:
        dom = entry.strip().lower().removeprefix("https://").removeprefix("http://").split("/", 1)[0]
        if dom and registrable_domain(dom) == reg:
            return SourceClassification("vendor", 0.7, f"vendor site of {dom}")
    if bare.startswith(("blog.", "blogs.")) or (segments and segments[0].lower() in {"blog", "blogs", "news"}):
        return SourceClassification("secondary", AUTHORITY["secondary"], "blog")
    return SourceClassification("secondary", 0.4, "general website")


# ----------------------------------------------------------------------------------------------- relevance
def relevance_score(question_terms: Sequence[str], *, title: str, text: str, snippet: str = "") -> float:
    """0..1: weighted term coverage (55 %), saturated term frequency (25 %), title hits (20 %)."""
    terms = [t for t in dict.fromkeys(question_terms) if t]
    if not terms:
        return 0.0
    doc_tokens = tokenize(f"{text}\n{snippet}")
    counts = Counter(doc_tokens)
    title_tokens = set(tokenize(title))
    doc_len = max(len(doc_tokens), 1)
    k1, b, avg_len = 1.2, 0.75, 800.0
    norm = k1 * (1 - b + b * doc_len / avg_len)
    total_w = sum(term_weight(t) for t in terms)
    covered_w = 0.0
    saturated = 0.0
    title_w = 0.0
    for term in terms:
        w = term_weight(term)
        tf = counts.get(term, 0)
        if tf == 0 and "-" in term:  # hyphenated query term vs. split document tokens
            parts = term.split("-")
            tf = min(counts.get(p, 0) for p in parts) if all(parts) else 0
        if tf:
            covered_w += w
            saturated += w * (tf * (k1 + 1)) / (tf + norm) / (k1 + 1)
        if term in title_tokens:
            title_w += w
    score = 0.55 * covered_w / total_w + 0.25 * saturated / total_w + 0.2 * title_w / total_w
    return round(min(max(score, 0.0), 1.0), 4)


def question_terms(question: str, queries: Sequence[str] = ()) -> list[str]:
    """Key terms of the question (primary) plus terms the planned queries add."""
    terms = key_terms(question)
    for q in queries:
        for t in key_terms(q):
            if t not in terms and len(terms) < 24:
                terms.append(t)
    return terms


def prior_score(url: str, rank: int, *, query_hits: int, primary_domains: Sequence[str]) -> float:
    """Ranking of search candidates before fetching (primary sources first, then search rank)."""
    cls = classify_source(url, primary_domains)
    rank_score = 1.0 / (1.0 + max(rank, 0) * 0.35)
    return 0.6 * cls.authority_score + 0.3 * rank_score + 0.1 * min(max(query_hits - 1, 0), 3) / 3


# ----------------------------------------------------------------------------------------------- freshness
def assess_freshness(date: datetime | None, *, now: datetime, fresh_days: int = 365, stale_days: int = 1095) -> Freshness:
    if date is None:
        return Freshness("unknown", None, 0.95)
    age = max((now - date).days, 0)
    if age <= fresh_days:
        return Freshness("fresh", age, 1.0)
    if age <= stale_days:
        return Freshness("aging", age, 0.93)
    # an old page loses at most 25 % confidence, more the older it is
    years_over = (age - stale_days) / 365.0
    return Freshness("stale", age, round(max(0.75, 0.85 - 0.03 * math.floor(years_over)), 3))
