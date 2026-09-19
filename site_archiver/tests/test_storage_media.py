from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import pytest

from crawler.media import best_from_srcset, extract_assets, image_dimensions, parse_srcset, urls_from_css
from crawler.parser import make_soup
from crawler.storage import (
    Storage,
    classify_asset,
    guess_extension,
    sha256_bytes,
    sha256_file,
    slugify,
)


def png(w: int, h: int, rgb=(1, 2, 3)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class TestHashing:
    def test_sha256_bytes_matches_hashlib(self):
        assert sha256_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()

    def test_sha256_file_matches_bytes(self, tmp_path):
        data = b"x" * 100_000
        path = tmp_path / "f.bin"
        path.write_bytes(data)
        assert sha256_file(path) == sha256_bytes(data)


class TestStoragePaths:
    def test_page_paths_are_deterministic(self, tmp_path):
        s = Storage(tmp_path)
        a = s.page_paths("https://e.com/a/b")
        b = s.page_paths("https://e.com/a/b")
        assert a.directory == b.directory

    def test_distinct_urls_get_distinct_dirs(self, tmp_path):
        s = Storage(tmp_path)
        assert (s.page_paths("https://e.com/a").directory
                != s.page_paths("https://e.com/b").directory)

    def test_query_string_does_not_collide(self, tmp_path):
        s = Storage(tmp_path)
        assert (s.page_paths("https://e.com/s?q=1").directory
                != s.page_paths("https://e.com/s?q=2").directory)

    def test_raw_url_never_used_as_path(self, tmp_path):
        s = Storage(tmp_path)
        paths = s.page_paths("https://e.com/a/../%2e%2e/weird?x=/../y")
        assert "://" not in str(paths.directory)
        assert ".." not in paths.directory.parts

    def test_slugify_strips_unsafe(self):
        assert "/" not in slugify("a/b")
        assert slugify("") == "index"
        assert len(slugify("x" * 500)) <= 60


class TestAssetStorage:
    def test_content_addressing_deduplicates(self, tmp_path):
        s = Storage(tmp_path)
        data = png(4, 4)
        p1, d1, new1 = s.store_asset(data, "image/png", "https://e.com/a.png")
        p2, d2, new2 = s.store_asset(data, "image/png", "https://e.com/different.png")
        assert new1 is True and new2 is False
        assert p1 == p2 and d1 == d2
        assert sum(1 for _ in tmp_path.rglob("*.png")) == 1

    def test_different_bytes_stored_separately(self, tmp_path):
        s = Storage(tmp_path)
        p1, _, _ = s.store_asset(png(4, 4), "image/png", "https://e.com/a.png")
        p2, _, _ = s.store_asset(png(5, 5), "image/png", "https://e.com/b.png")
        assert p1 != p2

    def test_media_and_documents_are_separated(self, tmp_path):
        s = Storage(tmp_path)
        video, _, _ = s.store_asset(b"\x00mp4", "video/mp4", "https://e.com/v.mp4")
        doc, _, _ = s.store_asset(b"%PDF-", "application/pdf", "https://e.com/d.pdf")
        img, _, _ = s.store_asset(png(2, 2), "image/png", "https://e.com/i.png")
        assert "media" in video.parts and "documents" in doc.parts and "assets" in img.parts

    def test_write_is_atomic_no_part_files_left(self, tmp_path):
        s = Storage(tmp_path)
        s.write_bytes(tmp_path / "x" / "y.bin", b"data")
        assert (tmp_path / "x" / "y.bin").read_bytes() == b"data"
        assert not list(tmp_path.rglob("*.part"))

    def test_gzip_roundtrip(self, tmp_path):
        s = Storage(tmp_path, compress_raw=True)
        path = s.page_paths("https://e.com/a").raw
        assert path.suffix == ".gz"
        s.write_bytes(path, b"<html>hi</html>")
        assert s.read_bytes(path) == b"<html>hi</html>"


class TestTypeDetection:
    @pytest.mark.parametrize("mime,url,expected", [
        ("image/jpeg", None, ".jpg"),
        ("image/png", None, ".png"),
        ("text/html", None, ".html"),
        (None, "https://e.com/a.WEBP", ".webp"),
        ("application/octet-stream", "https://e.com/a", ".bin"),
    ])
    def test_extension(self, mime, url, expected):
        assert guess_extension(mime, url) == expected

    @pytest.mark.parametrize("mime,url,expected", [
        ("image/png", None, "image"),
        ("video/mp4", None, "video"),
        ("audio/mpeg", None, "audio"),
        ("text/css", None, "stylesheet"),
        ("application/pdf", None, "document"),
        (None, "https://e.com/a.mp4", "video"),
        (None, "https://e.com/a.unknown", "other"),
    ])
    def test_classification(self, mime, url, expected):
        assert classify_asset(mime, url) == expected


class TestImageDimensions:
    def test_png(self):
        assert image_dimensions(png(120, 45)) == (120, 45)

    def test_gif(self):
        gif = b"GIF89a" + struct.pack("<HH", 30, 20) + b"\x00" * 10
        assert image_dimensions(gif) == (30, 20)

    def test_svg_viewbox(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"/>'
        assert image_dimensions(svg) == (100, 50)

    def test_garbage_returns_none(self):
        assert image_dimensions(b"not an image") == (None, None)

    def test_empty_returns_none(self):
        assert image_dimensions(b"") == (None, None)


class TestAssetExtraction:
    MARKUP = """<html><head>
    <link rel="stylesheet" href="/s.css"><link rel="icon" href="/f.ico">
    <meta property="og:image" content="https://cdn.e.com/og.png">
    </head><body>
    <img src="/small.jpg" srcset="/a-320.jpg 320w, /a-1280.jpg 1280w, /a-640.jpg 640w" alt="A">
    <img data-src="/lazy.jpg" data-original="/lazy2.jpg" alt="Lazy">
    <picture><source srcset="/p-2x.webp 2x, /p-1x.webp 1x"><img src="/p.jpg"></picture>
    <video poster="/poster.jpg"><source src="/v.mp4" type="video/mp4"></video>
    <audio src="/a.mp3"></audio>
    <div style="background-image:url('/bg.png')"></div>
    <a href="/docs/r.pdf">report</a><a href="/page">page</a>
    </body></html>"""

    def setup_method(self):
        soup, _ = make_soup(self.MARKUP)
        self.refs = extract_assets(soup, "https://e.com/x/")
        self.urls = {r.url for r in self.refs}

    def test_picks_highest_resolution_from_srcset(self):
        assert "https://e.com/a-1280.jpg" in self.urls
        assert "https://e.com/a-320.jpg" not in self.urls

    def test_picks_highest_density_from_picture(self):
        assert "https://e.com/p-2x.webp" in self.urls

    def test_lazy_attributes(self):
        assert {"https://e.com/lazy.jpg", "https://e.com/lazy2.jpg"} <= self.urls

    def test_media_poster_and_sources(self):
        assert {"https://e.com/poster.jpg", "https://e.com/v.mp4",
                "https://e.com/a.mp3"} <= self.urls

    def test_stylesheet_icon_and_og_image(self):
        assert {"https://e.com/s.css", "https://e.com/f.ico",
                "https://cdn.e.com/og.png"} <= self.urls

    def test_inline_css_url(self):
        assert "https://e.com/bg.png" in self.urls

    def test_document_link_captured_but_not_page_link(self):
        assert "https://e.com/docs/r.pdf" in self.urls
        assert "https://e.com/page" not in self.urls

    def test_data_uris_ignored(self):
        soup, _ = make_soup('<img src="data:image/gif;base64,R0lGOD">')
        assert extract_assets(soup, "https://e.com/") == []

    def test_alt_text_recorded(self):
        alts = {r.alt_text for r in self.refs if r.alt_text}
        assert "A" in alts

    def test_srcset_parsing(self):
        assert parse_srcset("/a.jpg 1x, /b.jpg 2x") == [("/a.jpg", 1.0, "x"),
                                                        ("/b.jpg", 2.0, "x")]
        assert best_from_srcset("") == (None, None)
        assert best_from_srcset("/only.jpg")[0] == "/only.jpg"

    def test_urls_from_css(self):
        css = "body{background:url('/a.png')} @font-face{src:url(/f.woff2)}"
        found = urls_from_css(css, "https://e.com/css/main.css")
        assert "https://e.com/a.png" in found
        assert "https://e.com/f.woff2" in found
