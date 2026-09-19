from __future__ import annotations

import sqlite3

import pytest

from crawler.database import Database
from crawler.frontier import ASSET, PAGE, Frontier


@pytest.fixture
def frontier(db):
    return Frontier(db)


class TestDatabase:
    def test_schema_created_with_foreign_keys(self, db):
        tables = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"urls", "pages", "links", "assets", "asset_references",
                "structured_data", "comments", "crawl_attempts",
                "discovery_sources", "errors"} <= tables
        assert db.query_one("PRAGMA foreign_keys")[0] == 1

    def test_wal_mode(self, db):
        assert db.query_one("PRAGMA journal_mode")[0].lower() == "wal"

    def test_meta_roundtrip(self, db):
        db.set_meta("k", "v")
        assert db.get_meta("k") == "v"
        db.set_meta("k", "v2")
        assert db.get_meta("k") == "v2"

    def test_transaction_rolls_back(self, db):
        with pytest.raises(RuntimeError):
            with db.transaction() as cur:
                cur.execute("INSERT INTO errors(phase, occurred_at) VALUES('x', 1)")
                raise RuntimeError("boom")
        assert db.scalar("SELECT COUNT(*) FROM errors") == 0

    def test_foreign_key_cascade(self, db):
        db.execute("INSERT INTO urls(url, normalized_url, first_seen, last_seen)"
                   " VALUES('u','u',1,1)")
        uid = db.query_one("SELECT id FROM urls")["id"]
        db.execute("INSERT INTO pages(url_id, url, normalized_url) VALUES(?,'u','u')", (uid,))
        pid = db.query_one("SELECT id FROM pages")["id"]
        db.execute("INSERT INTO comments(page_id, page_url, body, position, source)"
                   " VALUES(?,'u','x',1,'html')", (pid,))
        db.execute("DELETE FROM urls WHERE id=?", (uid,))
        assert db.scalar("SELECT COUNT(*) FROM pages") == 0
        assert db.scalar("SELECT COUNT(*) FROM comments") == 0

    def test_status_check_constraint(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO urls(url, normalized_url, status, first_seen, last_seen)"
                       " VALUES('u','u','NONSENSE',1,1)")

    def test_log_error(self, db):
        db.log_error("https://e.com/x", "fetch", ValueError("bad"))
        row = db.query_one("SELECT * FROM errors")
        assert row["error_type"] == "ValueError" and row["phase"] == "fetch"


class TestFrontier:
    def test_add_normalizes_and_deduplicates(self, frontier, db):
        a = frontier.add("https://E.com/a?utm_source=x#frag")
        b = frontier.add("https://e.com/a")
        assert a == b == "https://e.com/a"
        assert db.scalar("SELECT COUNT(*) FROM urls") == 1

    def test_add_rejects_unusable_urls(self, frontier):
        assert frontier.add("mailto:a@b.c") is None
        assert frontier.add("") is None

    def test_on_new_fires_once_per_url(self, frontier):
        seen = []
        frontier.on_new = seen.append
        frontier.add("https://e.com/a")
        frontier.add("https://e.com/a")
        frontier.add("https://e.com/b")
        assert seen == ["https://e.com/a", "https://e.com/b"]

    def test_claim_is_exclusive(self, frontier):
        frontier.add("https://e.com/a")
        first = frontier.claim()
        assert first is not None
        assert frontier.claim() is None          # nothing else queued
        assert first.attempt_count == 1

    def test_pages_claimed_before_assets(self, frontier):
        frontier.add("https://e.com/img.png", kind=ASSET)
        frontier.add("https://e.com/page", kind=PAGE)
        assert frontier.claim().kind == PAGE

    def test_shallower_urls_first(self, frontier):
        frontier.add("https://e.com/deep", depth=5)
        frontier.add("https://e.com/shallow", depth=1)
        assert frontier.claim().normalized_url == "https://e.com/shallow"

    def test_re_adding_lowers_depth_but_never_resets_completion(self, frontier, db):
        frontier.add("https://e.com/a", depth=5)
        item = frontier.claim()
        frontier.complete(item, http_status=200)
        frontier.add("https://e.com/a", depth=1)
        row = db.query_one("SELECT status, depth FROM urls WHERE normalized_url=?",
                           ("https://e.com/a",))
        assert row["status"] == "COMPLETED"
        assert row["depth"] == 1

    def test_fail_retries_then_gives_up(self, frontier, db):
        frontier.add("https://e.com/a")
        for attempt in range(1, 4):
            item = frontier.claim()
            assert item is not None, f"not requeued on attempt {attempt}"
            retried = frontier.fail(item, "boom", max_retries=2)
            if attempt < 2:
                assert retried
            else:
                assert not retried
                break
        assert db.query_one("SELECT status FROM urls")["status"] == "FAILED"

    def test_permanent_failure_is_not_retried(self, frontier, db):
        frontier.add("https://e.com/a")
        item = frontier.claim()
        assert frontier.fail(item, "HTTP_404", permanent=True, max_retries=5) is False
        assert db.query_one("SELECT status FROM urls")["status"] == "FAILED"

    def test_skip_records_reason(self, frontier, db):
        frontier.add("https://e.com/a")
        frontier.skip(frontier.claim(), "robots_disallow")
        row = db.query_one("SELECT status, skip_reason FROM urls")
        assert row["status"] == "SKIPPED" and row["skip_reason"] == "robots_disallow"

    def test_reset_stale_requeues_inflight(self, frontier, db):
        frontier.add("https://e.com/a")
        frontier.claim()                       # left FETCHING, as after a crash
        assert frontier.reset_stale() == 1
        assert frontier.claim() is not None

    def test_release_returns_item_to_queue(self, frontier):
        frontier.add("https://e.com/a")
        item = frontier.claim()
        frontier.release(item)
        assert frontier.claim() is not None

    def test_content_hash_duplicate_detection(self, frontier):
        frontier.add("https://e.com/a")
        first = frontier.claim()
        frontier.complete(first, content_hash="deadbeef")
        frontier.add("https://e.com/b")
        second = frontier.claim()
        assert frontier.seen_content_hash("deadbeef", second.id) == "https://e.com/a"
        assert frontier.seen_content_hash("other", second.id) is None

    def test_force_requeue_and_retry_failed(self, frontier, db):
        frontier.add("https://e.com/a")
        frontier.complete(frontier.claim(), http_status=200)
        frontier.add("https://e.com/b")
        frontier.fail(frontier.claim(), "x", permanent=True)
        assert frontier.requeue_failed() == 1
        assert frontier.force_requeue_completed() >= 1

    def test_counts(self, frontier):
        frontier.add("https://e.com/a")
        frontier.add("https://e.com/i.png", kind=ASSET)
        assert frontier.pending_count() == 2
        assert frontier.counts_by_kind() == {"page": 1, "asset": 1}


class TestPersistenceAcrossReopen:
    def test_state_survives_reopen(self, tmp_path):
        path = tmp_path / "f.db"
        db1 = Database(path)
        f1 = Frontier(db1)
        f1.add("https://e.com/a")
        f1.add("https://e.com/b")
        f1.complete(f1.claim(), http_status=200)
        db1.close()

        db2 = Database(path)
        f2 = Frontier(db2)
        assert f2.pending_count() == 1
        assert f2.is_done("https://e.com/a")
        assert not f2.is_done("https://e.com/b")
        db2.close()
