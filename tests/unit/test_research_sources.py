"""P12 12.4/12.8/12.9: source classification (authority), relevance scoring, freshness and candidate priority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hermclaw.research.sources import (
    assess_freshness,
    classify_source,
    prior_score,
    question_terms,
    registrable_domain,
    relevance_score,
)

PRIMARY = ["toolkit.dev", "github.com/acme/toolkit", "https://spec.acme.org/"]


@pytest.mark.parametrize(
    ("url", "source_type", "min_score", "max_score"),
    [
        ("https://toolkit.dev/guide/install", "official_docs", 0.95, 0.95),  # primary domain
        ("https://api.toolkit.dev/ref", "official_docs", 0.95, 0.95),  # subdomain of primary
        ("https://github.com/acme/toolkit/blob/main/README.md", "official_repo", 0.9, 0.9),  # primary with path prefix
        ("https://github.com/acme/toolkit-fork", "official_repo", 0.75, 0.75),  # NOT the primary path prefix
        ("https://github.com/acme/toolkit/issues/12", "official_repo", 0.9, 0.9),  # primary repo wins
        ("https://github.com/other/lib/issues/3", "forum", 0.4, 0.4),
        ("https://gitlab.com/grp/proj/-/issues/3", "forum", 0.4, 0.4),
        ("https://gitlab.com/grp/proj/-/blob/main/x.py", "official_repo", 0.75, 0.75),
        ("https://github.com/someuser", "secondary", 0.4, 0.4),
        ("https://gist.github.com/u/abc", "secondary", 0.4, 0.4),
        ("https://www.rfc-editor.org/rfc/rfc9110", "standard", 0.9, 0.9),
        ("https://datatracker.ietf.org/doc/html/rfc9110", "standard", 0.9, 0.9),
        ("https://docs.python.org/3/library/asyncio.html", "official_docs", 0.85, 0.85),
        ("https://httpx.readthedocs.io/en/latest/", "official_docs", 0.85, 0.85),
        ("https://developer.mozilla.org/en-US/docs/Web/HTTP", "official_docs", 0.85, 0.85),
        ("https://example.com/docs/setup", "official_docs", 0.75, 0.75),
        ("https://pypi.org/project/httpx/", "official_repo", 0.7, 0.7),
        ("https://stackoverflow.com/questions/1/x", "forum", 0.3, 0.3),
        ("https://unix.stackexchange.com/q/1", "forum", 0.3, 0.3),
        ("https://forum.toolkit-users.org/t/1", "forum", 0.3, 0.3),
        ("https://www.reddit.com/r/python/comments/1", "forum", 0.3, 0.3),
        ("https://en.wikipedia.org/wiki/HTTP", "secondary", 0.5, 0.5),
        ("https://medium.com/@x/post", "secondary", 0.45, 0.45),
        ("https://acme.org/blog/release", "vendor", 0.7, 0.7),  # same registrable domain as primary spec.acme.org
        ("https://blog.random.net/x", "secondary", 0.45, 0.45),
        ("https://random.net/page", "secondary", 0.4, 0.4),
        ("not a url", "unknown", 0.25, 0.25),
    ],
)
def test_classify_source(url: str, source_type: str, min_score: float, max_score: float) -> None:
    cls = classify_source(url, PRIMARY)
    assert cls.source_type == source_type, cls
    assert min_score <= cls.authority_score <= max_score
    assert cls.reason


def test_primary_outranks_everything_and_forum_is_lowest() -> None:
    scores = {u: classify_source(u, PRIMARY).authority_score for u in ["https://toolkit.dev/x", "https://docs.other.io/x", "https://stackoverflow.com/q/1"]}
    assert scores["https://toolkit.dev/x"] > scores["https://docs.other.io/x"] > scores["https://stackoverflow.com/q/1"]


def test_registrable_domain() -> None:
    assert registrable_domain("a.b.example.co.uk") == "example.co.uk"
    assert registrable_domain("docs.example.com") == "example.com"
    assert registrable_domain("localhost") == "localhost"


def test_relevance_prefers_on_topic_documents() -> None:
    terms = question_terms("Which Python version does Toolkit 4 require?", ["toolkit python requirement"])
    assert terms[:5] == ["python", "version", "toolkit", "4", "require"]
    assert "requirement" in terms
    on_topic = relevance_score(terms, title="Toolkit installation", text="Toolkit 4 requires Python 3.10. The Python version matters. " * 3)
    partial = relevance_score(terms, title="Python news", text="Python 3.13 was released with a new version of asyncio.")
    off_topic = relevance_score(terms, title="Cooking", text="How to bake bread with yeast and flour.")
    assert 0 <= off_topic < partial < on_topic <= 1
    assert off_topic == 0.0
    assert relevance_score([], title="x", text="y") == 0.0


def test_relevance_saturates_term_frequency() -> None:
    terms = ["toolkit", "python"]
    once = relevance_score(terms, title="", text="toolkit python " + "filler " * 50)
    many = relevance_score(terms, title="", text="toolkit python " * 50)
    assert many > once
    assert many - once < 0.25  # saturated: term spam cannot dominate coverage


def test_freshness_categories() -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    assert assess_freshness(None, now=now).status == "unknown"
    fresh = assess_freshness(now - timedelta(days=30), now=now)
    aging = assess_freshness(now - timedelta(days=800), now=now)
    stale = assess_freshness(now - timedelta(days=3000), now=now)
    very_stale = assess_freshness(now - timedelta(days=9000), now=now)
    assert (fresh.status, aging.status, stale.status) == ("fresh", "aging", "stale")
    assert fresh.age_days == 30
    assert fresh.factor > aging.factor > stale.factor >= very_stale.factor >= 0.75
    future = assess_freshness(now + timedelta(days=1), now=now)
    assert future.status == "fresh" and future.age_days == 0


def test_prior_score_puts_primary_sources_first() -> None:
    official = prior_score("https://toolkit.dev/install", 5, query_hits=1, primary_domains=PRIMARY)
    forum_top = prior_score("https://stackoverflow.com/q/1", 0, query_hits=1, primary_domains=PRIMARY)
    assert official > forum_top
    many_hits = prior_score("https://random.net/a", 2, query_hits=3, primary_domains=())
    one_hit = prior_score("https://random.net/a", 2, query_hits=1, primary_domains=())
    assert many_hits > one_hit
