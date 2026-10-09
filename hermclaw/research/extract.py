"""Main-text extraction (P12 12.5) and publication dates for freshness (12.8).

HTML is parsed with BeautifulSoup + lxml. Boilerplate (scripts, styles, navigation, page header/footer, asides,
forms, hidden elements) is dropped; the main content is taken from ``<main>``/``[role=main]``, else from the
top-level ``<article>`` elements, else from ``<body>``. Dates come from JSON-LD (``datePublished``/``dateModified``),
well-known meta tags and ``<time datetime>`` elements. The content hash is the SHA-256 of the extracted text, so
markup-only changes do not create "new" documents.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from hermclaw.research.text import clean_line

DROP_TAGS: tuple[str, ...] = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
    "form",
    "button",
    "input",
    "select",
    "textarea",
    "nav",
    "footer",
    "aside",
    "dialog",
)
DROP_ROLES: frozenset[str] = frozenset({"navigation", "banner", "contentinfo", "complementary", "search", "menu", "menubar", "dialog"})
BLOCK_TAGS: tuple[str, ...] = (
    "p",
    "div",
    "section",
    "article",
    "main",
    "li",
    "ul",
    "ol",
    "dl",
    "dt",
    "dd",
    "pre",
    "blockquote",
    "table",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "figure",
    "figcaption",
    "details",
    "summary",
    "hr",
)
PUBLISHED_META: frozenset[str] = frozenset(
    {
        "article:published_time",
        "og:published_time",
        "datepublished",
        "date",
        "dc.date",
        "dc.date.issued",
        "dc.date.created",
        "dcterms.issued",
        "dcterms.created",
        "dcterms.date",
        "publish-date",
        "publish_date",
        "publishdate",
        "pubdate",
        "parsely-pub-date",
        "sailthru.date",
        "citation_publication_date",
        "citation_date",
        "release_date",
    }
)
MODIFIED_META: frozenset[str] = frozenset(
    {"article:modified_time", "og:updated_time", "datemodified", "dc.date.modified", "dcterms.modified", "last-modified", "revised"}
)
_LD_PUBLISHED = ("datePublished", "dateCreated", "uploadDate")
_LD_MODIFIED = ("dateModified",)
_TEXT_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%d.%m.%Y", "%Y/%m/%d", "%B %Y", "%Y-%m")
_EARLIEST = datetime(1990, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class ExtractedDocument:
    title: str
    text: str
    published_at: datetime | None
    modified_at: datetime | None
    content_hash: str
    language: str | None
    kind: str  # html|text|json

    @property
    def freshness_date(self) -> datetime | None:
        """Most recent known date (an updated document is as fresh as its last modification)."""
        dates = [d for d in (self.published_at, self.modified_at) if d is not None]
        return max(dates) if dates else None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


# ----------------------------------------------------------------------------------------------- dates
def parse_date(value: str, *, now: datetime | None = None) -> datetime | None:
    """Parse ISO-8601, RFC-2822 and common textual dates to an aware UTC datetime; implausible dates → None."""
    raw = " ".join(value.strip().split())
    if not raw or len(raw) > 64:
        return None
    dt = _parse_any(raw)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    current = now or datetime.now(UTC)
    if dt < _EARLIEST or dt > current + timedelta(days=2):
        return None
    return dt


def _parse_any(raw: str) -> datetime | None:
    iso = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        pass
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)", raw)
    if m:  # ISO with odd fractional/zone suffix
        try:
            return datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}")
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        pass
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw)
    for fmt in _TEXT_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _walk_ld(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, dict | list):
                yield from _walk_ld(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_ld(item)


def _jsonld_dates(soup: BeautifulSoup) -> tuple[datetime | None, datetime | None]:
    published: datetime | None = None
    modified: datetime | None = None
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for obj in _walk_ld(data):
            for key in _LD_PUBLISHED:
                if published is None and isinstance(obj.get(key), str):
                    published = parse_date(obj[key])
            for key in _LD_MODIFIED:
                if modified is None and isinstance(obj.get(key), str):
                    modified = parse_date(obj[key])
    return published, modified


def _meta_dates(soup: BeautifulSoup) -> tuple[datetime | None, datetime | None]:
    published: datetime | None = None
    modified: datetime | None = None
    for meta in soup.find_all("meta"):
        key = ""
        for attr in ("property", "name", "itemprop", "http-equiv"):
            val = meta.get(attr)
            if isinstance(val, str) and val.strip():
                key = val.strip().lower()
                break
        content = meta.get("content")
        if not key or not isinstance(content, str):
            continue
        if published is None and key in PUBLISHED_META:
            published = parse_date(content)
        elif modified is None and key in MODIFIED_META:
            modified = parse_date(content)
    return published, modified


def _time_dates(soup: BeautifulSoup) -> tuple[datetime | None, datetime | None]:
    published: datetime | None = None
    modified: datetime | None = None
    first: datetime | None = None
    for el in soup.find_all("time"):
        val = el.get("datetime")
        raw = val if isinstance(val, str) and val.strip() else el.get_text(" ", strip=True)
        dt = parse_date(raw) if raw else None
        if dt is None:
            continue
        itemprop = str(el.get("itemprop") or "").lower()
        if published is None and (itemprop == "datepublished" or el.has_attr("pubdate")):
            published = dt
        elif modified is None and itemprop == "datemodified":
            modified = dt
        elif first is None and el.find_parent(["article", "main"]) is not None:
            first = dt
    return published or first, modified


# ----------------------------------------------------------------------------------------------- HTML
def _title(soup: BeautifulSoup) -> str:
    for key, value in (("property", "og:title"), ("name", "twitter:title")):
        meta = soup.find("meta", attrs={key: value})
        if isinstance(meta, Tag) and isinstance(meta.get("content"), str) and str(meta["content"]).strip():
            return clean_line(str(meta["content"]))[:300]
    if soup.title and soup.title.get_text(strip=True):
        return clean_line(soup.title.get_text(" ", strip=True))[:300]
    h1 = soup.find("h1")
    if isinstance(h1, Tag):
        return clean_line(h1.get_text(" ", strip=True))[:300]
    return ""


def _is_hidden(el: Tag) -> bool:
    if el.attrs is None:
        return False
    if el.has_attr("hidden") or str(el.get("aria-hidden", "")).lower() == "true":
        return True
    style = str(el.get("style", "")).replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    return str(el.get("role", "")).lower() in DROP_ROLES


def _strip_boilerplate(soup: BeautifulSoup) -> None:
    for el in soup.find_all(DROP_TAGS):
        el.decompose()
    for el in soup.find_all(True):
        if el.decomposed:
            continue
        if _is_hidden(el):
            el.decompose()
    # a page-level <header> (outside the content) is site chrome; an <article><header> holds title/date
    for el in soup.find_all("header"):
        if not el.decomposed and el.find_parent(["article", "main"]) is None:
            el.decompose()


def _root(soup: BeautifulSoup) -> Tag | list[Tag]:
    body = soup.body if isinstance(soup.body, Tag) else soup
    mains = [m for m in soup.find_all(["main"]) if isinstance(m, Tag)] or [
        m for m in soup.find_all(True, role="main") if isinstance(m, Tag)
    ]
    body_len = len(body.get_text(" ", strip=True))
    if len(mains) == 1 and len(mains[0].get_text(" ", strip=True)) >= min(200, body_len * 0.2):
        return mains[0]
    articles = [a for a in soup.find_all("article") if isinstance(a, Tag) and a.find_parent("article") is None]
    if articles:
        total = sum(len(a.get_text(" ", strip=True)) for a in articles)
        if total >= min(200, body_len * 0.2):
            return articles
    return body  # type: ignore[return-value]


def _block_text(root: Tag) -> str:
    for br in root.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for el in root.find_all(BLOCK_TAGS):
        el.insert_before(NavigableString("\n"))
        el.append(NavigableString("\n"))
    lines = [clean_line(line) for line in root.get_text().splitlines()]
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(line)
        blank = False
    return "\n".join(out).strip()


def extract_html(html: str, *, url: str = "", max_chars: int = 200_000) -> ExtractedDocument:
    soup = BeautifulSoup(html, "lxml")
    ld_pub, ld_mod = _jsonld_dates(soup)
    meta_pub, meta_mod = _meta_dates(soup)
    time_pub, time_mod = _time_dates(soup)
    title = _title(soup)
    html_tag = soup.find("html")
    lang_attr = html_tag.get("lang") if isinstance(html_tag, Tag) else None
    language = str(lang_attr).strip()[:16] if lang_attr else None
    _strip_boilerplate(soup)
    root = _root(soup)
    text = "\n\n".join(_block_text(a) for a in root) if isinstance(root, list) else _block_text(root)
    text = text[:max_chars]
    return ExtractedDocument(
        title=title or url,
        text=text,
        published_at=ld_pub or meta_pub or time_pub,
        modified_at=ld_mod or meta_mod or time_mod,
        content_hash=content_hash(text),
        language=language,
        kind="html",
    )


def extract_document(content: str, *, content_type: str, url: str = "", max_chars: int = 200_000) -> ExtractedDocument:
    """Dispatch on the MIME type (as returned by the fetcher)."""
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime in {"text/html", "application/xhtml+xml"} or (not mime and "<html" in content[:2000].lower()):
        return extract_html(content, url=url, max_chars=max_chars)
    if mime == "application/json":
        try:
            text = json.dumps(json.loads(content), indent=1, ensure_ascii=False)
        except ValueError:
            text = content
        text = text[:max_chars]
        return ExtractedDocument(
            title=url, text=text, published_at=None, modified_at=None, content_hash=content_hash(text), language=None, kind="json"
        )
    lines = [clean_line(line) for line in content.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:max_chars]
    first = next((line for line in lines if line), "")
    title = first[:120] if first and len(first) <= 120 else url
    return ExtractedDocument(
        title=title, text=text, published_at=None, modified_at=None, content_hash=content_hash(text), language=None, kind="text"
    )
