# site_archiver

A resumable, fault-tolerant website archival crawler. It builds a faithful
offline archive of a site you are authorized to archive: raw responses,
post-JavaScript DOM where rendering is needed, assets, structured data,
comments, a SQLite index, analysis-ready exports, and a browsable offline
mirror.

Everything it records comes from ordinary public requests. It honours
robots.txt by default and never attempts to defeat authentication, CAPTCHA,
paywalls or any other access control — restricted resources are recorded as
`ACCESS_RESTRICTED` and the crawl continues.

---

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # optional; skip if a browser is already present

python run.py crawl https://example.com/     # crawl (resumes automatically)
python run.py validate                       # verify the archive against the disk
python run.py export                         # JSONL / CSV / JSON exports
python run.py mirror                         # browsable offline copy
python run.py status                         # what is in the archive right now
```

Interrupt with Ctrl+C at any point. All progress is in SQLite; re-running the
same command continues from where it stopped.

## Commands

| Command | What it does |
| --- | --- |
| `crawl` | Full crawl: discover, fetch, render, extract, store. The default. |
| `discover` | Build the URL frontier from robots.txt and sitemaps only — fetches no pages. |
| `download` | Fetch and store without JavaScript rendering. |
| `render` | Force browser rendering for every page. |
| `validate` | Re-hash every file, check DB/disk consistency, write reports. |
| `export` | Write JSONL/CSV/JSON exports and `manifest.json`. |
| `mirror` | Build `mirror/` with internal links rewritten to local files. |
| `status` | Frontier and content counts for an existing archive. |
| `retry-failed` | Requeue URLs that ended in `FAILED`. |
| `requeue-skipped` | Requeue skipped URLs, optionally `--reason out_of_scope`. |

Roots are remembered in the archive, so `python run.py crawl --resume` works
without repeating the URL.

## Useful options

```bash
--concurrency 4 --per-host-concurrency 2 --delay 0.5   # politeness (defaults)
--max-depth 12 --max-pages 500 --max-assets 2000       # limits; a reached limit
                                                       # stops the run cleanly
--deny '/cart' --deny '\?add-to-cart='                 # regex exclusions
--allow '/blog/'                                       # regex allow-list
--no-subdomains --offsite-assets
--no-render --render-all --render-timeout 30
--force                                                # re-fetch completed URLs
--config config.json                                   # see config.example.json
```

`--ignore-robots` exists for sites you operate yourself. The default is to
honour robots.txt, and disallowed URLs are recorded as `SKIPPED` rather than
requested repeatedly.

## How it works

```
roots ─► robots.txt ─► sitemap index ─► sitemaps (xml / xml.gz / txt)
                                            │
                                            ▼
                              ┌──── SQLite URL frontier ────┐
                              │  DISCOVERED → QUEUED →      │
                              │  FETCHING → COMPLETED /     │
                              │  FAILED / SKIPPED           │
                              └──────────────┬──────────────┘
                                             ▼
    httpx (rate limited, retry/backoff) ─► raw bytes ─► raw.html
                                             │
                    needs JavaScript? ───────┤
                                             ▼
    Playwright (networkidle, load-more, infinite scroll, XHR capture)
                                             │
                                             ▼
        links · pagination · canonical · JSON-LD · OpenGraph · microdata
        embedded JSON · comments/replies · assets (srcset, lazy, CSS urls)
                                             │
                                             ▼
        archive/ + SQLite + exports/ + mirror/
```

### Discovery

robots.txt `Sitemap:` directives, eight common sitemap locations, sitemap
indexes, gzipped sitemaps and plain-text sitemaps; then links, `rel=next/prev`,
numbered and path-based pagination, canonical and `hreflang` URLs, URLs inside
JSON-LD and embedded JSON, GET form actions, and resources observed in the
browser's own network traffic. Every URL records how it was found
(`discovery_sources`).

### Rendering

Plain HTTP first. A browser is used only when the page demonstrably needs one —
an empty application shell, no links with scripts present, a load-more control,
a client-side-rendering state blob, or a very low text-to-bytes ratio. The
reason is stored in `pages.render_reason`. When rendering happens the crawler
clicks load-more controls until they stop appearing, drives infinite scroll
until the page stops producing content, and captures the JSON the page fetched
for itself into `xhr/`.

`raw.html` is always the original response. Rendering only ever adds
`rendered.html` beside it.

### Resilience

- Exponential backoff with jitter for 408/429/5xx, timeouts and connection
  resets; `Retry-After` is honoured (clamped).
- 400/401/403/404/410/451 are permanent — recorded once, never hammered.
- A 429 permanently slows that host for the rest of the run.
- Parser failures are logged and the raw bytes kept; one broken page never
  ends the crawl.
- A crashed browser is restarted and the page retried once.
- Every attempt, including retried ones, lands in `crawl_attempts`.

### Deduplication

Assets are content-addressed by SHA-256, so identical bytes are stored once
while `asset_references` keeps every page that uses them. Pages with an
identical body hash are linked via `pages.duplicate_of`, which also stops
pagination loops.

## Archive layout

```
archive/
  pages/<xx>/<hash>_<slug>/
    raw.html            original response body (authoritative, never rewritten)
    rendered.html       post-JavaScript DOM (only when rendering was needed)
    text.txt            normalized, section-headed text export
    metadata.json       status, headers, canonical, redirects, discovery source
    structured.json     parsed structured data
    jsonld.raw.json     original JSON-LD, verbatim
    comments.json       extracted comments with replies
    comments.txt        readable comment transcript
    comments_raw.html   original comment-region markup
    xhr/NNN.json        JSON the page fetched for itself
  assets/<xx>/<yy>/<sha256>.<ext>     content-addressed assets
  media/, documents/                  video/audio and documents
  site/                               robots.txt and sitemaps as fetched
  archive.db                          SQLite index
  manifest.json

exports/   pages/assets/links/comments/structured_data/urls/errors as JSONL+CSV,
           plus validation.json, missing.csv, failed.csv, duplicates.csv,
           missing_urls.txt, README.txt
mirror/    browsable offline copy with internal links rewritten
logs/      crawler.log, errors.log, statistics.json
```

`text.txt` uses fixed section headings (`PAGE`, `METADATA`, `STRUCTURED DATA`,
`TEXT`, `COMMENTS`, `LINKS`) so the archive is straightforward to feed to
downstream text analysis without re-parsing HTML.

## Database

13 tables: `urls` (the frontier), `pages`, `links`, `assets`,
`asset_references`, `structured_data`, `comments`, `crawl_attempts`,
`discovery_sources`, `errors`, `robots`, `media_events`, `meta`. Foreign keys
on, WAL journaling, indexes on the columns queries actually use.

```sql
-- pages with the most comments
SELECT p.url, COUNT(c.id) n FROM pages p JOIN comments c ON c.page_id = p.id
GROUP BY p.id ORDER BY n DESC LIMIT 20;

-- images shared across several pages
SELECT a.normalized_url, COUNT(DISTINCT r.page_id) pages FROM assets a
JOIN asset_references r ON r.asset_id = a.id WHERE a.asset_type = 'image'
GROUP BY a.id HAVING pages > 1 ORDER BY pages DESC;

-- everything discovered but not archived
SELECT normalized_url, status, http_status, last_error, skip_reason
FROM urls WHERE status <> 'COMPLETED';
```

## Validation

`validate` re-reads every file the database claims exists, recomputes SHA-256,
and reports missing files, zero-byte files, hash mismatches, broken foreign-key
references, failed URLs, unprocessed URLs, duplicate pages and duplicate media.
It writes `exports/validation.json` and exits non-zero when anything is wrong,
so it can gate a scripted archive run.

## Testing

The crawler is developed against a local fixture site that deliberately
includes the awkward cases: a sitemap index, a gzipped sitemap, a plain-text
sitemap, pagination, canonical duplicates, tracking-parameter URLs, lazy
images, `srcset`, client-side rendering, XHR-loaded comments behind a
load-more button, infinite scroll, malformed HTML, a redirect, broken links, a
robots-disallowed area returning 401, an endpoint that fails twice before
succeeding, and one that returns 429.

```bash
python fixtures/build_site.py             # generate the fixture site
python fixtures/server.py --port 8899     # serve it (tests start it themselves)
python -m pytest tests/ -q                # 228 tests
python fixtures/benchmark.py              # throughput / memory / DB growth
```

The suite covers URL normalization, sitemap and robots parsing, frontier
persistence and resume, duplicate detection, HTML parsing, structured data,
pagination, asset extraction, hashing, database constraints, error handling,
validation, exports, the mirror, and full end-to-end crawls.

## Requirements

Python 3.11+. `httpx`, `beautifulsoup4` and `lxml` are required; `playwright`
(JavaScript rendering), `Pillow` (image dimensions — header-parsing fallbacks
exist for PNG/GIF/JPEG/SVG) and `PyYAML` (YAML config) are optional.

If Playwright's bundled browser revision does not match the one installed on
the machine, the crawler probes for a usable Chromium instead of downloading
one. Override with `--browser /path/to/chrome` or `ARCHIVER_BROWSER_PATH`.

## Scope and conduct

Use this on sites you own or are authorized to archive. Defaults are
deliberately conservative: robots.txt honoured, `Crawl-delay` respected, 4
concurrent requests, 2 per host, 0.5s minimum gap, capped retries. Raise
concurrency only against infrastructure you control.
