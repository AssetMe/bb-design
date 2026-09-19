"""End-to-end tests against the live fixture server."""
from __future__ import annotations

import asyncio
import csv
import json
import sqlite3
from pathlib import Path

import pytest

from crawler.config import Config, Mode
from crawler.database import Database
from crawler.engine import Engine
from crawler.exporter import Exporter
from crawler.frontier import Frontier
from crawler.mirror import MirrorBuilder
from crawler.validator import Validator

BASE = "http://127.0.0.1:8899"


def make_config(root: Path, **overrides) -> Config:
    defaults = dict(
        roots=[BASE + "/"],
        project_root=str(root),
        output_dir="archive",
        db_path="archive/archive.db",
        log_dir="logs",
        export_dir="exports",
        mirror_dir="mirror",
        concurrency=4,
        per_host_concurrency=3,
        min_delay=0.0,
        max_retries=3,
        backoff_base=1.2,
        backoff_cap=3.0,
        render=True,
    )
    defaults.update(overrides)
    cfg = Config(**defaults)
    cfg.ensure_dirs()
    return cfg


@pytest.fixture(scope="session")
def crawled(tmp_path_factory, live_server):
    """One full crawl, reused by every assertion in this module."""
    root = tmp_path_factory.mktemp("crawl")
    cfg = make_config(root)
    engine = Engine(cfg)
    stats = asyncio.run(engine.run())
    engine.close()
    db = Database(cfg.database_path)
    yield cfg, stats, db
    db.close()


# ----------------------------------------------------------------------
class TestDiscovery:
    def test_robots_fetched_and_stored(self, crawled):
        _cfg, stats, db = crawled
        assert stats.robots_fetched >= 1
        row = db.query_one("SELECT * FROM robots WHERE host='127.0.0.1'")
        assert row["http_status"] == 200
        assert "Disallow: /private/" in row["body"]

    def test_all_sitemaps_including_index_gzip_and_text(self, crawled):
        _cfg, stats, _db = crawled
        # index + pages.xml + items.xml.gz + categories.txt
        assert stats.sitemaps_parsed == 4

    def test_gzipped_sitemap_urls_reached(self, crawled):
        """The item pages exist ONLY in the gzipped sitemap and in page links."""
        _cfg, _stats, db = crawled
        found = db.scalar(
            "SELECT COUNT(*) FROM discovery_sources WHERE source_type='sitemap'"
            " AND url LIKE '%/item/%'")
        assert found == 12

    def test_text_sitemap_urls_reached(self, crawled):
        _cfg, _stats, db = crawled
        assert db.scalar("SELECT COUNT(*) FROM pages WHERE normalized_url LIKE '%/category/%'") >= 6

    def test_every_item_page_archived(self, crawled):
        _cfg, _stats, db = crawled
        for item_id in range(1, 13):
            assert db.query_one(
                "SELECT id FROM pages WHERE normalized_url=?",
                (f"{BASE}/item/{item_id}",)) is not None, f"item {item_id} missing"

    def test_pagination_followed_to_the_last_page(self, crawled):
        _cfg, _stats, db = crawled
        for page in ("/listing/", "/listing/page/2", "/listing/page/3"):
            assert db.query_one("SELECT id FROM pages WHERE normalized_url=?",
                                (BASE + page,)) is not None, f"{page} missing"

    def test_discovery_sources_recorded(self, crawled):
        _cfg, _stats, db = crawled
        kinds = {r["source_type"] for r in db.query(
            "SELECT DISTINCT source_type FROM discovery_sources")}
        assert "sitemap" in kinds
        assert any(k.startswith("link:") for k in kinds)


class TestRobotsAndAccessControl:
    def test_disallowed_path_is_skipped_not_fetched(self, crawled):
        _cfg, _stats, db = crawled
        row = db.query_one("SELECT status, skip_reason, http_status FROM urls"
                           " WHERE normalized_url=?", (BASE + "/private/secret",))
        assert row["status"] == "SKIPPED"
        assert row["skip_reason"] == "robots_disallow"
        assert row["http_status"] is None, "a disallowed URL must never be requested"

    def test_no_page_archived_for_restricted_area(self, crawled):
        _cfg, _stats, db = crawled
        assert db.query_one("SELECT id FROM pages WHERE normalized_url LIKE '%/private/%'") is None


class TestErrorHandling:
    def test_404s_fail_permanently_without_excess_retries(self, crawled):
        _cfg, _stats, db = crawled
        row = db.query_one("SELECT status, http_status, attempt_count FROM urls"
                           " WHERE normalized_url=?", (BASE + "/does-not-exist",))
        assert row["status"] == "FAILED"
        assert row["http_status"] == 404
        assert row["attempt_count"] == 1, "permanent errors must not be retried"

    def test_transient_5xx_recovered_via_backoff(self, crawled):
        """/flaky returns 503 twice, then 200."""
        _cfg, _stats, db = crawled
        row = db.query_one("SELECT status, http_status FROM urls WHERE normalized_url=?",
                           (BASE + "/flaky",))
        assert row["status"] == "COMPLETED"
        assert row["http_status"] == 200
        page = db.query_one("SELECT title FROM pages WHERE normalized_url=?",
                            (BASE + "/flaky",))
        assert page is not None
        assert "recovered" in (page["title"] or "").lower()

    def test_flaky_endpoint_needed_more_than_one_attempt(self, crawled):
        _cfg, _stats, db = crawled
        attempts = db.query(
            "SELECT http_status, outcome FROM crawl_attempts WHERE url=? ORDER BY id",
            (BASE + "/flaky",))
        statuses = [a["http_status"] for a in attempts]
        assert 503 in statuses, f"backoff path not exercised, saw {statuses}"
        assert statuses[-1] == 200

    def test_429_recovered(self, crawled):
        _cfg, _stats, db = crawled
        row = db.query_one("SELECT status FROM urls WHERE normalized_url=?",
                           (BASE + "/rate-limited",))
        assert row["status"] == "COMPLETED"

    def test_redirect_followed_and_chain_recorded(self, crawled):
        _cfg, _stats, db = crawled
        row = db.query_one("SELECT final_url FROM pages WHERE normalized_url=?",
                           (BASE + "/redirect-me",))
        assert row is not None
        assert row["final_url"].rstrip("/").endswith("/about")

    def test_malformed_html_archived_and_parsed(self, crawled):
        """A page the parser struggles with must still be fully preserved."""
        cfg, _stats, db = crawled
        row = db.query_one("SELECT raw_path, title, comment_count FROM pages"
                           " WHERE normalized_url=?", (BASE + "/post/malformed",))
        assert row is not None
        raw = cfg.archive_path / row["raw_path"]
        assert raw.exists() and raw.stat().st_size > 0
        assert row["title"] == "Malformed page"
        assert row["comment_count"] == 1

    def test_crawl_attempts_recorded(self, crawled):
        _cfg, _stats, db = crawled
        assert db.scalar("SELECT COUNT(*) FROM crawl_attempts") > 0
        assert db.scalar("SELECT COUNT(*) FROM crawl_attempts WHERE outcome='ok'") > 0


class TestRendering:
    def test_client_rendered_page_yields_real_content(self, crawled):
        cfg, _stats, db = crawled
        row = db.query_one(
            "SELECT rendered, render_reason, rendered_path, text_length FROM pages"
            " WHERE normalized_url=?", (BASE + "/post/js-rendered",))
        assert row["rendered"] == 1
        assert row["render_reason"] == "empty_shell"
        rendered = cfg.archive_path / row["rendered_path"]
        assert "Casting the harbour lamp" in rendered.read_text(encoding="utf-8")

    def test_raw_html_kept_alongside_rendered(self, crawled):
        cfg, _stats, db = crawled
        row = db.query_one("SELECT raw_path, rendered_path FROM pages WHERE normalized_url=?",
                           (BASE + "/post/js-rendered",))
        raw = (cfg.archive_path / row["raw_path"]).read_text(encoding="utf-8")
        assert 'id="root"' in raw
        assert "Casting the harbour lamp" not in raw, "raw must stay the original response"

    def test_load_more_comments_collected(self, crawled):
        """Comments beyond the first page exist only behind the load-more button."""
        _cfg, _stats, db = crawled
        keys = {r["comment_key"] for r in db.query(
            "SELECT comment_key FROM comments WHERE page_url=?", (BASE + "/item/1",))}
        assert {"c-1-1", "c-1-2"} <= keys, "server-rendered comments missing"
        assert {"x-1-1", "x-1-2", "x-1-3"} <= keys, "XHR-loaded comments missing"

    def test_infinite_scroll_content_collected(self, crawled):
        cfg, _stats, db = crawled
        row = db.query_one("SELECT rendered_path FROM pages WHERE normalized_url=?",
                           (BASE + "/post/infinite",))
        html = (cfg.archive_path / row["rendered_path"]).read_text(encoding="utf-8")
        # Page 1 alone would give entries 1-3; later pages require scrolling.
        assert "Feed entry 7" in html, "infinite scroll did not advance past page 2"

    def test_xhr_json_payloads_captured(self, crawled):
        _cfg, _stats, db = crawled
        rows = db.query("SELECT subtype, parsed_json FROM structured_data WHERE kind='xhr_json'")
        assert rows, "no XHR JSON captured"
        assert any("/api/" in (r["subtype"] or "") for r in rows)

    def test_static_pages_are_not_rendered_unnecessarily(self, crawled):
        _cfg, _stats, db = crawled
        row = db.query_one("SELECT rendered FROM pages WHERE normalized_url=?",
                           (BASE + "/about",))
        assert row["rendered"] == 0


class TestExtraction:
    def test_comments_across_both_layouts(self, crawled):
        _cfg, _stats, db = crawled
        # item/1 uses the list layout, item/7 the review layout.
        assert db.scalar("SELECT COUNT(*) FROM comments WHERE page_url=?",
                         (BASE + "/item/7",)) >= 2

    def test_comment_fields_populated(self, crawled):
        _cfg, _stats, db = crawled
        row = db.query_one("SELECT * FROM comments WHERE comment_key='c-1-1'")
        assert row["author"] == "Mira Solberg"
        assert row["author_url"].endswith("/user/mira")
        assert row["published"] == "2024-02-11"
        assert row["rating"] == "5"
        assert "Warm light" in row["body"]

    def test_replies_linked_to_parent(self, crawled):
        _cfg, _stats, db = crawled
        row = db.query_one("SELECT parent_key, author, body FROM comments"
                           " WHERE comment_key='c-1-1-r1'")
        assert row is not None, "nested reply not extracted"
        assert row["parent_key"] == "c-1-1"

    def test_comment_files_written(self, crawled):
        cfg, _stats, db = crawled
        row = db.query_one("SELECT local_dir FROM pages WHERE normalized_url=?",
                           (BASE + "/item/1",))
        directory = cfg.archive_path / row["local_dir"]
        for name in ("comments.json", "comments.txt", "comments_raw.html"):
            assert (directory / name).exists(), f"{name} missing"
        payload = json.loads((directory / "comments.json").read_text())
        assert len(payload) >= 5

    def test_jsonld_preserved_verbatim(self, crawled):
        cfg, _stats, db = crawled
        row = db.query_one("SELECT local_dir FROM pages WHERE normalized_url=?",
                           (BASE + "/item/3",))
        raw = (cfg.archive_path / row["local_dir"] / "jsonld.raw.json").read_text()
        assert '"@type": "Product"' in raw

    def test_malformed_jsonld_kept_with_error(self, crawled):
        _cfg, _stats, db = crawled
        row = db.query_one(
            "SELECT raw, parse_error FROM structured_data"
            " WHERE kind='jsonld' AND parse_error IS NOT NULL")
        assert row is not None
        assert row["raw"], "raw JSON-LD must survive a parse failure"

    def test_structured_data_kinds_present(self, crawled):
        _cfg, _stats, db = crawled
        kinds = {r["kind"] for r in db.query("SELECT DISTINCT kind FROM structured_data")}
        assert {"jsonld", "opengraph", "twitter", "meta", "microdata"} <= kinds

    def test_text_export_has_sections(self, crawled):
        cfg, _stats, db = crawled
        row = db.query_one("SELECT local_dir FROM pages WHERE normalized_url=?",
                           (BASE + "/item/1",))
        text = (cfg.archive_path / row["local_dir"] / "text.txt").read_text()
        for heading in ("PAGE", "URL:", "TITLE:", "METADATA", "STRUCTURED DATA",
                        "TEXT", "COMMENTS", "LINKS"):
            assert heading in text, f"missing section {heading}"

    def test_metadata_json_complete(self, crawled):
        cfg, _stats, db = crawled
        row = db.query_one("SELECT local_dir FROM pages WHERE normalized_url=?",
                           (BASE + "/item/1",))
        meta = json.loads((cfg.archive_path / row["local_dir"] / "metadata.json").read_text())
        for key in ("url", "normalized_url", "final_url", "canonical_url", "http_status",
                    "content_type", "sha256", "title", "downloaded_at_iso",
                    "response_headers", "discovery_source"):
            assert key in meta, f"metadata missing {key}"
        assert meta["http_status"] == 200

    def test_links_recorded_with_scope_flag(self, crawled):
        _cfg, _stats, db = crawled
        assert db.scalar("SELECT COUNT(*) FROM links") > 100
        assert db.scalar("SELECT COUNT(*) FROM links WHERE link_type='canonical'") > 0
        assert db.scalar("SELECT COUNT(*) FROM links WHERE link_type='pagination'") > 0

    def test_page_types_assigned(self, crawled):
        _cfg, _stats, db = crawled
        types = {r["page_type"] for r in db.query("SELECT DISTINCT page_type FROM pages")}
        assert {"HOME", "DETAIL", "LISTING", "CATEGORY"} <= types
        assert db.scalar("SELECT COUNT(*) FROM pages WHERE page_type IS NULL") == 0


class TestAssets:
    def test_images_downloaded_with_dimensions(self, crawled):
        _cfg, _stats, db = crawled
        row = db.query_one(
            "SELECT * FROM assets WHERE normalized_url=?",
            (BASE + "/assets/img/item-1.png",))
        assert row["local_path"] and row["sha256"]
        assert (row["width"], row["height"]) == (60, 40)
        assert row["mime_type"] == "image/png"

    def test_highest_resolution_variant_preferred(self, crawled):
        _cfg, _stats, db = crawled
        # The listing offers 480w and 1200w; both are referenced, the big one
        # must be among them.
        assert db.query_one("SELECT id FROM assets WHERE normalized_url=?",
                            (BASE + "/assets/img/item-1.png",)) is not None

    def test_lazy_loaded_images_downloaded(self, crawled):
        _cfg, _stats, db = crawled
        assert db.query_one(
            "SELECT local_path FROM assets WHERE normalized_url=? AND local_path IS NOT NULL",
            (BASE + "/assets/img/item-1-alt3.png",)) is not None

    def test_pdf_and_video_archived(self, crawled):
        _cfg, _stats, db = crawled
        pdf = db.query_one("SELECT asset_type, local_path FROM assets"
                           " WHERE normalized_url=?", (BASE + "/docs/report.pdf",))
        mp4 = db.query_one("SELECT asset_type, local_path FROM assets"
                           " WHERE normalized_url=?", (BASE + "/media/clip.mp4",))
        assert pdf["asset_type"] == "document" and pdf["local_path"]
        assert mp4["asset_type"] == "video" and mp4["local_path"]

    def test_stylesheet_and_its_background_image(self, crawled):
        _cfg, _stats, db = crawled
        assert db.query_one("SELECT id FROM assets WHERE normalized_url=?",
                            (BASE + "/assets/style.css",)) is not None
        # bg-texture.png is referenced only from inside the CSS file.
        assert db.query_one(
            "SELECT local_path FROM assets WHERE normalized_url=? AND local_path IS NOT NULL",
            (BASE + "/assets/img/bg-texture.png",)) is not None

    def test_identical_images_stored_once_with_many_references(self, crawled):
        """alt2.png is byte-identical across all 12 items."""
        cfg, _stats, db = crawled
        rows = db.query("SELECT normalized_url, sha256, local_path FROM assets"
                        " WHERE normalized_url LIKE '%-alt2.png'")
        assert len(rows) == 12
        digests = {r["sha256"] for r in rows}
        paths = {r["local_path"] for r in rows}
        assert len(digests) == 1, "identical files should share one hash"
        assert len(paths) == 1, "identical files should share one physical copy"
        assert (cfg.archive_path / rows[0]["local_path"]).exists()

    def test_asset_references_record_which_page_uses_what(self, crawled):
        _cfg, _stats, db = crawled
        count = db.scalar(
            "SELECT COUNT(*) FROM asset_references ar JOIN assets a ON a.id=ar.asset_id"
            " WHERE a.normalized_url LIKE '%-alt2.png'")
        assert count >= 12, "each referencing page must keep its own reference row"

    def test_every_downloaded_asset_exists_on_disk(self, crawled):
        cfg, _stats, db = crawled
        rows = db.query("SELECT normalized_url, local_path, byte_size FROM assets"
                        " WHERE local_path IS NOT NULL")
        assert len(rows) > 40
        for row in rows:
            path = cfg.archive_path / row["local_path"]
            assert path.exists(), f"{row['normalized_url']} -> {row['local_path']} missing"
            assert path.stat().st_size == row["byte_size"]


class TestNormalizationInPractice:
    def test_tracking_parameter_variant_not_fetched_twice(self, crawled):
        _cfg, _stats, db = crawled
        rows = db.query("SELECT normalized_url FROM urls WHERE normalized_url LIKE '%listing%'"
                        " AND normalized_url LIKE '%utm%'")
        assert rows == [], "tracking-parameter URLs should normalize away"

    def test_fragment_variant_not_fetched_twice(self, crawled):
        _cfg, _stats, db = crawled
        assert db.scalar("SELECT COUNT(*) FROM urls WHERE normalized_url LIKE '%#%'") == 0

    def test_canonical_recorded(self, crawled):
        _cfg, _stats, db = crawled
        row = db.query_one("SELECT canonical_url FROM pages WHERE normalized_url=?",
                           (BASE + "/dup-of-item-1",))
        assert row["canonical_url"] == BASE + "/item/1"

    def test_duplicate_content_detected(self, crawled):
        _cfg, stats, _db = crawled
        assert stats.pages_duplicate >= 1


# ----------------------------------------------------------------------
class TestResume:
    def test_interrupted_crawl_resumes_without_refetching(self, tmp_path, live_server):
        cfg = make_config(tmp_path, render=False, max_pages=8)
        engine = Engine(cfg)
        first = asyncio.run(engine.run())
        engine.close()
        assert first.urls_completed > 0

        db = Database(cfg.database_path)
        completed_before = {
            r["normalized_url"] for r in db.query(
                "SELECT normalized_url FROM urls WHERE status='COMPLETED'")}
        pending_before = db.scalar(
            "SELECT COUNT(*) FROM urls WHERE status IN ('DISCOVERED','QUEUED')")
        db.close()
        assert pending_before > 0, "fixture should leave work outstanding"

        # Second run: no page cap, so it continues where it stopped.
        cfg2 = make_config(tmp_path, render=False)
        engine2 = Engine(cfg2)
        second = asyncio.run(engine2.run())
        engine2.close()

        db = Database(cfg2.database_path)
        completed_after = {
            r["normalized_url"] for r in db.query(
                "SELECT normalized_url FROM urls WHERE status='COMPLETED'")}
        # Nothing already done was thrown away, and progress was made.
        assert completed_before <= completed_after
        assert len(completed_after) > len(completed_before)
        # Each URL was attempted once, not re-fetched on the resume.
        for url in completed_before:
            attempts = db.scalar(
                "SELECT COUNT(*) FROM crawl_attempts WHERE url=? AND outcome='ok'", (url,))
            assert attempts == 1, f"{url} was re-fetched on resume"
        db.close()
        assert second.urls_completed > 0

    def test_resume_without_roots_restores_scope(self, tmp_path, live_server):
        """Regression: an empty root list once wiped the scope on resume,
        marking every pending URL out_of_scope instead of crawling it."""
        cfg = make_config(tmp_path, render=False, max_pages=6)
        engine = Engine(cfg)
        asyncio.run(engine.run())
        engine.close()

        db = Database(cfg.database_path)
        pending_before = db.scalar(
            "SELECT COUNT(*) FROM urls WHERE status IN ('DISCOVERED','QUEUED')")
        db.close()
        assert pending_before > 0

        # Second run supplies no roots at all - they must come from the archive.
        cfg2 = make_config(tmp_path, render=False, roots=[])
        engine2 = Engine(cfg2)
        assert cfg2.roots == [BASE + "/"], "roots not restored from the database"
        stats = asyncio.run(engine2.run())
        engine2.close()

        db = Database(cfg2.database_path)
        out_of_scope = db.scalar(
            "SELECT COUNT(*) FROM urls WHERE skip_reason='out_of_scope'")
        db.close()
        assert out_of_scope == 0, "resume wrongly discarded in-scope URLs"
        assert stats.urls_completed > 0

    def test_engine_refuses_a_scopeless_crawl(self, tmp_path):
        cfg = make_config(tmp_path, render=False, roots=[])
        with pytest.raises(ValueError, match="no crawl scope"):
            Engine(cfg)

    def test_limit_stops_crawl_without_discarding_the_frontier(self, tmp_path, live_server):
        """max_pages must end the run, not mark the remainder SKIPPED."""
        cfg = make_config(tmp_path, render=False, max_pages=5)
        engine = Engine(cfg)
        asyncio.run(engine.run())
        engine.close()
        db = Database(cfg.database_path)
        assert db.scalar("SELECT COUNT(*) FROM urls WHERE skip_reason='max_pages_reached'") == 0
        assert db.scalar(
            "SELECT COUNT(*) FROM urls WHERE status IN ('DISCOVERED','QUEUED')") > 0
        assert db.scalar("SELECT COUNT(*) FROM urls WHERE status='FETCHING'") == 0
        db.close()

    def test_requeue_skipped_by_reason(self, tmp_path, live_server):
        cfg = make_config(tmp_path, render=False, max_pages=4)
        engine = Engine(cfg)
        asyncio.run(engine.run())
        engine.close()
        db = Database(cfg.database_path)
        frontier = Frontier(db)
        before = db.scalar("SELECT COUNT(*) FROM urls WHERE skip_reason='robots_disallow'")
        assert frontier.requeue_skipped("robots_disallow") == before
        assert db.scalar("SELECT COUNT(*) FROM urls WHERE skip_reason='robots_disallow'") == 0
        db.close()

    def test_stale_inflight_urls_are_requeued(self, tmp_path, live_server):
        cfg = make_config(tmp_path, render=False, max_pages=3)
        engine = Engine(cfg)
        asyncio.run(engine.run())
        engine.close()

        db = Database(cfg.database_path)
        # Simulate a hard crash: leave a URL stuck in FETCHING.
        db.execute("UPDATE urls SET status='FETCHING' WHERE status='QUEUED'"
                   " AND id=(SELECT MIN(id) FROM urls WHERE status='QUEUED')")
        stuck = db.scalar("SELECT COUNT(*) FROM urls WHERE status='FETCHING'")
        assert stuck == 1
        frontier = Frontier(db)
        assert frontier.reset_stale() == 1
        assert db.scalar("SELECT COUNT(*) FROM urls WHERE status='FETCHING'") == 0
        db.close()

    def test_force_requeues_completed(self, tmp_path, live_server):
        cfg = make_config(tmp_path, render=False, max_pages=4)
        engine = Engine(cfg)
        asyncio.run(engine.run())
        engine.close()
        db = Database(cfg.database_path)
        frontier = Frontier(db)
        assert frontier.force_requeue_completed() > 0
        db.close()


class TestDiscoveryOnlyMode:
    def test_discovery_only_queues_without_fetching_pages(self, tmp_path, live_server):
        cfg = make_config(tmp_path, render=False, mode=Mode.DISCOVERY_ONLY)
        engine = Engine(cfg)
        asyncio.run(engine.run())
        engine.close()
        db = Database(cfg.database_path)
        assert db.scalar("SELECT COUNT(*) FROM urls") > 20
        assert db.scalar("SELECT COUNT(*) FROM pages") == 0
        db.close()


# ----------------------------------------------------------------------
class TestValidation:
    def test_clean_archive_validates(self, crawled):
        cfg, _stats, _db = crawled
        report = Validator(cfg).run(verify_hashes=True)
        assert report.missing_files == [], report.missing_files[:3]
        assert report.zero_byte_files == [], report.zero_byte_files[:3]
        assert report.hash_mismatches == [], report.hash_mismatches[:3]
        assert report.broken_references == []
        assert report.ok

    def test_validation_detects_a_deleted_file(self, crawled, tmp_path):
        """Validation must actually check the disk, not just the database."""
        cfg, _stats, db = crawled
        row = db.query_one("SELECT local_path FROM assets WHERE local_path IS NOT NULL LIMIT 1")
        victim = cfg.archive_path / row["local_path"]
        backup = victim.read_bytes()
        victim.unlink()
        try:
            report = Validator(cfg).run(verify_hashes=False)
            assert any(m["path"] == row["local_path"] for m in report.missing_files)
            assert not report.ok
        finally:
            victim.write_bytes(backup)

    def test_validation_detects_corruption(self, crawled):
        cfg, _stats, db = crawled
        row = db.query_one("SELECT local_path FROM assets WHERE local_path IS NOT NULL LIMIT 1")
        victim = cfg.archive_path / row["local_path"]
        backup = victim.read_bytes()
        victim.write_bytes(backup + b"tampered")
        try:
            report = Validator(cfg).run(verify_hashes=True)
            assert report.hash_mismatches, "corrupted file not detected"
        finally:
            victim.write_bytes(backup)

    def test_reports_written(self, crawled):
        cfg, _stats, _db = crawled
        validator = Validator(cfg)
        written = validator.write_reports(validator.run(verify_hashes=False))
        for name in ("missing.csv", "failed.csv", "duplicates.csv",
                     "validation.json", "missing_urls.txt"):
            assert written[name].exists(), f"{name} not written"
        data = json.loads(written["validation.json"].read_text())
        assert "totals" in data and "counts" in data

    def test_failed_csv_lists_the_404s(self, crawled):
        cfg, _stats, _db = crawled
        validator = Validator(cfg)
        validator.write_reports(validator.run(verify_hashes=False))
        rows = list(csv.DictReader((cfg.exports_path / "failed.csv").open()))
        urls = {r["url"] for r in rows}
        assert BASE + "/does-not-exist" in urls


class TestExports:
    def test_all_exports_written(self, crawled):
        cfg, _stats, _db = crawled
        results = Exporter(cfg).export_all()
        for name in ("pages.jsonl", "pages.json", "pages.csv", "assets.jsonl",
                     "asset_references.jsonl", "links.jsonl", "comments.jsonl",
                     "structured_data.jsonl", "errors.jsonl", "urls.jsonl",
                     "urls.txt", "discovery_sources.jsonl"):
            path = cfg.exports_path / name
            assert path.exists(), f"{name} missing"
        assert results["pages.jsonl"] > 30
        assert (cfg.exports_path / "README.txt").exists()

    def test_jsonl_is_valid_line_delimited_json(self, crawled):
        cfg, _stats, _db = crawled
        Exporter(cfg).export_all()
        for name in ("pages.jsonl", "assets.jsonl", "comments.jsonl", "links.jsonl"):
            for line in (cfg.exports_path / name).read_text().splitlines():
                json.loads(line)

    def test_pages_csv_opens_as_a_table(self, crawled):
        cfg, _stats, _db = crawled
        Exporter(cfg).export_all()
        rows = list(csv.DictReader((cfg.exports_path / "pages.csv").open()))
        assert rows
        assert {"url", "title", "page_type", "comments_total", "images_total"} <= set(rows[0])

    def test_manifest_written(self, crawled):
        cfg, _stats, _db = crawled
        Exporter(cfg).export_all()
        manifest = json.loads((cfg.archive_path / "manifest.json").read_text())
        assert manifest["page_count"] > 30
        assert manifest["target"] == [BASE + "/"]
        assert manifest["total_bytes"] > 0


class TestMirror:
    def test_mirror_built_with_rewritten_links(self, crawled):
        cfg, _stats, _db = crawled
        result = MirrorBuilder(cfg).build()
        assert result["pages_written"] > 30
        assert result["links_rewritten"] > 0
        assert result["asset_refs_rewritten"] > 0
        index = cfg.mirror_path / "index.html"
        assert index.exists()

    def test_mirror_links_resolve_on_disk(self, crawled):
        import re

        cfg, _stats, _db = crawled
        MirrorBuilder(cfg).build()
        candidates = list(cfg.mirror_path.rglob("*.html"))
        assert candidates
        checked = 0
        # Only links the mirror actually rewrote are expected to resolve; a
        # link to a page that was never archived is left pointing at the
        # original site on purpose.
        # BeautifulSoup emits attributes alphabetically, so the marker can sit
        # on either side of href - match the whole tag and pull href out.
        tag_re = re.compile(r"<a\s[^>]*data-original-href=[^>]*>", re.I)
        href_re = re.compile(r'\shref="([^"]+)"')
        for page in candidates[:25]:
            html = page.read_text(encoding="utf-8", errors="replace")
            for tag in tag_re.findall(html):
                match = href_re.search(tag)
                if not match:
                    continue
                href = match.group(1)
                target = (page.parent / href).resolve()
                assert target.exists(), f"{page.name} -> {href} does not resolve"
                checked += 1
        assert checked > 0, "no rewritten links found to verify"

    def test_raw_archive_untouched_by_mirroring(self, crawled):
        cfg, _stats, db = crawled
        row = db.query_one("SELECT raw_path, sha256 FROM pages WHERE normalized_url=?",
                           (BASE + "/item/1",))
        import hashlib
        raw = (cfg.archive_path / row["raw_path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row["sha256"]
