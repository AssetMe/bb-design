#!/usr/bin/env python3
"""Generate the fixture site used to test the crawler end to end.

Deliberately includes the awkward cases: gzipped sitemaps, a sitemap index,
pagination, canonical duplicates, lazy images, srcset, client-side rendering,
XHR-loaded comments, load-more, infinite scroll, malformed HTML, redirects,
broken links, and a restricted area that robots.txt also disallows.
"""
from __future__ import annotations

import gzip
import json
import shutil
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "site"
BASE = "http://127.0.0.1:8899"

ITEMS = [
    {"id": 1,  "name": "Blue Harbour Lamp",   "city": "Porto",     "price": 49.0,  "layout": "a", "tags": ["lighting", "coastal"]},
    {"id": 2,  "name": "Copper Kettle",        "city": "Leeds",     "price": 32.5,  "layout": "a", "tags": ["kitchen"]},
    {"id": 3,  "name": "Walnut Stool",         "city": "Aarhus",    "price": 88.0,  "layout": "b", "tags": ["furniture", "wood"]},
    {"id": 4,  "name": "Linen Throw",          "city": "Vilnius",   "price": 24.0,  "layout": "a", "tags": ["textile"]},
    {"id": 5,  "name": "Slate Coaster Set",    "city": "Cardiff",   "price": 15.0,  "layout": "b", "tags": ["kitchen", "stone"]},
    {"id": 6,  "name": "Brass Compass",        "city": "Bergen",    "price": 61.0,  "layout": "a", "tags": ["travel"]},
    {"id": 7,  "name": "Canvas Satchel",       "city": "Ghent",     "price": 74.5,  "layout": "b", "tags": ["bags"]},
    {"id": 8,  "name": "Ceramic Planter",      "city": "Valencia",  "price": 28.0,  "layout": "a", "tags": ["garden"]},
    {"id": 9,  "name": "Oak Cutting Board",    "city": "Tampere",   "price": 41.0,  "layout": "b", "tags": ["kitchen", "wood"]},
    {"id": 10, "name": "Wool Scarf",           "city": "Galway",    "price": 36.0,  "layout": "a", "tags": ["textile"]},
    {"id": 11, "name": "Glass Carafe",         "city": "Trieste",   "price": 22.0,  "layout": "b", "tags": ["kitchen"]},
    {"id": 12, "name": "Iron Bookend",         "city": "Kaunas",    "price": 19.0,  "layout": "a", "tags": ["office"]},
]

COMMENTS = {
    1: [
        {"id": "c-1-1", "author": "Mira Solberg", "author_url": "/user/mira", "date": "2024-02-11", "rating": "5", "text": "Warm light, solid base. Shipped quickly.", "replies": [{"id": "c-1-1-r1", "author": "Shop", "date": "2024-02-12", "text": "Thanks Mira!"}]},
        {"id": "c-1-2", "author": "Tomas Alves", "author_url": "/user/tomas", "date": "2024-03-02", "rating": "4", "text": "Cable is shorter than I expected but the finish is lovely."},
    ],
    3: [
        {"id": "c-3-1", "author": "Ines Duarte", "author_url": "/user/ines", "date": "2024-01-19", "rating": "5", "text": "Perfect height for our kitchen counter."},
    ],
    7: [
        {"id": "c-7-1", "author": "Ola Berg", "date": "2024-04-05", "rating": "3", "text": "Good bag, the strap hardware feels light."},
        {"id": "c-7-2", "author": "Rui Costa", "author_url": "/user/rui", "date": "2024-04-09", "rating": "5", "text": "Used it daily for a month, holding up well."},
    ],
}

# Comments served only via XHR (the "load more" pages).
XHR_COMMENTS = {
    1: [
        {"id": "x-1-1", "author": "Katrin Vogel", "date": "2024-05-01", "rating": "4", "text": "Second page of reviews, loaded by JavaScript."},
        {"id": "x-1-2", "author": "Piotr Nowak", "date": "2024-05-04", "rating": "5", "text": "Only visible after clicking load more."},
        {"id": "x-1-3", "author": "Sofia Renna", "date": "2024-05-06", "rating": "5", "text": "Third XHR comment, deliberately past the initial page."},
    ]
}


# ----------------------------------------------------------------------
def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A minimal valid PNG without needing an image library."""
    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(rgb) * width
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def minimal_pdf(text: str) -> bytes:
    body = (f"BT /F1 18 Tf 60 700 Td ({text}) Tj ET").encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n"
            f"{xref}\n%%EOF\n").encode()
    return bytes(out)


def write(rel: str, data: bytes | str) -> Path:
    path = ROOT / rel.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


# ----------------------------------------------------------------------
def head(title: str, description: str, canonical: str | None = None,
         extra: str = "", image: str | None = None) -> str:
    og_image = image or "/assets/img/og-default.png"
    canon = f'\n  <link rel="canonical" href="{BASE}{canonical}">' if canonical else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <meta name="description" content="{description}">
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{description}">
  <meta property="og:type" content="website">
  <meta property="og:image" content="{BASE}{og_image}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{title}">
  <link rel="stylesheet" href="/assets/style.css">
  <link rel="icon" href="/assets/img/favicon.png">{canon}{extra}
</head>
<body>
<header class="site-header">
  <a href="/" class="logo">Fixture Shop</a>
  <nav>
    <a href="/listing/">Catalogue</a>
    <a href="/about">About</a>
    <a href="/category/kitchen">Kitchen</a>
    <a href="/post/js-rendered">Journal</a>
  </nav>
</header>
<main>
"""


FOOT = """</main>
<footer class="site-footer">
  <p>Fixture site for archival testing. <a href="/about">About</a> ·
     <a href="/private/secret">Members area</a> ·
     <a href="/docs/report.pdf">Annual report (PDF)</a></p>
</footer>
</body>
</html>
"""


def item_jsonld(item: dict) -> str:
    comments = COMMENTS.get(item["id"], [])
    payload = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": item["name"],
        "sku": f"SKU-{item['id']:04d}",
        "url": f"{BASE}/item/{item['id']}",
        "image": [f"{BASE}/assets/img/item-{item['id']}.png"],
        "description": f"{item['name']} from our {item['city']} workshop.",
        "offers": {"@type": "Offer", "price": item["price"], "priceCurrency": "EUR",
                   "availability": "https://schema.org/InStock"},
        "keywords": item["tags"],
    }
    if comments:
        payload["review"] = [
            {"@type": "Review", "author": {"@type": "Person", "name": c["author"],
                                           "url": BASE + c.get("author_url", "")} if c.get("author_url")
                                 else {"@type": "Person", "name": c["author"]},
             "datePublished": c["date"], "reviewBody": c["text"],
             "reviewRating": {"@type": "Rating", "ratingValue": c.get("rating")}}
            for c in comments
        ]
        payload["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": round(sum(float(c.get("rating", 0)) for c in comments) / len(comments), 1),
            "reviewCount": len(comments),
        }
    return json.dumps(payload, indent=2)


def comment_html(c: dict, layout: str) -> str:
    author = (f'<a href="{c["author_url"]}">{c["author"]}</a>'
              if c.get("author_url") else c["author"])
    rating = (f'<span class="rating" data-rating="{c["rating"]}">{c["rating"]}/5</span>'
              if c.get("rating") else "")
    replies = ""
    if c.get("replies"):
        inner = "".join(
            f'<div class="comment" data-comment-id="{r["id"]}">'
            f'<span class="author">{r["author"]}</span>'
            f'<time datetime="{r["date"]}">{r["date"]}</time>'
            f'<div class="comment-text">{r["text"]}</div></div>'
            for r in c["replies"]
        )
        replies = f'<div class="replies">{inner}</div>'
    if layout == "b":
        return (f'<article class="review" data-review-id="{c["id"]}">'
                f'<header><span class="review-author">{author}</span>'
                f'<time class="date" datetime="{c["date"]}">{c["date"]}</time>{rating}</header>'
                f'<div class="review-text">{c["text"]}</div>{replies}</article>')
    return (f'<li class="comment" data-comment-id="{c["id"]}">'
            f'<span class="author">{author}</span>'
            f'<time datetime="{c["date"]}">{c["date"]}</time>{rating}'
            f'<div class="comment-text">{c["text"]}</div>{replies}</li>')


def build_item(item: dict) -> None:
    iid = item["id"]
    comments = COMMENTS.get(iid, [])
    layout = item["layout"]
    img = f"/assets/img/item-{iid}.png"
    gallery = "".join(
        f'<img class="gallery-img" src="{img}" '
        f'srcset="/assets/img/item-{iid}-480.png 480w, /assets/img/item-{iid}.png 1200w" '
        f'sizes="(max-width:600px) 480px, 1200px" alt="{item["name"]} view {n}">'
        if n == 1 else
        f'<img class="gallery-img lazy" data-src="/assets/img/item-{iid}-alt{n}.png" '
        f'data-original="/assets/img/item-{iid}-alt{n}.png" '
        f'src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==" '
        f'alt="{item["name"]} detail {n}">'
        for n in (1, 2, 3)
    )
    if layout == "b":
        comment_block = ("".join(comment_html(c, "b") for c in comments)
                         if comments else '<p class="no-reviews">No reviews yet.</p>')
        comment_wrap = f'<section id="reviews" class="reviews">{comment_block}</section>'
        fields = (
            f'<dl class="specs"><dt>Origin</dt><dd itemprop="city">{item["city"]}</dd>'
            f'<dt>Price</dt><dd>€{item["price"]:.2f}</dd>'
            f'<dt>Tags</dt><dd>{", ".join(item["tags"])}</dd></dl>'
        )
    else:
        comment_block = ("".join(comment_html(c, "a") for c in comments)
                         if comments else '<li class="no-comments">No comments yet.</li>')
        comment_wrap = (f'<section id="comments"><h2>Comments</h2>'
                        f'<ul class="comment-list">{comment_block}</ul></section>')
        fields = (
            f'<table class="specs"><tr><th>Origin</th><td>{item["city"]}</td></tr>'
            f'<tr><th>Price</th><td>€{item["price"]:.2f}</td></tr>'
            f'<tr><th>Tags</th><td>{", ".join(item["tags"])}</td></tr></table>'
        )

    # Item 1 additionally loads more comments over XHR behind a button.
    load_more = ""
    if iid in XHR_COMMENTS:
        load_more = f"""
  <button class="load-more" data-load-more data-item="{iid}" data-page="2">Load more reviews</button>
  <script src="/assets/comments.js" defer></script>"""

    related = [i for i in ITEMS if i["id"] != iid and set(i["tags"]) & set(item["tags"])][:3]
    related_html = "".join(
        f'<li><a href="/item/{r["id"]}">{r["name"]}</a></li>' for r in related)

    extra = f'\n  <script type="application/ld+json">\n{item_jsonld(item)}\n  </script>'
    body = f"""{head(item['name'] + " | Fixture Shop", f"{item['name']} from {item['city']}",
                     canonical=f"/item/{iid}", extra=extra, image=img)}
<article class="item" itemscope itemtype="https://schema.org/Product">
  <h1 itemprop="name">{item['name']}</h1>
  <p class="lede" itemprop="description">Hand finished in {item['city']}.
     A durable everyday piece with a {item['tags'][0]} focus.</p>
  <div class="gallery">{gallery}</div>
  {fields}
  <p><a href="/listing/">Back to the catalogue</a> ·
     <a href="/category/{item['tags'][0]}">More {item['tags'][0]}</a></p>
  <h2>Related</h2>
  <ul class="related">{related_html}</ul>
</article>
{comment_wrap}{load_more}
{FOOT}"""
    write(f"item/{iid}/index.html", body)


def build_listing() -> None:
    per_page = 4
    pages = [ITEMS[i:i + per_page] for i in range(0, len(ITEMS), per_page)]
    for n, chunk in enumerate(pages, 1):
        path = "listing/index.html" if n == 1 else f"listing/page/{n}/index.html"
        url = "/listing/" if n == 1 else f"/listing/page/{n}"
        rel = ""
        if n < len(pages):
            rel += f'\n  <link rel="next" href="{BASE}/listing/page/{n + 1}">'
        if n > 1:
            prev = "/listing/" if n == 2 else f"/listing/page/{n - 1}"
            rel += f'\n  <link rel="prev" href="{BASE}{prev}">'
        cards = "".join(
            f'<li class="card"><a href="/item/{i["id"]}">'
            f'<img src="/assets/img/item-{i["id"]}-480.png" '
            f'srcset="/assets/img/item-{i["id"]}-480.png 480w, /assets/img/item-{i["id"]}.png 1200w" '
            f'alt="{i["name"]}" loading="lazy">'
            f'<span class="card-title">{i["name"]}</span>'
            f'<span class="card-city">{i["city"]}</span></a></li>'
            for i in chunk
        )
        nav = '<nav class="pagination">'
        for p in range(1, len(pages) + 1):
            target = "/listing/" if p == 1 else f"/listing/page/{p}"
            nav += (f'<span class="current">{p}</span>' if p == n
                    else f'<a href="{target}">{p}</a>')
        if n < len(pages):
            nav += f'<a rel="next" class="next" href="/listing/page/{n + 1}">Next →</a>'
        nav += "</nav>"
        jsonld = json.dumps({
            "@context": "https://schema.org", "@type": "CollectionPage",
            "name": f"Catalogue page {n}", "url": BASE + url,
            "mainEntity": {"@type": "ItemList", "itemListElement": [
                {"@type": "ListItem", "position": k + 1,
                 "url": f"{BASE}/item/{i['id']}", "name": i["name"]}
                for k, i in enumerate(chunk)]},
        }, indent=2)
        extra = rel + f'\n  <script type="application/ld+json">\n{jsonld}\n  </script>'
        body = f"""{head(f"Catalogue - page {n}", f"Catalogue page {n} of {len(pages)}",
                         canonical=url, extra=extra)}
<h1>Catalogue</h1>
<p>Page {n} of {len(pages)}.</p>
<ul class="cards">{cards}</ul>
{nav}
{FOOT}"""
        write(path, body)


def build_category() -> None:
    tags: dict[str, list[dict]] = {}
    for item in ITEMS:
        for tag in item["tags"]:
            tags.setdefault(tag, []).append(item)
    for tag, items in tags.items():
        links = "".join(f'<li><a href="/item/{i["id"]}">{i["name"]}</a> '
                        f'<span class="city">{i["city"]}</span></li>' for i in items)
        body = f"""{head(f"{tag.title()} | Fixture Shop", f"Everything tagged {tag}",
                         canonical=f"/category/{tag}")}
<h1>{tag.title()}</h1>
<ul class="cards">{links}</ul>
<p><a href="/listing/">All items</a></p>
{FOOT}"""
        write(f"category/{tag}/index.html", body)


def build_misc() -> None:
    # Home
    featured = "".join(
        f'<li><a href="/item/{i["id"]}">'
        f'<img src="/assets/img/item-{i["id"]}-480.png" alt="{i["name"]}">'
        f'{i["name"]}</a></li>' for i in ITEMS[:4])
    jsonld = json.dumps({
        "@context": "https://schema.org", "@type": "WebSite", "name": "Fixture Shop",
        "url": BASE + "/",
        "potentialAction": {"@type": "SearchAction",
                            "target": f"{BASE}/search?q={{search_term_string}}",
                            "query-input": "required name=search_term_string"},
    }, indent=2)
    home_extra = "\n  <script type=\"application/ld+json\">\n" + jsonld + "\n  </script>"
    home_head = head("Fixture Shop", "A fixture site for archival testing",
                     canonical="/", extra=home_extra)
    write("index.html", f"""{home_head}
<h1>Fixture Shop</h1>
<p>A small catalogue used to exercise an archival crawler.</p>
<ul class="cards">{featured}</ul>
<p>
  <a href="/listing/">Browse the catalogue</a> ·
  <a href="/post/js-rendered">Journal (client rendered)</a> ·
  <a href="/post/infinite">Infinite scroll demo</a> ·
  <a href="/post/malformed">Malformed page</a> ·
  <a href="/dup-of-item-1">Duplicate of item 1</a> ·
  <a href="/redirect-me">Redirect</a> ·
  <a href="/broken-link">Broken link</a> ·
  <a href="/flaky">Flaky endpoint</a> ·
  <a href="/rate-limited">Rate limited endpoint</a> ·
  <a href="/listing/?utm_source=newsletter&utm_medium=email">Tracked catalogue link</a> ·
  <a href="/listing/#top">Catalogue with fragment</a> ·
  <a href="/search?q=lamp">Search</a>
</p>
{FOOT}""")

    write("about/index.html", f"""{head("About | Fixture Shop", "About this fixture site",
                                        canonical="/about")}
<h1>About</h1>
<p>This site exists purely to test a website archival crawler. Every page here
   is synthetic. It includes deliberately awkward cases: gzipped sitemaps,
   client-side rendering, lazy images, pagination, redirects and malformed HTML.</p>
<p>Media: <a href="/docs/report.pdf">annual report (PDF)</a>,
   <a href="/media/clip.mp4">a short clip</a>.</p>
<video poster="/assets/img/poster.png" controls>
  <source src="/media/clip.mp4" type="video/mp4">
</video>
{FOOT}""")

    # Canonical duplicate: different URL, same content, canonical points elsewhere.
    item = ITEMS[0]
    write("dup-of-item-1/index.html", f"""{head(item['name'] + " (duplicate)",
        "A duplicate of item 1 that canonicalises elsewhere", canonical="/item/1")}
<h1>{item['name']}</h1>
<p class="lede">Hand finished in {item['city']}.</p>
<p>This page duplicates <a href="/item/1">item 1</a> and declares it canonical.</p>
{FOOT}""")

    # Client-side rendered page: the shell has almost no content.
    write("post/js-rendered/index.html", f"""{head("Journal | Fixture Shop",
        "Client-side rendered journal", canonical="/post/js-rendered")}
<div id="root"><noscript>This page needs JavaScript.</noscript></div>
<script src="/assets/app.js" defer></script>
{FOOT}""")

    # Infinite scroll.
    write("post/infinite/index.html", f"""{head("Infinite scroll | Fixture Shop",
        "Content appended as you scroll", canonical="/post/infinite")}
<h1>Infinite scroll</h1>
<div id="feed"></div>
<script src="/assets/infinite.js" defer></script>
{FOOT}""")

    # Malformed HTML: unclosed tags, stray markup, bad JSON-LD.
    write("post/malformed/index.html", """<!doctype html>
<html><head><title>Malformed page</title>
<meta property="og:title" content="Malformed page">
<script type="application/ld+json">{ "@type": "Article", "headline": "unterminated</script>
<link rel=canonical href=http://127.0.0.1:8899/post/malformed>
<body>
<h1>Malformed<h2>Nested without closing
<p>Paragraph with <b>unclosed bold and an <a href="/item/2">item link</a>
<ul><li>one<li>two<li>three
<div class="comment" data-comment-id="m-1"><span class="author">Anon</span>
<div class="comment-text">A comment inside malformed markup.</div>
<table><tr><td>cell without close
<img src="/assets/img/item-2-480.png" alt="loose image">
<p>Trailing text with a stray < angle bracket &amp; entity.
""")

    write("broken-link/index.html", f"""{head("Broken link | Fixture Shop",
        "Links to a page that does not exist", canonical="/broken-link")}
<h1>Broken link</h1>
<p>This link is dead: <a href="/does-not-exist">missing page</a>.</p>
<p>This one too: <a href="/also/missing.html">another missing page</a>.</p>
{FOOT}""")

    write("search/index.html", f"""{head("Search | Fixture Shop", "Search the catalogue",
                                         canonical="/search")}
<h1>Search</h1>
<form action="/search" method="get"><input name="q" placeholder="Search"><button>Go</button></form>
<ul class="cards"><li><a href="/item/1">Blue Harbour Lamp</a></li></ul>
{FOOT}""")

    for name in ("mira", "tomas", "ines", "rui"):
        write(f"user/{name}/index.html", f"""{head(f"{name.title()} | Fixture Shop",
            f"Public profile page for {name}", canonical=f"/user/{name}")}
<h1>{name.title()}</h1>
<p>Reviewer profile. Member since 2023.</p>
{FOOT}""")

    write("private/secret/index.html", f"""{head("Members", "Restricted", canonical="/private/secret")}
<h1>Members area</h1><p>This should never be archived.</p>{FOOT}""")


def build_assets() -> None:
    palette = [(31, 84, 140), (176, 96, 42), (92, 112, 68), (140, 60, 96),
               (58, 96, 120), (168, 140, 48), (76, 76, 96), (112, 144, 96),
               (144, 96, 64), (96, 120, 144), (64, 112, 112), (128, 88, 128)]
    for item in ITEMS:
        colour = palette[(item["id"] - 1) % len(palette)]
        write(f"assets/img/item-{item['id']}.png", png(60, 40, colour))
        write(f"assets/img/item-{item['id']}-480.png", png(24, 16, colour))
        # alt2 is intentionally identical across every item: tests dedup.
        write(f"assets/img/item-{item['id']}-alt2.png", png(12, 12, (200, 200, 200)))
        write(f"assets/img/item-{item['id']}-alt3.png", png(16, 10, colour))
    write("assets/img/og-default.png", png(64, 34, (24, 24, 32)))
    write("assets/img/favicon.png", png(8, 8, (200, 40, 40)))
    write("assets/img/poster.png", png(48, 27, (40, 40, 40)))
    write("assets/img/bg-texture.png", png(20, 20, (245, 245, 242)))

    write("assets/style.css", """
:root { --ink:#222; --muted:#666; }
body { font:15px/1.6 system-ui,sans-serif; color:var(--ink); margin:0;
       background:#fff url("/assets/img/bg-texture.png") repeat; }
main { max-width:900px; margin:0 auto; padding:1.5rem; }
.site-header { display:flex; gap:1rem; padding:1rem 1.5rem; border-bottom:1px solid #eee; }
.cards { list-style:none; display:flex; flex-wrap:wrap; gap:1rem; padding:0; }
.card img, .gallery img { max-width:180px; height:auto; display:block; }
.pagination a, .pagination span { padding:.25rem .5rem; }
.comment, .review { border-top:1px solid #eee; padding:.75rem 0; }
.author, .review-author { font-weight:600; }
.rating { color:#b06; margin-left:.5rem; }
.site-footer { border-top:1px solid #eee; padding:1rem 1.5rem; color:var(--muted); }
""".strip())

    write("assets/app.js", """
// Client-side rendering: nothing meaningful exists until this runs.
(async function () {
  const root = document.getElementById('root');
  if (!root) return;
  const res = await fetch('/api/journal.json');
  const data = await res.json();
  const posts = data.posts.map(p => `
    <article class="post">
      <h2>${p.title}</h2>
      <p class="byline">by ${p.author} on <time datetime="${p.date}">${p.date}</time></p>
      <p>${p.body}</p>
      <p><a href="${p.url}">Read the related item</a></p>
      <img src="${p.image}" alt="${p.title}">
    </article>`).join('');
  const comments = data.comments.map(c => `
    <li class="comment" data-comment-id="${c.id}">
      <span class="author">${c.author}</span>
      <time datetime="${c.date}">${c.date}</time>
      <div class="comment-text">${c.text}</div>
    </li>`).join('');
  root.innerHTML = `<h1>${data.title}</h1>${posts}
    <section id="comments"><h2>Comments</h2>
    <ul class="comment-list">${comments}</ul></section>`;
})();
""".strip())

    write("assets/comments.js", """
// "Load more" comments, fetched over XHR one page at a time.
document.addEventListener('click', async function (ev) {
  const btn = ev.target.closest('[data-load-more]');
  if (!btn) return;
  const item = btn.dataset.item, page = parseInt(btn.dataset.page, 10);
  const res = await fetch(`/api/comments?item=${item}&page=${page}`);
  const data = await res.json();
  const list = document.querySelector('.comment-list') ||
               document.querySelector('#reviews');
  for (const c of data.comments) {
    const li = document.createElement('li');
    li.className = 'comment';
    li.dataset.commentId = c.id;
    li.innerHTML = `<span class="author">${c.author}</span>` +
      `<time datetime="${c.date}">${c.date}</time>` +
      (c.rating ? `<span class="rating" data-rating="${c.rating}">${c.rating}/5</span>` : '') +
      `<div class="comment-text">${c.text}</div>`;
    if (list) list.appendChild(li);
  }
  if (data.has_more) { btn.dataset.page = String(page + 1); }
  else { btn.remove(); }
});
""".strip())

    write("assets/infinite.js", """
// Appends a batch every time the viewport nears the bottom.
let page = 0, done = false;
async function loadMore() {
  if (done) return;
  page += 1;
  const res = await fetch(`/api/feed?page=${page}`);
  const data = await res.json();
  const feed = document.getElementById('feed');
  for (const entry of data.entries) {
    const div = document.createElement('div');
    div.className = 'feed-entry';
    div.innerHTML = `<h3>${entry.title}</h3><p>${entry.body}</p>` +
                    `<a href="${entry.url}">${entry.url}</a>`;
    feed.appendChild(div);
  }
  if (!data.has_more) done = true;
}
window.addEventListener('scroll', () => {
  if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 50) loadMore();
});
loadMore();
""".strip())

    write("docs/report.pdf", minimal_pdf("Fixture Shop annual report"))
    # A tiny but structurally valid MP4 container.
    ftyp = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
    mdat = b"\x00\x00\x00\x10mdat" + b"\x00" * 8
    write("media/clip.mp4", ftyp + mdat)


def build_sitemaps() -> None:
    def urlset(entries: list[tuple[str, str | None, list[str]]]) -> str:
        rows = []
        for loc, lastmod, images in entries:
            row = f"  <url>\n    <loc>{BASE}{loc}</loc>\n"
            if lastmod:
                row += f"    <lastmod>{lastmod}</lastmod>\n"
            for image in images:
                row += (f"    <image:image><image:loc>{BASE}{image}</image:loc>"
                        f"</image:image>\n")
            row += "  </url>"
            rows.append(row)
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
                '        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n'
                + "\n".join(rows) + "\n</urlset>\n")

    pages = [
        ("/", "2024-06-01", []),
        ("/about", "2024-05-20", []),
        ("/listing/", "2024-06-02", []),
        ("/listing/page/2", "2024-06-02", []),
        ("/listing/page/3", "2024-06-02", []),
        ("/search?q=lamp", None, []),
        ("/post/js-rendered", "2024-06-03", []),
        ("/post/infinite", "2024-06-03", []),
        ("/post/malformed", "2024-06-03", []),
        ("/broken-link", None, []),
        ("/dup-of-item-1", None, []),
        ("/private/secret", None, []),
    ]
    write("sitemap-pages.xml", urlset(pages))

    items = [(f"/item/{i['id']}", "2024-06-04",
              [f"/assets/img/item-{i['id']}.png"]) for i in ITEMS]
    # Gzipped, and NOT listed in the plain sitemap - only via the index.
    write("sitemap-items.xml.gz", gzip.compress(urlset(items).encode()))

    # A category sitemap reachable only from the index, in plain-text format.
    tags = sorted({t for i in ITEMS for t in i["tags"]})
    write("sitemap-categories.txt",
          "\n".join(f"{BASE}/category/{t}" for t in tags) + "\n")

    write("sitemap_index.xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{BASE}/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>{BASE}/sitemap-items.xml.gz</loc></sitemap>
  <sitemap><loc>{BASE}/sitemap-categories.txt</loc></sitemap>
</sitemapindex>
""")

    write("robots.txt", f"""User-agent: *
Disallow: /private/
Disallow: /api/internal/
Allow: /

Sitemap: {BASE}/sitemap_index.xml
""")


def build_api() -> None:
    """Static JSON endpoints (dynamic ones live in server.py)."""
    write("api/journal.json", json.dumps({
        "title": "Workshop journal",
        "posts": [
            {"title": "Casting the harbour lamp", "author": "Mira Solberg",
             "date": "2024-04-02", "url": "/item/1",
             "image": "/assets/img/item-1-480.png",
             "body": "Notes from the Porto workshop on casting and finishing the "
                     "harbour lamp base, including the patina process."},
            {"title": "Choosing walnut", "author": "Ines Duarte", "date": "2024-04-18",
             "url": "/item/3", "image": "/assets/img/item-3-480.png",
             "body": "Why we moved the stool from beech to walnut, and what that "
                     "changed about the joinery."},
        ],
        "comments": [
            {"id": "j-1", "author": "Lena Fischer", "date": "2024-04-20",
             "text": "This whole post only exists after JavaScript runs."},
            {"id": "j-2", "author": "Arto Niemi", "date": "2024-04-21",
             "text": "Second client-rendered comment."},
        ],
    }, indent=2))


def main() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    build_assets()
    for item in ITEMS:
        build_item(item)
    build_listing()
    build_category()
    build_misc()
    build_sitemaps()
    build_api()
    files = sum(1 for _ in ROOT.rglob("*") if _.is_file())
    size = sum(f.stat().st_size for f in ROOT.rglob("*") if f.is_file())
    print(f"fixture site built at {ROOT}: {files} files, {size / 1024:.1f} KiB")


if __name__ == "__main__":
    main()
