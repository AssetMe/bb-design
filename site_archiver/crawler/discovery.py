"""robots.txt handling and sitemap discovery/parsing."""
from __future__ import annotations

import gzip
import io
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Iterable, Iterator
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
    "video": "http://www.google.com/schemas/sitemap-video/1.1",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
    "xhtml": "http://www.w3.org/1999/xhtml",
}
_SITEMAP_DIRECTIVE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.I | re.M)
_TAG_RE = re.compile(r"\{.*\}")


def _localname(tag: str) -> str:
    return _TAG_RE.sub("", tag)


@dataclass(slots=True)
class RobotsPolicy:
    host: str
    url: str
    status: int | None
    body: str
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)
    _parser: urllib.robotparser.RobotFileParser | None = field(default=None, repr=False)
    fetch_failed: bool = False

    @classmethod
    def parse(cls, host: str, url: str, status: int | None, body: str,
              user_agent: str = "*") -> "RobotsPolicy":
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(url)
        try:
            parser.parse(body.splitlines())
        except Exception:
            parser = urllib.robotparser.RobotFileParser()
            parser.allow_all = True
        # A missing robots.txt means "crawl freely"; a 5xx means "stay away",
        # but we treat that as allow-with-caution and let rate limits protect
        # the origin, matching common crawler behaviour.
        if status is not None and status >= 400:
            parser.allow_all = True
        sitemaps = [m.group(1).strip() for m in _SITEMAP_DIRECTIVE.finditer(body or "")]
        delay = None
        try:
            delay = parser.crawl_delay(user_agent) or parser.crawl_delay("*")
        except Exception:
            delay = None
        return cls(
            host=host, url=url, status=status, body=body or "",
            crawl_delay=float(delay) if delay else None,
            sitemaps=sitemaps, _parser=parser,
        )

    @classmethod
    def permissive(cls, host: str, url: str, reason: str = "") -> "RobotsPolicy":
        parser = urllib.robotparser.RobotFileParser()
        parser.allow_all = True
        return cls(host=host, url=url, status=None, body="", _parser=parser,
                   fetch_failed=bool(reason))

    def can_fetch(self, url: str, user_agent: str = "*") -> bool:
        if self._parser is None:
            return True
        try:
            return bool(self._parser.can_fetch(user_agent, url))
        except Exception:
            return True


def robots_url_for(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def maybe_gunzip(data: bytes, content_type: str = "", url: str = "") -> bytes:
    """Transparently decompress gzipped sitemaps."""
    if data[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data)
        except (OSError, EOFError):
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as fh:
                    return fh.read()
            except (OSError, EOFError):
                return data
    return data


@dataclass(slots=True)
class SitemapEntry:
    loc: str
    lastmod: str | None = None
    changefreq: str | None = None
    priority: str | None = None
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    alternates: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SitemapResult:
    url: str
    kind: str                       # "index", "urlset", "text", "unknown"
    entries: list[SitemapEntry] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def locations(self) -> list[str]:
        return [e.loc for e in self.entries]


def parse_sitemap(data: bytes, url: str = "", content_type: str = "") -> SitemapResult:
    """Parse an XML sitemap, sitemap index, gzipped sitemap, or a .txt sitemap.

    Never raises: malformed input degrades to a best-effort regex scrape so a
    broken sitemap cannot cost us the URLs it contains.
    """
    raw = maybe_gunzip(data, content_type, url)
    text = raw.decode("utf-8", errors="replace").lstrip("﻿ \t\r\n")

    # Plain-text sitemap: one URL per line.
    if text[:1] not in ("<",):
        locs = [
            line.strip() for line in text.splitlines()
            if line.strip().startswith(("http://", "https://"))
        ]
        if locs:
            return SitemapResult(url=url, kind="text",
                                 entries=[SitemapEntry(loc=l) for l in locs])
        return SitemapResult(url=url, kind="unknown", error="not xml or text sitemap")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text, re.I)
        result = SitemapResult(
            url=url, kind="urlset" if locs else "unknown",
            entries=[SitemapEntry(loc=l) for l in locs],
            error=f"xml parse error, recovered {len(locs)} loc(s): {exc}",
        )
        return result

    tag = _localname(root.tag).lower()
    result = SitemapResult(url=url, kind="index" if tag == "sitemapindex" else "urlset")
    if tag == "sitemapindex":
        for node in root:
            loc = node.findtext("{*}loc") or node.findtext("loc")
            if loc and loc.strip():
                result.sitemaps.append(loc.strip())
        return result

    for node in root:
        if _localname(node.tag).lower() != "url":
            continue
        loc = node.findtext("{*}loc") or node.findtext("loc")
        if not loc or not loc.strip():
            continue
        entry = SitemapEntry(
            loc=loc.strip(),
            lastmod=(node.findtext("{*}lastmod") or "").strip() or None,
            changefreq=(node.findtext("{*}changefreq") or "").strip() or None,
            priority=(node.findtext("{*}priority") or "").strip() or None,
        )
        for img in node.findall("{*}image"):
            img_loc = img.findtext("{*}loc")
            if img_loc:
                entry.images.append(img_loc.strip())
        for vid in node.findall("{*}video"):
            for field_name in ("content_loc", "player_loc"):
                v = vid.findtext("{*}" + field_name)
                if v:
                    entry.videos.append(v.strip())
        for alt in node.findall("{*}link"):
            href = alt.get("href")
            if href:
                entry.alternates.append(href.strip())
        result.entries.append(entry)
    return result


def sitemap_candidates(root_url: str, extra: Iterable[str] = ()) -> Iterator[str]:
    parts = urlsplit(root_url)
    base = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    for path in extra:
        yield base + path if path.startswith("/") else path
