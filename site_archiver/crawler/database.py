"""SQLite schema and access helpers for the archive."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- The URL frontier.  Every resource the crawler has ever heard of lives here.
CREATE TABLE IF NOT EXISTS urls (
    id               INTEGER PRIMARY KEY,
    url              TEXT NOT NULL,              -- original, as discovered
    normalized_url   TEXT NOT NULL UNIQUE,
    canonical_url    TEXT,
    final_url        TEXT,                       -- after redirects
    kind             TEXT NOT NULL DEFAULT 'page' CHECK (kind IN ('page','asset')),
    priority         INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'DISCOVERED'
                     CHECK (status IN ('DISCOVERED','QUEUED','FETCHING','FETCHED',
                                       'PARSED','COMPLETED','FAILED','SKIPPED')),
    depth            INTEGER NOT NULL DEFAULT 0,
    parent_url       TEXT,
    discovery_source TEXT,
    first_seen       REAL NOT NULL,
    last_seen        REAL NOT NULL,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    http_status      INTEGER,
    content_type     TEXT,
    content_hash     TEXT,
    skip_reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_urls_status   ON urls(status);
CREATE INDEX IF NOT EXISTS idx_urls_claim    ON urls(status, priority, depth, id);
CREATE INDEX IF NOT EXISTS idx_urls_kind     ON urls(kind);
CREATE INDEX IF NOT EXISTS idx_urls_hash     ON urls(content_hash);

CREATE TABLE IF NOT EXISTS pages (
    id             INTEGER PRIMARY KEY,
    url_id         INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
    url            TEXT NOT NULL,
    normalized_url TEXT NOT NULL UNIQUE,
    canonical_url  TEXT,
    final_url      TEXT,
    title          TEXT,
    page_type      TEXT,
    status         TEXT,
    http_status    INTEGER,
    content_type   TEXT,
    content_length INTEGER,
    encoding       TEXT,
    sha256         TEXT,
    rendered       INTEGER NOT NULL DEFAULT 0,
    render_reason  TEXT,
    depth          INTEGER,
    parent_id      INTEGER,
    first_seen     REAL,
    last_crawled   REAL,
    local_dir      TEXT,
    raw_path       TEXT,
    rendered_path  TEXT,
    text_path      TEXT,
    text_length    INTEGER,
    word_count     INTEGER,
    link_count     INTEGER,
    asset_count    INTEGER,
    comment_count  INTEGER,
    duplicate_of   INTEGER REFERENCES pages(id) ON DELETE SET NULL,
    headers_json   TEXT,
    redirect_chain TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_sha  ON pages(sha256);
CREATE INDEX IF NOT EXISTS idx_pages_type ON pages(page_type);
CREATE INDEX IF NOT EXISTS idx_pages_dup  ON pages(duplicate_of);

CREATE TABLE IF NOT EXISTS links (
    id             INTEGER PRIMARY KEY,
    source_page_id INTEGER REFERENCES pages(id) ON DELETE CASCADE,
    source_url     TEXT NOT NULL,
    destination_url TEXT NOT NULL,
    normalized_destination TEXT NOT NULL,
    link_text      TEXT,
    rel            TEXT,
    link_type      TEXT,            -- a, canonical, alternate, jsonld, pagination...
    in_scope       INTEGER NOT NULL DEFAULT 1,
    discovered_at  REAL NOT NULL,
    UNIQUE(source_url, normalized_destination, link_type)
);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(source_page_id);
CREATE INDEX IF NOT EXISTS idx_links_dst ON links(normalized_destination);

CREATE TABLE IF NOT EXISTS assets (
    id            INTEGER PRIMARY KEY,
    url_id        INTEGER REFERENCES urls(id) ON DELETE SET NULL,
    url           TEXT NOT NULL,
    normalized_url TEXT NOT NULL UNIQUE,
    final_url     TEXT,
    asset_type    TEXT,             -- image, video, audio, stylesheet, script, document, other
    mime_type     TEXT,
    extension     TEXT,
    http_status   INTEGER,
    byte_size     INTEGER,
    sha256        TEXT,
    width         INTEGER,
    height        INTEGER,
    local_path    TEXT,
    downloaded_at REAL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_sha  ON assets(sha256);
CREATE INDEX IF NOT EXISTS idx_assets_type ON assets(asset_type);

-- Many pages -> one physical asset.  Keeps dedup references intact.
CREATE TABLE IF NOT EXISTS asset_references (
    id          INTEGER PRIMARY KEY,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    page_id     INTEGER REFERENCES pages(id) ON DELETE CASCADE,
    page_url    TEXT NOT NULL,
    attribute   TEXT,               -- src, srcset, data-src, poster, og:image...
    element     TEXT,
    position    INTEGER,
    alt_text    TEXT,
    UNIQUE(asset_id, page_url, attribute, position)
);
CREATE INDEX IF NOT EXISTS idx_assetref_page ON asset_references(page_id);

CREATE TABLE IF NOT EXISTS structured_data (
    id          INTEGER PRIMARY KEY,
    page_id     INTEGER REFERENCES pages(id) ON DELETE CASCADE,
    page_url    TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- jsonld, opengraph, twitter, microdata, meta, embedded_json
    subtype     TEXT,               -- @type / property name
    raw         TEXT,               -- original text, always preserved
    parsed_json TEXT,               -- parsed form, NULL when unparsable
    parse_error TEXT,
    position    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sd_page ON structured_data(page_id);
CREATE INDEX IF NOT EXISTS idx_sd_kind ON structured_data(kind);

CREATE TABLE IF NOT EXISTS comments (
    id           INTEGER PRIMARY KEY,
    page_id      INTEGER REFERENCES pages(id) ON DELETE CASCADE,
    page_url     TEXT NOT NULL,
    comment_key  TEXT,              -- site-provided id when present
    parent_key   TEXT,
    author       TEXT,
    author_url   TEXT,
    published    TEXT,
    rating       TEXT,
    body         TEXT,
    source       TEXT,              -- html, rendered, jsonld, xhr
    position     INTEGER,
    raw_html     TEXT,
    UNIQUE(page_url, source, position)
);
CREATE INDEX IF NOT EXISTS idx_comments_page ON comments(page_id);

CREATE TABLE IF NOT EXISTS discovery_sources (
    id            INTEGER PRIMARY KEY,
    url           TEXT NOT NULL,
    source_type   TEXT NOT NULL,    -- root, robots, sitemap, sitemap_index, link, network, api
    source_url    TEXT,
    discovered_at REAL NOT NULL,
    detail        TEXT,
    UNIQUE(url, source_type, source_url)
);

CREATE TABLE IF NOT EXISTS crawl_attempts (
    id           INTEGER PRIMARY KEY,
    url_id       INTEGER REFERENCES urls(id) ON DELETE CASCADE,
    url          TEXT NOT NULL,
    attempt_no   INTEGER,
    started_at   REAL,
    finished_at  REAL,
    duration_ms  INTEGER,
    http_status  INTEGER,
    bytes        INTEGER,
    method       TEXT,
    outcome      TEXT,              -- ok, retry, failed, skipped
    error        TEXT,
    via          TEXT               -- http, browser
);
CREATE INDEX IF NOT EXISTS idx_attempts_url ON crawl_attempts(url_id);

CREATE TABLE IF NOT EXISTS errors (
    id         INTEGER PRIMARY KEY,
    url        TEXT,
    phase      TEXT,                -- fetch, render, parse, store, validate
    error_type TEXT,
    message    TEXT,
    traceback  TEXT,
    occurred_at REAL NOT NULL,
    fatal      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_errors_phase ON errors(phase);

CREATE TABLE IF NOT EXISTS robots (
    host        TEXT PRIMARY KEY,
    url         TEXT,
    http_status INTEGER,
    body        TEXT,
    crawl_delay REAL,
    fetched_at  REAL
);

CREATE TABLE IF NOT EXISTS media_events (
    id        INTEGER PRIMARY KEY,
    page_url  TEXT NOT NULL,
    url       TEXT NOT NULL,
    resource_type TEXT,
    mime_type TEXT,
    http_status INTEGER,
    observed_at REAL NOT NULL,
    UNIQUE(page_url, url)
);
"""


class Database:
    """Thin, thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self.conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.close()

    def _migrate(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    # -- primitives -----------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
            except Exception:
                cur.execute("ROLLBACK")
                raise
            else:
                cur.execute("COMMIT")
            finally:
                cur.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = 0) -> Any:
        row = self.query_one(sql, params)
        if row is None or row[0] is None:
            return default
        return row[0]

    # -- meta -----------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        if not isinstance(value, str):
            value = json.dumps(value)
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    # -- convenience ----------------------------------------------------
    def log_error(
        self,
        url: str | None,
        phase: str,
        exc: BaseException | str,
        tb: str | None = None,
        fatal: bool = False,
    ) -> None:
        etype = type(exc).__name__ if isinstance(exc, BaseException) else "Error"
        self.execute(
            "INSERT INTO errors(url, phase, error_type, message, traceback, occurred_at, fatal)"
            " VALUES(?,?,?,?,?,?,?)",
            (url, phase, etype, str(exc)[:4000], tb, time.time(), int(fatal)),
        )

    def record_discovery(
        self, url: str, source_type: str, source_url: str | None = None, detail: str | None = None
    ) -> None:
        self.execute(
            "INSERT OR IGNORE INTO discovery_sources(url, source_type, source_url, discovered_at, detail)"
            " VALUES(?,?,?,?,?)",
            (url, source_type, source_url, time.time(), detail),
        )

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()
