from __future__ import annotations

import pytest

from crawler.urlnorm import Scope, UrlError, normalize, registrable_suffix, try_normalize


class TestNormalize:
    @pytest.mark.parametrize("raw,expected", [
        # scheme/host casing and default ports
        ("HTTP://Example.COM/a", "http://example.com/a"),
        ("https://Example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        # empty path becomes root
        ("https://example.com", "https://example.com/"),
        # fragments are dropped
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a#", "https://example.com/a"),
        # dot segments resolve
        ("https://example.com/a/./b/../c", "https://example.com/a/c"),
        ("https://example.com/a/../../b", "https://example.com/b"),
        # trailing slashes are preserved (they can be distinct resources)
        ("https://example.com/a/", "https://example.com/a/"),
        ("https://example.com/a", "https://example.com/a"),
        # redundant percent-encoding of unreserved chars is decoded
        ("https://example.com/%7Euser", "https://example.com/~user"),
        ("https://example.com/a%2Db", "https://example.com/a-b"),
        # trailing dot on host
        ("https://example.com./a", "https://example.com/a"),
        # session ids in the path
        ("https://example.com/a;jsessionid=ABC123", "https://example.com/a"),
    ])
    def test_basic(self, raw, expected):
        assert normalize(raw) == expected

    def test_tracking_params_removed_content_params_kept(self):
        assert normalize("https://e.com/x?utm_source=a&id=7&fbclid=z") == "https://e.com/x?id=7"
        # A query that is entirely tracking collapses to no query at all.
        assert normalize("https://e.com/x?utm_source=a") == "https://e.com/x"
        # Content-selecting params are never dropped.
        assert normalize("https://e.com/s?q=lamp&page=2") == "https://e.com/s?page=2&q=lamp"

    def test_query_params_sorted_and_deduplicated(self):
        assert normalize("https://e.com/x?b=2&a=1") == "https://e.com/x?a=1&b=2"
        # exact repeats collapse
        assert normalize("https://e.com/x?a=1&a=1") == "https://e.com/x?a=1"
        # genuine multi-values survive
        assert normalize("https://e.com/x?a=1&a=2") == "https://e.com/x?a=1&a=2"

    def test_relative_resolution(self):
        base = "https://e.com/dir/page.html"
        assert normalize("sub/x", base) == "https://e.com/dir/sub/x"
        assert normalize("/root", base) == "https://e.com/root"
        assert normalize("../up", base) == "https://e.com/up"
        assert normalize("//cdn.e.com/a", base) == "https://cdn.e.com/a"

    def test_ipv6_and_idna(self):
        assert normalize("http://[2001:DB8::1]/x") == "http://[2001:db8::1]/x"
        assert normalize("https://bücher.de/x").startswith("https://xn--bcher-kva.de/")

    @pytest.mark.parametrize("raw", [
        "mailto:a@b.c", "javascript:void(0)", "data:text/plain,hi",
        "tel:+123", "", "   ", "ftp://e.com/x",
    ])
    def test_rejects_non_archivable(self, raw):
        with pytest.raises((UrlError, ValueError)):
            normalize(raw)
        assert try_normalize(raw) is None

    def test_strip_index_is_opt_in(self):
        url = "https://e.com/dir/index.html"
        assert normalize(url) == url
        assert normalize(url, strip_index=True) == "https://e.com/dir/"

    def test_idempotent(self):
        once = normalize("HTTP://E.com:80/a/./b?utm_source=x&z=1&a=2#f")
        assert normalize(once) == once


class TestScope:
    def test_from_roots_pairs_www_and_apex(self):
        scope = Scope.from_roots(["https://example.com/"])
        assert scope.host_allowed("example.com")
        assert scope.host_allowed("www.example.com")

    def test_subdomains_toggle(self):
        inc = Scope.from_roots(["https://example.com/"], include_subdomains=True)
        exc = Scope.from_roots(["https://example.com/"], include_subdomains=False)
        assert inc.allows("https://cdn.example.com/a")
        assert not exc.allows("https://cdn.example.com/a")
        assert not inc.allows("https://elsewhere.org/a")

    def test_deny_beats_allow(self):
        scope = Scope.from_roots(["https://e.com/"], allow_patterns=[r"/keep/"],
                                 deny_patterns=[r"/keep/secret"])
        assert scope.allows("https://e.com/keep/page")
        assert not scope.allows("https://e.com/keep/secret")
        assert not scope.allows("https://e.com/other")

    def test_registrable_suffix(self):
        assert registrable_suffix("a.b.example.com") == "example.com"
        assert registrable_suffix("shop.example.co.uk") == "example.co.uk"
        assert registrable_suffix("example.com") == "example.com"
