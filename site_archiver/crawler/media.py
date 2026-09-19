"""Asset reference extraction and image metadata."""
from __future__ import annotations

import io
import re
import struct
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 200_000_000
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

#: Attributes lazy-loading libraries use to stash the real URL.
LAZY_ATTRS = (
    "data-src", "data-original", "data-lazy-src", "data-lazy", "data-url",
    "data-image", "data-img", "data-full", "data-large", "data-hi-res",
    "data-zoom-image", "data-echo", "data-defer-src", "data-thumb",
    "data-original-src", "data-actualsrc", "data-fallback-src",
)
LAZY_SRCSET_ATTRS = ("data-srcset", "data-lazy-srcset", "data-responsive")

_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+?)['\"]?\s*\)", re.I)
_SRCSET_SPLIT = re.compile(r"\s*,\s*(?=[^\s,]+\s*(?:[\d.]+[wx])?\s*(?:,|$))")


@dataclass(slots=True)
class AssetRef:
    url: str
    element: str
    attribute: str
    position: int
    asset_type_hint: str | None = None
    alt_text: str | None = None
    width_hint: int | None = None


def parse_srcset(value: str) -> list[tuple[str, float | None, str | None]]:
    """Return [(url, descriptor_value, descriptor_unit)] from a srcset."""
    out: list[tuple[str, float | None, str | None]] = []
    if not value:
        return out
    for candidate in re.split(r",(?![^(]*\))", value):
        candidate = candidate.strip()
        if not candidate:
            continue
        parts = candidate.split()
        url = parts[0]
        num: float | None = None
        unit: str | None = None
        if len(parts) > 1:
            m = re.fullmatch(r"([\d.]+)([wx])", parts[-1])
            if m:
                try:
                    num = float(m.group(1))
                    unit = m.group(2)
                except ValueError:
                    pass
        out.append((url, num, unit))
    return out


def best_from_srcset(value: str) -> tuple[str | None, int | None]:
    """Pick the highest-resolution candidate the page itself offers."""
    candidates = parse_srcset(value)
    if not candidates:
        return None, None
    scored = []
    for url, num, unit in candidates:
        score = 0.0
        if unit == "w" and num:
            score = num
        elif unit == "x" and num:
            score = num * 1000
        scored.append((score, url, num, unit))
    scored.sort(key=lambda t: t[0], reverse=True)
    best = scored[0]
    width = int(best[2]) if best[3] == "w" and best[2] else None
    return best[1], width


def extract_assets(soup: BeautifulSoup, base_url: str,
                   include_css_urls: bool = True) -> list[AssetRef]:
    """Collect every asset URL the page references, preferring the highest
    resolution the page itself supplies."""
    refs: list[AssetRef] = []
    seen: set[tuple[str, str]] = set()
    pos = 0

    def add(raw: str | None, element: str, attribute: str, hint: str | None = None,
            alt: str | None = None, width: int | None = None) -> None:
        nonlocal pos
        if not raw:
            return
        raw = raw.strip()
        if not raw or raw.startswith(("data:", "javascript:", "about:", "blob:", "#")):
            return
        try:
            absolute = urljoin(base_url, raw)
        except ValueError:
            return
        key = (absolute, attribute)
        if key in seen:
            return
        seen.add(key)
        pos += 1
        refs.append(AssetRef(absolute, element, attribute, pos, hint, alt, width))

    for img in soup.find_all("img"):
        alt = img.get("alt")
        best, width = (None, None)
        for attr in ("srcset", *LAZY_SRCSET_ATTRS):
            if img.get(attr):
                best, width = best_from_srcset(str(img[attr]))
                if best:
                    add(best, "img", attr, "image", alt, width)
                    break
        add(img.get("src"), "img", "src", "image", alt)
        for attr in LAZY_ATTRS:
            if img.get(attr):
                add(str(img[attr]), "img", attr, "image", alt)

    for source in soup.find_all("source"):
        parent = source.parent.name if source.parent else ""
        if source.get("srcset"):
            best, width = best_from_srcset(str(source["srcset"]))
            add(best, "source", "srcset", "image" if parent == "picture" else None,
                None, width)
        add(source.get("src"), "source", "src",
            "video" if parent == "video" else ("audio" if parent == "audio" else None))
        for attr in LAZY_SRCSET_ATTRS:
            if source.get(attr):
                best, width = best_from_srcset(str(source[attr]))
                add(best, "source", attr, "image", None, width)

    for tag_name, hint in (("video", "video"), ("audio", "audio"), ("embed", None),
                           ("object", None), ("track", None)):
        for tag in soup.find_all(tag_name):
            add(tag.get("src") or tag.get("data"), tag_name, "src", hint)
            if tag.get("poster"):
                add(str(tag["poster"]), tag_name, "poster", "image")
            for attr in LAZY_ATTRS:
                if tag.get(attr):
                    add(str(tag[attr]), tag_name, attr, hint)

    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        href = link.get("href")
        if not href:
            continue
        if "stylesheet" in rel:
            add(str(href), "link", "stylesheet", "stylesheet")
        elif "icon" in rel or "apple-touch-icon" in rel or "mask-icon" in rel:
            add(str(href), "link", "icon", "image")
        elif "preload" in rel and link.get("as") in ("image", "font", "style", "script"):
            add(str(href), "link", "preload", link.get("as"))
        elif "manifest" in rel:
            add(str(href), "link", "manifest", "data")

    for script in soup.find_all("script", src=True):
        add(str(script["src"]), "script", "src", "script")

    for key, hint in (("og:image", "image"), ("og:image:secure_url", "image"),
                      ("og:video", "video"), ("og:audio", "audio"),
                      ("twitter:image", "image")):
        for meta in soup.find_all("meta", attrs={"property": key}) + \
                    soup.find_all("meta", attrs={"name": key}):
            add(meta.get("content"), "meta", key, hint)

    for tag in soup.find_all(attrs={"style": True}):
        style = str(tag.get("style") or "")
        if "url(" in style:
            for match in _CSS_URL_RE.findall(style):
                add(match, tag.name, "style-url", "image")

    if include_css_urls:
        for style in soup.find_all("style"):
            css = style.string or style.get_text() or ""
            for match in _CSS_URL_RE.findall(css):
                add(match, "style", "css-url", None)

    # Anchors pointing straight at a media/document file.
    doc_ext = re.compile(
        r"\.(pdf|docx?|xlsx?|pptx?|csv|zip|rar|7z|mp4|webm|mov|mp3|ogg|wav|flac|epub)"
        r"(\?|$)", re.I)
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if doc_ext.search(href):
            add(href, "a", "href", None, a.get_text(" ", strip=True)[:200] or None)

    return refs


def urls_from_css(css_text: str, base_url: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for match in _CSS_URL_RE.findall(css_text or ""):
        if match.startswith(("data:", "#")):
            continue
        try:
            absolute = urljoin(base_url, match.strip())
        except ValueError:
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


# ----------------------------------------------------------------------
def image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Width/height without decoding the whole image where possible."""
    if not data:
        return None, None
    # SVG: read the viewBox/width attributes textually.
    head = data[:1024].lstrip()
    if head[:5] == b"<?xml" or head[:4] == b"<svg":
        try:
            text = data[:4096].decode("utf-8", errors="replace")
            w = re.search(r'\bwidth\s*=\s*["\']([\d.]+)', text)
            h = re.search(r'\bheight\s*=\s*["\']([\d.]+)', text)
            if w and h:
                return int(float(w.group(1))), int(float(h.group(1)))
            vb = re.search(r'viewBox\s*=\s*["\']\s*[\d.-]+\s+[\d.-]+\s+([\d.]+)\s+([\d.]+)', text)
            if vb:
                return int(float(vb.group(1))), int(float(vb.group(2)))
        except (ValueError, UnicodeDecodeError):
            pass
        return None, None
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as img:
                return int(img.width), int(img.height)
        except Exception:
            pass
    return _dimensions_fallback(data)


def _dimensions_fallback(data: bytes) -> tuple[int | None, int | None]:
    """Minimal header readers so dimensions still work without Pillow."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return int(w), int(h)
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return int(w), int(h)
        if data[:2] == b"\xff\xd8":  # JPEG
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return int(w), int(h)
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg
    except (struct.error, IndexError):
        pass
    return None, None
