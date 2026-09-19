from __future__ import annotations

import gzip

from crawler.discovery import RobotsPolicy, maybe_gunzip, parse_sitemap, robots_url_for

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
  <url><loc>https://e.com/a</loc><lastmod>2024-01-01</lastmod>
       <image:image><image:loc>https://e.com/a.jpg</image:loc></image:image></url>
  <url><loc>https://e.com/b</loc><changefreq>daily</changefreq><priority>0.8</priority></url>
</urlset>"""

INDEX = b"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://e.com/s1.xml</loc></sitemap>
  <sitemap><loc>https://e.com/s2.xml.gz</loc></sitemap>
</sitemapindex>"""


class TestSitemaps:
    def test_urlset(self):
        result = parse_sitemap(URLSET, "https://e.com/sitemap.xml")
        assert result.kind == "urlset"
        assert result.locations == ["https://e.com/a", "https://e.com/b"]
        assert result.entries[0].lastmod == "2024-01-01"
        assert result.entries[0].images == ["https://e.com/a.jpg"]
        assert result.entries[1].changefreq == "daily"
        assert result.error is None

    def test_index(self):
        result = parse_sitemap(INDEX, "https://e.com/sitemap_index.xml")
        assert result.kind == "index"
        assert result.sitemaps == ["https://e.com/s1.xml", "https://e.com/s2.xml.gz"]
        assert result.entries == []

    def test_gzipped(self):
        result = parse_sitemap(gzip.compress(URLSET), "https://e.com/s.xml.gz")
        assert result.kind == "urlset"
        assert result.locations == ["https://e.com/a", "https://e.com/b"]

    def test_text_sitemap(self):
        data = b"https://e.com/one\nhttps://e.com/two\n\n# comment\n"
        result = parse_sitemap(data, "https://e.com/sitemap.txt")
        assert result.kind == "text"
        assert result.locations == ["https://e.com/one", "https://e.com/two"]

    def test_malformed_xml_still_yields_urls(self):
        """A broken sitemap must not cost us the URLs it does contain."""
        data = b"<urlset><url><loc>https://e.com/x</loc></url><url><loc>https://e.com/y"
        result = parse_sitemap(data, "https://e.com/bad.xml")
        assert "https://e.com/x" in result.locations
        assert result.error is not None

    def test_namespaceless_sitemap(self):
        data = b"<urlset><url><loc>https://e.com/n</loc></url></urlset>"
        assert parse_sitemap(data).locations == ["https://e.com/n"]

    def test_bom_and_whitespace(self):
        assert parse_sitemap(b"\xef\xbb\xbf\n  " + URLSET).kind == "urlset"

    def test_garbage_is_not_a_sitemap(self):
        result = parse_sitemap(b"just some words", "https://e.com/x")
        assert result.kind == "unknown"
        assert result.entries == []

    def test_empty_input(self):
        assert parse_sitemap(b"").kind == "unknown"

    def test_maybe_gunzip_passes_through_plain_data(self):
        assert maybe_gunzip(b"<xml/>") == b"<xml/>"


class TestRobots:
    BODY = """User-agent: *
Disallow: /private/
Disallow: /tmp
Allow: /
Crawl-delay: 2

User-agent: EvilBot
Disallow: /

Sitemap: https://e.com/sitemap.xml
Sitemap: https://e.com/sitemap-news.xml
"""

    def test_parses_rules_delay_and_sitemaps(self):
        policy = RobotsPolicy.parse("e.com", "https://e.com/robots.txt", 200, self.BODY)
        assert policy.can_fetch("https://e.com/public")
        assert not policy.can_fetch("https://e.com/private/x")
        assert policy.crawl_delay == 2.0
        assert policy.sitemaps == ["https://e.com/sitemap.xml",
                                   "https://e.com/sitemap-news.xml"]

    def test_missing_robots_allows_everything(self):
        policy = RobotsPolicy.parse("e.com", "https://e.com/robots.txt", 404, "")
        assert policy.can_fetch("https://e.com/anything")

    def test_unfetchable_robots_is_permissive_but_flagged(self):
        policy = RobotsPolicy.permissive("e.com", "https://e.com/robots.txt", "timeout")
        assert policy.can_fetch("https://e.com/x")
        assert policy.fetch_failed

    def test_garbage_robots_does_not_raise(self):
        policy = RobotsPolicy.parse("e.com", "https://e.com/robots.txt", 200,
                                    "\x00\xff not a robots file at all")
        assert policy.can_fetch("https://e.com/x") in (True, False)

    def test_robots_url_for(self):
        assert robots_url_for("https://e.com/deep/page?x=1") == "https://e.com/robots.txt"
        assert robots_url_for("http://e.com:8080/x") == "http://e.com:8080/robots.txt"
