"""P12 12.5/12.8: text utilities, HTML main-text extraction and publication dates."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hermclaw.research.extract import content_hash, extract_document, extract_html, parse_date
from hermclaw.research.text import extract_values, is_negated, key_terms, split_sentences, subject_terms, tokenize

DOCS_HTML = """<!doctype html><html lang="en"><head><title>Install – Toolkit Docs</title>
<meta property="article:modified_time" content="2026-03-01T10:00:00Z">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebSite"},
{"@type":"TechArticle","datePublished":"2025-11-02","dateModified":"2026-02-01"}]}</script>
<style>.x { color: red }</style></head>
<body><header><nav><a href="/">Home</a><a href="/api">API</a></nav><div>Site banner text</div></header>
<main><article><header><h1>Installing Toolkit</h1></header>
<p>Toolkit 4 requires <b>Python 3.10</b> or later.</p><p>Install it with <code>pip install toolkit</code>.<br>Done.</p>
<div hidden>hidden secret text</div><div style="display: none">invisible</div><div aria-hidden="true">aria hidden</div>
<ul><li>first item</li><li>second item</li></ul></article></main>
<aside>Related links</aside><footer>Copyright 2026 Toolkit</footer><script>var tracking = 1;</script></body></html>"""


def test_html_extraction_drops_boilerplate_and_prefers_main() -> None:
    doc = extract_html(DOCS_HTML, url="https://docs.toolkit.test/install")
    assert doc.title == "Install – Toolkit Docs"
    assert doc.language == "en"
    assert "Toolkit 4 requires Python 3.10 or later." in doc.text  # inline <b> stays inside the sentence
    assert "Installing Toolkit" in doc.text  # article header kept
    for junk in ("Home", "Site banner", "Related links", "Copyright", "tracking", "hidden secret", "invisible", "aria hidden", "color"):
        assert junk not in doc.text, junk
    assert "pip install toolkit.\nDone." in doc.text  # <br> → newline
    assert "first item\n\nsecond item" in doc.text or "first item\nsecond item" in doc.text
    # JSON-LD wins over meta for published, and for modified
    assert doc.published_at == datetime(2025, 11, 2, tzinfo=UTC)
    assert doc.modified_at == datetime(2026, 2, 1, tzinfo=UTC)
    assert doc.freshness_date == doc.modified_at
    assert doc.content_hash == content_hash(doc.text) and len(doc.content_hash) == 64


def test_content_hash_ignores_markup_only_changes() -> None:
    a = extract_html("<html><body><main><p>Same text here for hashing purposes.</p></main></body></html>")
    b = extract_html(
        "<html><body><main><div class='x'><p>Same   text here for <span>hashing</span> purposes.</p></div></main></body></html>"
    )
    assert a.content_hash == b.content_hash


def test_meta_and_time_dates_and_multiple_articles() -> None:
    html = """<html><head><meta name="date" content="2024-05-06"></head><body>
    <article><p>First answer explains the option in enough detail to be kept by the extractor.</p></article>
    <article><p>Second answer disagrees with the first answer and also has enough text.</p>
    <time datetime="2023-01-02T03:04:05+01:00">Jan 2</time></article></body></html>"""
    doc = extract_html(html)
    assert doc.published_at == datetime(2024, 5, 6, tzinfo=UTC)
    assert "First answer" in doc.text and "Second answer" in doc.text
    only_time = extract_html(
        "<html><body><article><p>Text text text text.</p><time itemprop='datePublished' datetime='2022-02-03'>x</time></article></body></html>"
    )
    assert only_time.published_at == datetime(2022, 2, 3, tzinfo=UTC)
    no_date = extract_html("<html><body><p>Nothing dated here.</p></body></html>")
    assert no_date.published_at is None and no_date.freshness_date is None


def test_broken_jsonld_is_ignored() -> None:
    html = "<html><head><script type='application/ld+json'>{not json</script></head><body><p>Body text.</p></body></html>"
    doc = extract_html(html)
    assert doc.published_at is None and "Body text." in doc.text


def test_plain_text_and_json_documents() -> None:
    txt = extract_document("Release notes\n\n\n\nVersion 2.0   adds streaming.\n", content_type="text/plain", url="u")
    assert txt.kind == "text" and txt.title == "Release notes" and txt.text == "Release notes\n\nVersion 2.0 adds streaming."
    js = extract_document('{"version": "2.0", "requires": ">=3.10"}', content_type="application/json", url="https://x/api.json")
    assert js.kind == "json" and '"version": "2.0"' in js.text and js.title == "https://x/api.json"
    xhtml = extract_document("<html><body><main><p>XHTML body.</p></main></body></html>", content_type="application/xhtml+xml")
    assert xhtml.kind == "html" and "XHTML body." in xhtml.text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-01T10:00:00Z", datetime(2026, 3, 1, 10, tzinfo=UTC)),
        ("2026-03-01", datetime(2026, 3, 1, tzinfo=UTC)),
        ("2024-03-05T10:00:00+02:00", datetime(2024, 3, 5, 8, tzinfo=UTC)),
        ("Tue, 05 Mar 2024 10:00:00 GMT", datetime(2024, 3, 5, 10, tzinfo=UTC)),
        ("March 5th, 2024", datetime(2024, 3, 5, tzinfo=UTC)),
        ("5 March 2024", datetime(2024, 3, 5, tzinfo=UTC)),
        ("05.03.2024", datetime(2024, 3, 5, tzinfo=UTC)),
        ("2099-01-01", None),  # implausible future
        ("1970-01-01", None),  # implausibly old
        ("yesterday", None),
        ("", None),
    ],
)
def test_parse_date(raw: str, expected: datetime | None) -> None:
    assert parse_date(raw, now=datetime(2026, 10, 9, tzinfo=UTC)) == expected


def test_text_values_terms_negation_and_sentences() -> None:
    assert extract_values("requires Python 3.9 or later") == (frozenset({"3.9"}), frozenset())
    assert extract_values("port 8,888 since v2.1.0 (2024)") == (frozenset({"2.1.0"}), frozenset({"8888", "2024"}))
    assert key_terms("How do I install the Toolkit on Python 3.12?") == ["install", "toolkit", "python", "3.12"]
    assert subject_terms("Toolkit 4 does not support Python 3.8") == frozenset({"toolkit", "support", "python"})
    assert is_negated("Streaming isn't supported") and is_negated("Das wird nicht unterstützt") and not is_negated("It works")
    assert tokenize("Use docs.python.org/3/ — it’s fine.") == ["use", "docs.python.org/3", "it's", "fine"]
    sentences = split_sentences("First claim is here. [1] Second, e.g. Python 3.12 works! [2][3] Third one.\n\n- bullet a\n- bullet b")
    assert sentences == ["First claim is here. [1]", "Second, e.g. Python 3.12 works! [2][3]", "Third one.", "- bullet a", "- bullet b"]
