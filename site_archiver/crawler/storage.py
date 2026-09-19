"""Deterministic, collision-free on-disk layout for the archive.

Raw URLs are never used as filesystem paths.  Pages get a stable directory
derived from a hash of their normalized URL plus a readable slug; assets are
content-addressed by SHA-256 so identical bytes are stored exactly once.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import mimetypes
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

mimetypes.add_type("application/json", ".json")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("font/woff2", ".woff2")

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_MAX_SLUG = 60

EXT_BY_MIME = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "application/json": ".json",
    "application/ld+json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/plain": ".txt",
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "application/pdf": ".pdf",
    "font/woff2": ".woff2",
    "font/woff": ".woff",
}

ASSET_TYPE_BY_PREFIX = {
    "image/": "image",
    "video/": "video",
    "audio/": "audio",
    "font/": "font",
}
ASSET_TYPE_BY_MIME = {
    "text/css": "stylesheet",
    "application/javascript": "script",
    "text/javascript": "script",
    "application/pdf": "document",
    "application/json": "data",
    "application/xml": "data",
    "text/xml": "data",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def slugify(value: str, fallback: str = "index") -> str:
    value = unquote(value or "")
    value = _SLUG_RE.sub("-", value).strip("-.")
    value = re.sub(r"-{2,}", "-", value)
    if not value:
        return fallback
    return value[:_MAX_SLUG]


def guess_extension(mime: str | None, url: str | None = None) -> str:
    if mime:
        base = mime.split(";")[0].strip().lower()
        if base in EXT_BY_MIME:
            return EXT_BY_MIME[base]
        guessed = mimetypes.guess_extension(base)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    if url:
        ext = Path(urlsplit(url).path).suffix.lower()
        if ext and len(ext) <= 6 and re.fullmatch(r"\.[a-z0-9]+", ext):
            return ext
    return ".bin"


def classify_asset(mime: str | None, url: str | None = None) -> str:
    base = (mime or "").split(";")[0].strip().lower()
    for prefix, kind in ASSET_TYPE_BY_PREFIX.items():
        if base.startswith(prefix):
            return kind
    if base in ASSET_TYPE_BY_MIME:
        return ASSET_TYPE_BY_MIME[base]
    ext = Path(urlsplit(url or "").path).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".ico", ".bmp"):
        return "image"
    if ext in (".mp4", ".webm", ".mov", ".m4v"):
        return "video"
    if ext in (".mp3", ".ogg", ".wav", ".m4a", ".flac"):
        return "audio"
    if ext in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".zip"):
        return "document"
    if ext == ".css":
        return "stylesheet"
    if ext == ".js":
        return "script"
    return "other"


@dataclass(slots=True)
class PagePaths:
    directory: Path
    raw: Path
    rendered: Path
    text: Path
    metadata: Path
    structured: Path
    comments_json: Path
    comments_text: Path
    comments_raw: Path

    def relative_to(self, root: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in self.__slots__:  # type: ignore[attr-defined]
            value: Path = getattr(self, name)
            try:
                out[name] = str(value.relative_to(root))
            except ValueError:
                out[name] = str(value)
        return out


class Storage:
    """Owns everything written under ``archive/``."""

    def __init__(self, root: Path, compress_raw: bool = False):
        self.root = Path(root)
        self.compress_raw = compress_raw
        self.pages_dir = self.root / "pages"
        self.assets_dir = self.root / "assets"
        self.media_dir = self.root / "media"
        self.documents_dir = self.root / "documents"
        self.site_dir = self.root / "site"
        for d in (self.pages_dir, self.assets_dir, self.media_dir,
                  self.documents_dir, self.site_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- pages ----------------------------------------------------------
    def page_key(self, normalized_url: str) -> str:
        return hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()

    def page_paths(self, normalized_url: str) -> PagePaths:
        key = self.page_key(normalized_url)
        parts = urlsplit(normalized_url)
        stem = Path(parts.path).name or parts.path.strip("/").replace("/", "-") or "index"
        if parts.query:
            stem = f"{stem}-q{hashlib.sha1(parts.query.encode()).hexdigest()[:6]}"
        slug = slugify(stem)
        directory = self.pages_dir / key[:2] / f"{key[2:12]}_{slug}"
        return PagePaths(
            directory=directory,
            raw=directory / ("raw.html.gz" if self.compress_raw else "raw.html"),
            rendered=directory / "rendered.html",
            text=directory / "text.txt",
            metadata=directory / "metadata.json",
            structured=directory / "structured.json",
            comments_json=directory / "comments.json",
            comments_text=directory / "comments.txt",
            comments_raw=directory / "comments_raw.html",
        )

    def write_bytes(self, path: Path, data: bytes) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        if path.suffix == ".gz":
            with gzip.open(tmp, "wb") as fh:
                fh.write(data)
        else:
            with open(tmp, "wb") as fh:
                fh.write(data)
        os.replace(tmp, path)
        return len(data)

    def write_text(self, path: Path, text: str) -> int:
        return self.write_bytes(path, text.encode("utf-8", errors="replace"))

    def write_json(self, path: Path, payload: Any) -> int:
        return self.write_text(
            path, json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        )

    def read_bytes(self, path: Path) -> bytes:
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as fh:
                return fh.read()
        return path.read_bytes()

    # -- assets ---------------------------------------------------------
    def asset_path(self, digest: str, mime: str | None, url: str | None,
                   asset_type: str | None = None) -> Path:
        ext = guess_extension(mime, url)
        kind = asset_type or classify_asset(mime, url)
        base = {
            "video": self.media_dir,
            "audio": self.media_dir,
            "document": self.documents_dir,
        }.get(kind, self.assets_dir)
        return base / digest[:2] / digest[2:4] / f"{digest}{ext}"

    def store_asset(self, data: bytes, mime: str | None, url: str | None,
                    asset_type: str | None = None) -> tuple[Path, str, bool]:
        """Content-address ``data``.  Returns (path, sha256, was_new)."""
        digest = sha256_bytes(data)
        path = self.asset_path(digest, mime, url, asset_type)
        if path.exists() and path.stat().st_size == len(data):
            return path, digest, False
        self.write_bytes(path, data)
        return path, digest, True

    # -- site-level files -----------------------------------------------
    def store_site_file(self, name: str, data: bytes) -> Path:
        path = self.site_dir / slugify(name, "file")
        self.write_bytes(path, data)
        return path

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def disk_usage(self) -> int:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return total

    def free_space(self) -> int:
        return shutil.disk_usage(self.root).free
