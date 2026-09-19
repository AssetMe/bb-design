from __future__ import annotations

import json

from crawler.parser import (
    classify_page,
    comments_from_structured,
    detect_load_more,
    extract_comments,
    make_soup,
    needs_rendering,
    parse_html,
)

PAGE = """<!doctype html>
<html lang="en"><head>
<title>Widget</title>
<meta name="description" content="A widget">
<meta property="og:title" content="Widget OG">
<meta property="og:image" content="/og.png">
<meta name="twitter:card" content="summary">
<link rel="canonical" href="https://e.com/widget">
<link rel="next" href="/widget?page=2">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Widget",
 "review":[{"@type":"Review","author":{"@type":"Person","name":"Zoe","url":"https://e.com/u/zoe"},
            "datePublished":"2024-05-05","reviewBody":"Solid.",
            "reviewRating":{"@type":"Rating","ratingValue":"5"}}]}
</script>
<script type="application/ld+json">{ broken json here </script>
<script id="__NEXT_DATA__" type="application/json">{"props":{"id":42}}</script>
</head><body itemscope itemtype="https://schema.org/Product">
<h1 itemprop="name">Widget</h1>
<p itemprop="description">A dependable widget with a long description so the
   text extractor has something substantial to work with on this page.</p>
<a href="/other">Other</a>
<a href="/widget?page=3" class="pagination">3</a>
<a href="mailto:x@y.z">mail</a>
<ul class="comment-list">
  <li class="comment" data-comment-id="c1">
    <span class="author"><a href="/u/ann">Ann</a></span>
    <time datetime="2024-01-02">Jan 2</time>
    <span class="rating" data-rating="4">4/5</span>
    <div class="comment-text">Works well.</div>
    <div class="replies">
      <div class="comment" data-comment-id="c1r1">
        <span class="author">Shop</span><time datetime="2024-01-03">Jan 3</time>
        <div class="comment-text">Glad to hear it.</div>
      </div>
    </div>
  </li>
  <li class="comment" data-comment-id="c2">
    <span class="author">Bo</span>
    <div class="comment-text">Second.</div>
  </li>
</ul>
<button class="load-more">Load more</button>
</body></html>"""


class TestParseHtml:
    def setup_method(self):
        self.r = parse_html(PAGE, "https://e.com/widget")

    def test_title_lang_canonical(self):
        assert self.r.title == "Widget"
        assert self.r.lang == "en"
        assert self.r.canonical == "https://e.com/widget"

    def test_meta(self):
        assert self.r.meta["og:title"] == "Widget OG"
        assert self.r.meta["description"] == "A widget"
        assert self.r.meta["twitter:card"] == "summary"

    def test_links_resolved_and_filtered(self):
        urls = {l.url for l in self.r.links}
        assert "https://e.com/other" in urls
        assert not any(u.startswith("mailto:") for u in urls)

    def test_pagination_from_rel_and_class(self):
        pag = {l.url for l in self.r.pagination}
        assert "https://e.com/widget?page=2" in pag
        assert "https://e.com/widget?page=3" in pag

    def test_structured_data_kinds(self):
        kinds = {s.kind for s in self.r.structured}
        assert {"jsonld", "opengraph", "twitter", "meta", "microdata",
                "embedded_json"} <= kinds

    def test_malformed_jsonld_preserved_with_error(self):
        bad = [s for s in self.r.structured if s.kind == "jsonld" and s.parse_error]
        assert len(bad) == 1
        assert "broken json here" in bad[0].raw

    def test_embedded_state_parsed(self):
        nxt = [s for s in self.r.structured
               if s.kind == "embedded_json" and s.subtype == "__NEXT_DATA__"]
        assert nxt and nxt[0].parsed["props"]["id"] == 42

    def test_comments_with_replies_and_ratings(self):
        html_comments = [c for c in self.r.comments if c.source == "html"]
        assert len(html_comments) == 2
        first = html_comments[0]
        assert first.comment_key == "c1"
        assert first.author == "Ann"
        assert first.author_url == "https://e.com/u/ann"
        assert first.published == "2024-01-02"
        assert first.rating == "4"
        assert first.body == "Works well."
        assert len(first.replies) == 1
        assert first.replies[0].author == "Shop"
        assert first.replies[0].body == "Glad to hear it."

    def test_jsonld_comments_merged_not_duplicated(self):
        authors = [c.author for c in self.r.comments]
        assert "Zoe" in authors          # only present in JSON-LD
        assert authors.count("Ann") == 1  # not double counted

    def test_text_extraction_keeps_structure_drops_scripts(self):
        assert "Widget" in self.r.text
        assert "dependable widget" in self.r.text
        assert "@context" not in self.r.text
        assert "broken json" not in self.r.text

    def test_load_more_detected(self):
        assert self.r.has_load_more is True

    def test_no_problems_on_valid_page(self):
        assert self.r.problems == []


class TestResilience:
    def test_malformed_html_still_parses(self):
        bad = ("<html><head><title>Bad</title><body><h1>Head<p>text with <b>unclosed"
               '<a href="/x">link</a><ul><li>one<li>two'
               '<div class="comment"><span class="author">A</span>'
               '<div class="comment-text">c</div></div>')
        r = parse_html(bad, "https://e.com/bad")
        assert r.title == "Bad"
        assert any(l.url == "https://e.com/x" for l in r.links)
        assert len(r.comments) == 1

    def test_empty_and_garbage_input_never_raise(self):
        for markup in ("", "   ", "not html at all", "<<<>>>", "\x00\x01\x02"):
            r = parse_html(markup, "https://e.com/x")
            assert isinstance(r.text, str)

    def test_base_tag_is_honoured(self):
        markup = '<html><head><base href="https://cdn.e.com/v2/"></head><body><a href="x">x</a></body></html>'
        r = parse_html(markup, "https://e.com/page")
        assert any(l.url == "https://cdn.e.com/v2/x" for l in r.links)


class TestRenderDecision:
    def test_empty_shell_triggers_render(self):
        r = parse_html('<html><body><div id="root"></div><script src="/a.js"></script></body></html>',
                       "https://e.com/")
        assert needs_rendering(r, 200) == "empty_shell"

    def test_load_more_triggers_render(self):
        assert needs_rendering(parse_html(PAGE, "https://e.com/widget"), len(PAGE)) \
            == "load_more_control"

    def test_full_static_page_does_not_render(self):
        markup = ("<html><body>" + "<p>Plenty of real server-rendered text here. </p>" * 40 +
                  '<a href="/a">a</a></body></html>')
        r = parse_html(markup, "https://e.com/x")
        assert needs_rendering(r, len(markup)) is None


class TestClassification:
    def _classify(self, url, markup):
        r = parse_html(markup, url)
        soup, _ = make_soup(markup)
        return classify_page(url, soup, r.structured, r.text)

    def test_home(self):
        assert self._classify("https://e.com/", "<html><body><h1>Home</h1></body></html>") == "HOME"

    def test_product_detail_from_jsonld(self):
        markup = ('<html><body><script type="application/ld+json">'
                  '{"@type":"Product","name":"x"}</script><p>text</p></body></html>')
        assert self._classify("https://e.com/item/1", markup) == "DETAIL"

    def test_category_from_path(self):
        assert self._classify("https://e.com/category/kitchen",
                              "<html><body><p>x</p></body></html>") == "CATEGORY"

    def test_listing_from_pagination_url(self):
        assert self._classify("https://e.com/listing?page=2",
                              "<html><body><p>x</p></body></html>") == "LISTING"
