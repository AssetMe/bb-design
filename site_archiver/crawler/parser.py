"""HTML parsing: links, structured data, text, pagination and comments.

Every extractor here is defensive.  A parse failure must never cost us the
archive: callers keep the raw bytes regardless, and each extractor returns
whatever it managed to find plus a list of non-fatal problems.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, FeatureNotFound

try:
    from bs4 import XMLParsedAsHTMLWarning
    import warnings
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:  # pragma: no cover
    pass

BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "td", "th", "dt", "dd",
    "blockquote", "pre", "figcaption", "caption", "label", "br", "hr",
}
DROP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "iframe"}

PAGINATION_REL = {"next", "prev", "previous", "first", "last"}
_PAGE_TEXT_RE = re.compile(
    r"^\s*(next|older|newer|prev(ious)?|more|→|←|»|«|›|‹|\d{1,4})\s*$", re.I
)
_PAGE_URL_RE = re.compile(
    r"(?:[?&](?:page|p|pg|paged|offset|start|from|skip)=\d+)|(?:/page[/-]\d+)|(?:/\d+/?$)", re.I
)
_LOAD_MORE_RE = re.compile(r"load[-_ ]?more|show[-_ ]?more|ещ[её]|показать|больше", re.I)

COMMENT_CONTAINER_SELECTORS = [
    "[itemtype*='Comment']", "[itemtype*='Review']",
    ".comment", ".comments li", ".comment-item", ".comment-body",
    ".review", ".review-item", ".reviews li", ".testimonial",
    "article.comment", "li.comment", "div.comment",
    "[id^='comment-']", "[class*='comment-list'] > *",
    "[data-comment-id]", "[data-review-id]",
]
COMMENT_ROOT_SELECTORS = [
    "#comments", ".comments", ".comment-list", ".commentlist",
    "#reviews", ".reviews", ".review-list", "[data-comments]",
]

_AUTHOR_SEL = [
    "[itemprop='author']", ".comment-author", ".author", ".comment-meta .fn",
    ".fn", ".user-name", ".username", ".comment__author", ".review-author",
    "[data-author]", "cite",
]
_DATE_SEL = [
    "time", "[itemprop='datePublished']", ".comment-date", ".date",
    ".comment-meta time", ".timestamp", "[datetime]", ".comment__date",
]
_BODY_SEL = [
    "[itemprop='reviewBody']", "[itemprop='text']", ".comment-text",
    ".comment-content", ".comment-body", ".comment__text", ".review-text",
    ".review-body", ".content", "p",
]
#: Matches wrapper classes like "replies", "comment-children", "responses".
#: Note "reply" alone does not match "replies", hence the explicit stem.
_REPLY_WRAPPER_RE = re.compile(r"repl(y|ies|ie)|children|child-comments|responses|answers", re.I)

_RATING_SEL = [
    "[itemprop='ratingValue']", ".rating", ".stars", "[data-rating]", ".score",
]

EMBEDDED_JSON_IDS = (
    "__NEXT_DATA__", "__NUXT__", "__APOLLO_STATE__", "__INITIAL_STATE__",
    "__PRELOADED_STATE__", "wp-json", "shopify-features",
)
_STATE_ASSIGN_RE = re.compile(
    r"(?:window|self|globalThis)\.(__[A-Z0-9_]+__|[A-Za-z_$][\w$]*)\s*=\s*(\{.*?\}|\[.*?\])\s*;",
    re.S,
)


def make_soup(markup: str | bytes) -> tuple[BeautifulSoup, str]:
    """Parse HTML, preferring lxml and degrading gracefully."""
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(markup, parser), parser
        except (FeatureNotFound, Exception):
            continue
    return BeautifulSoup("", "html.parser"), "none"


@dataclass(slots=True)
class ExtractedLink:
    url: str
    text: str | None = None
    rel: str | None = None
    link_type: str = "a"


@dataclass(slots=True)
class ExtractedComment:
    position: int
    comment_key: str | None = None
    parent_key: str | None = None
    author: str | None = None
    author_url: str | None = None
    published: str | None = None
    rating: str | None = None
    body: str | None = None
    source: str = "html"
    raw_html: str | None = None
    replies: list["ExtractedComment"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "comment_id": self.comment_key,
            "parent_id": self.parent_key,
            "author": self.author,
            "author_url": self.author_url,
            "date": self.published,
            "rating": self.rating,
            "text": self.body,
            "source": self.source,
            "position": self.position,
            "replies": [r.to_dict() for r in self.replies],
        }
        return {k: v for k, v in d.items() if v is not None or k in ("rating", "replies")}


@dataclass(slots=True)
class StructuredItem:
    kind: str
    subtype: str | None
    raw: str
    parsed: Any | None = None
    parse_error: str | None = None
    position: int = 0


@dataclass(slots=True)
class ParseResult:
    title: str | None = None
    canonical: str | None = None
    meta: dict[str, str] = field(default_factory=dict)
    links: list[ExtractedLink] = field(default_factory=list)
    pagination: list[ExtractedLink] = field(default_factory=list)
    structured: list[StructuredItem] = field(default_factory=list)
    comments: list[ExtractedComment] = field(default_factory=list)
    comments_html: str | None = None
    text: str = ""
    lang: str | None = None
    parser_used: str = ""
    problems: list[str] = field(default_factory=list)
    has_load_more: bool = False
    body_text_length: int = 0
    script_count: int = 0


# ----------------------------------------------------------------------
def _attr_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value) or None
    return str(value) or None


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = re.sub(r"[ \t\xa0]+", " ", text).strip()
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    return collapsed or None


def _first(node: Any, selectors: Iterable[str]) -> Any | None:
    for sel in selectors:
        try:
            found = node.select_one(sel)
        except Exception:
            continue
        if found is not None:
            return found
    return None


def extract_text(soup: BeautifulSoup) -> str:
    """Readable text with structure preserved (headings, lists, tables)."""
    body = soup.body or soup
    clone = body
    for tag in clone.find_all(DROP_TAGS):
        tag.decompose()
    lines: list[str] = []
    for element in clone.descendants:
        name = getattr(element, "name", None)
        if name is None:
            text = str(element).strip()
            if text:
                lines.append(text)
        elif name in ("br", "hr"):
            lines.append("\n")
        elif name in BLOCK_TAGS:
            lines.append("\n")
        elif name == "img":
            alt = _attr_str(element.get("alt"))
            if alt:
                lines.append(f"[image: {alt}]")
    joined = " ".join(lines)
    joined = re.sub(r"[ \t\xa0]+", " ", joined)
    joined = re.sub(r" *\n *", "\n", joined)
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return joined.strip()


def extract_meta(soup: BeautifulSoup) -> dict[str, str]:
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name") or tag.get("itemprop") or tag.get("http-equiv")
        content = tag.get("content")
        if key and content is not None:
            meta[str(key).strip().lower()] = str(content).strip()
    return meta


def extract_structured(soup: BeautifulSoup, meta: dict[str, str]) -> list[StructuredItem]:
    items: list[StructuredItem] = []
    pos = 0

    # JSON-LD - always keep the raw text, even when it will not parse.
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        pos += 1
        try:
            parsed = json.loads(raw)
            subtype = None
            if isinstance(parsed, dict):
                subtype = parsed.get("@type")
            elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                subtype = parsed[0].get("@type")
            items.append(StructuredItem("jsonld", _attr_str(subtype), raw, parsed, None, pos))
        except (json.JSONDecodeError, ValueError) as exc:
            items.append(StructuredItem("jsonld", None, raw, None, str(exc), pos))

    og = {k: v for k, v in meta.items() if k.startswith("og:")}
    if og:
        pos += 1
        items.append(StructuredItem("opengraph", None, json.dumps(og, ensure_ascii=False), og, None, pos))
    tw = {k: v for k, v in meta.items() if k.startswith("twitter:")}
    if tw:
        pos += 1
        items.append(StructuredItem("twitter", None, json.dumps(tw, ensure_ascii=False), tw, None, pos))
    plain = {k: v for k, v in meta.items() if not k.startswith(("og:", "twitter:"))}
    if plain:
        pos += 1
        items.append(StructuredItem("meta", None, json.dumps(plain, ensure_ascii=False), plain, None, pos))

    # Microdata: one record per top-level itemscope.
    for node in soup.select("[itemscope]"):
        if node.find_parent(attrs={"itemscope": True}) is not None:
            continue
        record = _microdata(node)
        if record.get("properties"):
            pos += 1
            items.append(StructuredItem(
                "microdata", record.get("type"),
                json.dumps(record, ensure_ascii=False), record, None, pos))

    # Embedded JSON blobs used to hydrate the page.
    for script in soup.find_all("script"):
        stype = (script.get("type") or "").lower()
        if "ld+json" in stype:
            continue
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        sid = script.get("id") or ""
        if stype in ("application/json", "application/x-json") or sid in EMBEDDED_JSON_IDS:
            pos += 1
            try:
                items.append(StructuredItem(
                    "embedded_json", sid or stype, raw, json.loads(raw), None, pos))
            except (json.JSONDecodeError, ValueError) as exc:
                items.append(StructuredItem("embedded_json", sid or stype, raw, None, str(exc), pos))
            continue
        if len(raw) < 200_000:
            for match in _STATE_ASSIGN_RE.finditer(raw):
                name, blob = match.group(1), match.group(2)
                pos += 1
                try:
                    items.append(StructuredItem(
                        "embedded_json", name, blob, json.loads(blob), None, pos))
                except (json.JSONDecodeError, ValueError) as exc:
                    items.append(StructuredItem("embedded_json", name, blob, None, str(exc), pos))
    return items


def _microdata(node: Any, depth: int = 0) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": _attr_str(node.get("itemtype")),
        "id": _attr_str(node.get("itemid")),
        "properties": {},
    }
    if depth > 6:
        return record
    for prop in node.find_all(attrs={"itemprop": True}):
        owner = prop.find_parent(attrs={"itemscope": True})
        if owner is not node and prop is not node:
            continue
        name = _attr_str(prop.get("itemprop"))
        if not name:
            continue
        if prop.has_attr("itemscope"):
            value: Any = _microdata(prop, depth + 1)
        else:
            value = (
                prop.get("content") or prop.get("datetime") or prop.get("href")
                or prop.get("src") or _clean(prop.get_text(" "))
            )
            value = _attr_str(value)
        record["properties"].setdefault(name, []).append(value)
    return record


def extract_links(soup: BeautifulSoup, base_url: str) -> tuple[list[ExtractedLink], list[ExtractedLink], str | None]:
    links: list[ExtractedLink] = []
    pagination: list[ExtractedLink] = []
    canonical: str | None = None
    seen: set[tuple[str, str]] = set()

    def add(href: str | None, text: str | None, rel: str | None, ltype: str,
            bucket: list[ExtractedLink]) -> None:
        if not href:
            return
        href = href.strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            return
        try:
            absolute = urljoin(base_url, href)
        except ValueError:
            return
        key = (absolute, ltype)
        if key in seen:
            return
        seen.add(key)
        bucket.append(ExtractedLink(absolute, _clean(text), rel, ltype))

    for tag in soup.find_all(["a", "area"]):
        rel = _attr_str(tag.get("rel"))
        href = _attr_str(tag.get("href"))
        text = _clean(tag.get_text(" "))
        add(href, text, rel, tag.name, links)
        rel_lower = (rel or "").lower()
        if any(r in PAGINATION_REL for r in rel_lower.split()):
            add(href, text, rel, "pagination", pagination)
        elif href and (_PAGE_TEXT_RE.match(text or "") or _PAGE_URL_RE.search(href)):
            cls = " ".join(tag.get("class") or []).lower()
            parent_cls = " ".join((tag.parent.get("class") or []) if tag.parent else []).lower()
            if ("pag" in cls or "pag" in parent_cls or "next" in cls or "page" in cls
                    or _PAGE_TEXT_RE.match(text or "")):
                add(href, text, rel, "pagination", pagination)

    for tag in soup.find_all("link"):
        rel = " ".join(tag.get("rel") or []).lower()
        href = _attr_str(tag.get("href"))
        if not href:
            continue
        if "canonical" in rel and canonical is None:
            canonical = urljoin(base_url, href.strip())
            add(href, None, rel, "canonical", links)
        elif "alternate" in rel:
            add(href, tag.get("hreflang"), rel, "alternate", links)
        elif any(r in PAGINATION_REL for r in rel.split()):
            add(href, None, rel, "pagination", pagination)

    for key in ("og:url", "twitter:url"):
        node = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if node and node.get("content"):
            add(str(node["content"]), None, key, "opengraph", links)

    # Forms that represent public GET navigation (search/filter).
    for form in soup.find_all("form"):
        method = (form.get("method") or "get").lower()
        action = _attr_str(form.get("action"))
        if method == "get" and action:
            add(action, _clean(form.get("aria-label") or form.get("name")), None, "form", links)

    return links, pagination, canonical


def links_from_structured(items: Iterable[StructuredItem], base_url: str) -> list[ExtractedLink]:
    """Pull url-ish strings out of JSON-LD / embedded JSON."""
    out: list[ExtractedLink] = []
    seen: set[str] = set()
    url_keys = {"url", "@id", "contenturl", "mainentityofpage", "sameas", "item",
                "thumbnailurl", "image", "href", "permalink", "link", "canonical"}

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and k.lower() in url_keys:
                    if v.startswith(("http://", "https://", "/")):
                        absolute = urljoin(base_url, v)
                        if absolute not in seen:
                            seen.add(absolute)
                            out.append(ExtractedLink(absolute, None, None, "jsonld"))
                else:
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    for item in items:
        if item.parsed is not None and item.kind in ("jsonld", "embedded_json", "microdata"):
            walk(item.parsed)
    return out


# ----------------------------------------------------------------------
def extract_comments(soup: BeautifulSoup, base_url: str,
                     source: str = "html") -> tuple[list[ExtractedComment], str | None]:
    """Find publicly rendered comment/review blocks.

    Uses schema.org markup first (reliable), then common class-name patterns.
    Returns the comments plus the raw HTML of the comment region for archival.
    """
    nodes: list[Any] = []
    seen_ids: set[int] = set()
    for sel in COMMENT_CONTAINER_SELECTORS:
        try:
            found = soup.select(sel)
        except Exception:
            continue
        for node in found:
            if id(node) in seen_ids:
                continue
            # Skip a node that merely contains other candidates.
            seen_ids.add(id(node))
            nodes.append(node)
    # Drop candidates nested inside another candidate (those become replies).
    top: list[Any] = []
    node_set = set(id(n) for n in nodes)
    for node in nodes:
        parent = node.parent
        nested = False
        while parent is not None:
            if id(parent) in node_set:
                nested = True
                break
            parent = parent.parent
        if not nested:
            top.append(node)

    comments: list[ExtractedComment] = []
    for i, node in enumerate(top, start=1):
        comment = _comment_from_node(node, base_url, i, source, node_set)
        if comment and (comment.body or comment.author):
            comments.append(comment)

    region = None
    root = _first(soup, COMMENT_ROOT_SELECTORS)
    if root is not None:
        region = str(root)
    elif comments and top:
        region = "\n".join(str(n) for n in top)
    return comments, region


def _comment_from_node(node: Any, base_url: str, position: int, source: str,
                       candidate_ids: set[int]) -> ExtractedComment | None:
    try:
        key = (
            _attr_str(node.get("data-comment-id")) or _attr_str(node.get("data-review-id"))
            or _attr_str(node.get("data-id")) or _attr_str(node.get("id"))
        )
        author_node = _first(node, _AUTHOR_SEL)
        author = _clean(author_node.get_text(" ")) if author_node is not None else None
        if author_node is not None and not author:
            author = _attr_str(author_node.get("content"))
        author_url = None
        if author_node is not None:
            link = author_node if author_node.name == "a" else author_node.find("a")
            if link is not None and link.get("href"):
                author_url = urljoin(base_url, str(link["href"]).strip())

        date_node = _first(node, _DATE_SEL)
        published = None
        if date_node is not None:
            published = (
                _attr_str(date_node.get("datetime")) or _attr_str(date_node.get("content"))
                or _clean(date_node.get_text(" "))
            )

        rating_node = _first(node, _RATING_SEL)
        rating = None
        if rating_node is not None:
            rating = (
                _attr_str(rating_node.get("content")) or _attr_str(rating_node.get("data-rating"))
                or _clean(rating_node.get_text(" "))
            )

        body = None
        body_node = _first(node, _BODY_SEL)
        if body_node is not None:
            body = _clean(body_node.get_text("\n"))
        if not body:
            # Fall back to the node's own text minus the metadata we lifted out.
            text = _clean(node.get_text("\n")) or ""
            for part in (author, published, rating):
                if part:
                    text = text.replace(part, "", 1)
            body = _clean(text)

        # Nested comment nodes inside a .replies/.children wrapper are this
        # comment's replies - they were excluded from the top-level list
        # precisely so they could be attached here.
        replies: list[ExtractedComment] = []
        for child in node.find_all(attrs={"class": _REPLY_WRAPPER_RE}):
            for k, sub in enumerate(child.find_all(True, recursive=False), 1):
                reply = _comment_from_node(sub, base_url, k, source, candidate_ids)
                if reply and (reply.body or reply.author):
                    reply.parent_key = key
                    replies.append(reply)

        return ExtractedComment(
            position=position, comment_key=key, author=author, author_url=author_url,
            published=published, rating=rating, body=body, source=source,
            raw_html=str(node)[:200_000], replies=replies,
        )
    except Exception:
        return None


def comments_from_structured(items: Iterable[StructuredItem]) -> list[ExtractedComment]:
    """Comments/reviews declared in JSON-LD."""
    out: list[ExtractedComment] = []
    pos = 0

    def as_text(value: Any) -> str | None:
        if isinstance(value, str):
            return _clean(value)
        if isinstance(value, dict):
            return _clean(value.get("name") or value.get("@id") or value.get("url"))
        if isinstance(value, list) and value:
            return as_text(value[0])
        return None

    def url_of(value: Any) -> str | None:
        if isinstance(value, dict):
            u = value.get("url") or value.get("@id")
            return u if isinstance(u, str) else None
        return None

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal pos
        if depth > 10:
            return
        if isinstance(node, list):
            for v in node:
                walk(v, depth + 1)
            return
        if not isinstance(node, dict):
            return
        typ = str(node.get("@type") or "").lower()
        if typ in ("comment", "review", "usercomments", "answer"):
            pos += 1
            rating = node.get("reviewRating")
            if isinstance(rating, dict):
                rating = rating.get("ratingValue")
            out.append(ExtractedComment(
                position=pos,
                comment_key=as_text(node.get("identifier")) or as_text(node.get("@id")),
                author=as_text(node.get("author") or node.get("creator")),
                author_url=url_of(node.get("author")),
                published=as_text(node.get("datePublished") or node.get("dateCreated")),
                rating=as_text(rating),
                body=as_text(node.get("reviewBody") or node.get("text") or node.get("description")),
                source="jsonld",
            ))
        for v in node.values():
            walk(v, depth + 1)

    for item in items:
        if item.parsed is not None and item.kind == "jsonld":
            walk(item.parsed)
    return out


def detect_load_more(soup: BeautifulSoup) -> bool:
    for tag in soup.find_all(["button", "a", "div", "span"]):
        cls = " ".join(tag.get("class") or [])
        ident = tag.get("id") or ""
        text = tag.get_text(" ", strip=True)[:60]
        if (_LOAD_MORE_RE.search(cls) or _LOAD_MORE_RE.search(str(ident))
                or _LOAD_MORE_RE.search(text) or tag.has_attr("data-load-more")):
            return True
    return False


def classify_page(url: str, soup: BeautifulSoup, structured: list[StructuredItem],
                  text: str) -> str:
    """Coarse page-type label used to pick specialised parsers later."""
    from urllib.parse import urlsplit

    path = urlsplit(url).path.rstrip("/")
    types = {(s.subtype or "").lower() for s in structured if s.kind in ("jsonld", "microdata")}
    if any("article" in t or "blogposting" in t or "newsarticle" in t for t in types):
        return "ARTICLE"
    if any("product" in t or "person" in t or "profile" in t or "offer" in t for t in types):
        return "DETAIL"
    if any("collectionpage" in t or "itemlist" in t for t in types):
        return "LISTING"
    if not path or path == "":
        return "HOME"
    title = (soup.title.get_text(strip=True) if soup.title else "") or ""
    if re.search(r"\b(404|not found|page not found)\b", title, re.I):
        return "ERROR"
    if re.search(r"/(search|poisk|find)\b", path, re.I) or "?" in url and "q=" in url:
        return "SEARCH"
    if re.search(r"/(category|categories|rubrika|tag|tags|catalog|section)/", path, re.I):
        return "CATEGORY"
    if _PAGE_URL_RE.search(url):
        return "LISTING"
    if soup.find("article") is not None and len(text) > 1200:
        return "ARTICLE"
    links = soup.find_all("a")
    if len(links) > 40 and len(text) < 4000:
        return "LISTING"
    if len(text) > 400:
        return "DETAIL"
    return "OTHER"


# ----------------------------------------------------------------------
def parse_html(markup: str, base_url: str, *, extract_comment_data: bool = True,
               source: str = "html") -> ParseResult:
    """Parse a page.  Never raises - problems are collected, not thrown."""
    result = ParseResult()
    try:
        soup, parser_used = make_soup(markup)
        result.parser_used = parser_used
    except Exception as exc:
        result.problems.append(f"soup failed: {exc}")
        return result

    try:
        result.title = _clean(soup.title.get_text()) if soup.title else None
    except Exception as exc:
        result.problems.append(f"title: {exc}")
    try:
        html_tag = soup.find("html")
        result.lang = _attr_str(html_tag.get("lang")) if html_tag else None
    except Exception:
        pass
    try:
        result.meta = extract_meta(soup)
    except Exception as exc:
        result.problems.append(f"meta: {exc}")
    try:
        result.structured = extract_structured(soup, result.meta)
    except Exception as exc:
        result.problems.append(f"structured: {exc}")
    try:
        base_tag = soup.find("base", href=True)
        effective_base = urljoin(base_url, str(base_tag["href"])) if base_tag else base_url
        links, pagination, canonical = extract_links(soup, effective_base)
        result.links = links
        result.pagination = pagination
        result.canonical = canonical
    except Exception as exc:
        result.problems.append(f"links: {exc}")
        effective_base = base_url
    try:
        result.links.extend(links_from_structured(result.structured, base_url))
    except Exception as exc:
        result.problems.append(f"jsonld links: {exc}")
    try:
        result.script_count = len(soup.find_all("script"))
        result.has_load_more = detect_load_more(soup)
    except Exception:
        pass
    if extract_comment_data:
        try:
            comments, region = extract_comments(soup, base_url, source=source)
            result.comments = comments
            result.comments_html = region
        except Exception as exc:
            result.problems.append(f"comments: {exc}")
        try:
            jsonld_comments = comments_from_structured(result.structured)
            known = {(c.author, (c.body or "")[:80]) for c in result.comments}
            for c in jsonld_comments:
                if (c.author, (c.body or "")[:80]) not in known:
                    c.position = len(result.comments) + 1
                    result.comments.append(c)
        except Exception as exc:
            result.problems.append(f"jsonld comments: {exc}")
    try:
        result.text = extract_text(soup)
        result.body_text_length = len(result.text)
    except Exception as exc:
        result.problems.append(f"text: {exc}")
    return result


def needs_rendering(result: ParseResult, content_length: int) -> str | None:
    """Heuristic: does this page hide its content behind JavaScript?

    Returns a reason string when rendering is warranted, else None.
    """
    text_len = result.body_text_length
    if text_len < 200 and result.script_count > 0:
        return "empty_shell"
    if not result.links and result.script_count > 2:
        return "no_links_scripted"
    if result.has_load_more:
        return "load_more_control"
    ratio = (text_len / content_length) if content_length else 1.0
    if content_length > 20_000 and ratio < 0.01 and result.script_count > 3:
        return "low_text_ratio"
    for item in result.structured:
        if item.kind == "embedded_json" and item.subtype in (
            "__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "__PRELOADED_STATE__"
        ):
            if text_len < 800:
                return f"csr_state:{item.subtype}"
    return None
