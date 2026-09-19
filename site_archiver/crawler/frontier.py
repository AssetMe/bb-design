"""Persistent URL frontier backed by SQLite.

All crawl progress lives in the database, never only in RAM, so a crash or
Ctrl+C loses at most the URLs currently in flight (which are reset to QUEUED
on the next start).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .database import Database
from .urlnorm import try_normalize

PAGE, ASSET = "page", "asset"

ACTIVE_STATUSES = ("DISCOVERED", "QUEUED")
TERMINAL_STATUSES = ("COMPLETED", "FAILED", "SKIPPED")


@dataclass(slots=True)
class FrontierItem:
    id: int
    url: str
    normalized_url: str
    kind: str
    depth: int
    parent_url: str | None
    discovery_source: str | None
    attempt_count: int

    @classmethod
    def from_row(cls, row: Any) -> "FrontierItem":
        return cls(
            id=row["id"],
            url=row["url"],
            normalized_url=row["normalized_url"],
            kind=row["kind"],
            depth=row["depth"],
            parent_url=row["parent_url"],
            discovery_source=row["discovery_source"],
            attempt_count=row["attempt_count"],
        )


class Frontier:
    def __init__(self, db: Database, norm_kwargs: dict[str, Any] | None = None):
        self.db = db
        self.norm_kwargs = norm_kwargs or {}
        #: Called with the normalized URL the first time it enters the frontier.
        self.on_new: Callable[[str], None] | None = None

    # -- seeding --------------------------------------------------------
    def add(
        self,
        url: str,
        *,
        kind: str = PAGE,
        depth: int = 0,
        parent_url: str | None = None,
        source: str | None = None,
        base: str | None = None,
        detail: str | None = None,
    ) -> str | None:
        """Register a URL.  Returns the normalized URL, or None if unusable.

        Re-adding a known URL only refreshes ``last_seen`` - it never resets a
        completed resource back into the queue.
        """
        normalized = try_normalize(url, base, **self.norm_kwargs)
        if not normalized:
            return None
        is_new = not self.is_known(normalized)
        now = time.time()
        priority = 0 if kind == PAGE else 1
        self.db.execute(
            "INSERT INTO urls(url, normalized_url, kind, priority, status, depth,"
            " parent_url, discovery_source, first_seen, last_seen)"
            " VALUES(?,?,?,?,'DISCOVERED',?,?,?,?,?)"
            " ON CONFLICT(normalized_url) DO UPDATE SET"
            "   last_seen=excluded.last_seen,"
            "   depth=MIN(urls.depth, excluded.depth)",
            (url, normalized, kind, priority, depth, parent_url, source, now, now),
        )
        if source:
            self.db.record_discovery(normalized, source, parent_url, detail)
        if is_new and self.on_new is not None:
            self.on_new(normalized)
        return normalized

    def add_many(self, urls: Iterable[str], **kwargs: Any) -> list[str]:
        return [n for n in (self.add(u, **kwargs) for u in urls) if n]

    # -- claiming -------------------------------------------------------
    def claim(self) -> FrontierItem | None:
        """Atomically take the next unit of work, or None when idle."""
        with self.db.transaction() as cur:
            row = cur.execute(
                "SELECT * FROM urls WHERE status IN ('DISCOVERED','QUEUED')"
                " ORDER BY priority ASC, depth ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur.execute(
                "UPDATE urls SET status='FETCHING', attempt_count=attempt_count+1,"
                " last_seen=? WHERE id=?",
                (time.time(), row["id"]),
            )
            item = FrontierItem.from_row(row)
            item.attempt_count = row["attempt_count"] + 1
            return item

    def release(self, item: FrontierItem) -> None:
        """Return an in-flight item to the queue (clean shutdown)."""
        self.db.execute(
            "UPDATE urls SET status='QUEUED' WHERE id=? AND status='FETCHING'", (item.id,)
        )

    def reset_stale(self) -> int:
        """Requeue anything left FETCHING by a previous crashed run."""
        cur = self.db.execute(
            "UPDATE urls SET status='QUEUED' WHERE status='FETCHING'"
        )
        return cur.rowcount or 0

    # -- completion -----------------------------------------------------
    def complete(
        self,
        item: FrontierItem,
        *,
        http_status: int | None = None,
        content_type: str | None = None,
        content_hash: str | None = None,
        final_url: str | None = None,
        canonical_url: str | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE urls SET status='COMPLETED', http_status=?, content_type=?,"
            " content_hash=?, final_url=?, canonical_url=?, last_error=NULL,"
            " last_seen=? WHERE id=?",
            (http_status, content_type, content_hash, final_url, canonical_url,
             time.time(), item.id),
        )

    def fail(self, item: FrontierItem, error: str, *, http_status: int | None = None,
             permanent: bool = False, max_retries: int = 4) -> bool:
        """Mark a failure.  Returns True when the URL was requeued for retry."""
        retry = (not permanent) and item.attempt_count < max_retries
        status = "QUEUED" if retry else "FAILED"
        self.db.execute(
            "UPDATE urls SET status=?, last_error=?, http_status=?, last_seen=? WHERE id=?",
            (status, error[:2000], http_status, time.time(), item.id),
        )
        return retry

    def skip(self, item: FrontierItem, reason: str) -> None:
        self.db.execute(
            "UPDATE urls SET status='SKIPPED', skip_reason=?, last_seen=? WHERE id=?",
            (reason[:500], time.time(), item.id),
        )

    def mark_queued_all(self) -> None:
        self.db.execute("UPDATE urls SET status='QUEUED' WHERE status='DISCOVERED'")

    def force_requeue_completed(self) -> int:
        cur = self.db.execute(
            "UPDATE urls SET status='QUEUED', attempt_count=0 WHERE status IN ('COMPLETED','FAILED')"
        )
        return cur.rowcount or 0

    def requeue_skipped(self, reason: str | None = None) -> int:
        """Requeue SKIPPED URLs, optionally only those skipped for one reason.

        Useful after fixing a scope misconfiguration; robots.txt skips are
        re-evaluated against robots.txt again on the next attempt.
        """
        if reason:
            cur = self.db.execute(
                "UPDATE urls SET status='QUEUED', attempt_count=0, skip_reason=NULL"
                " WHERE status='SKIPPED' AND skip_reason=?", (reason,))
        else:
            cur = self.db.execute(
                "UPDATE urls SET status='QUEUED', attempt_count=0, skip_reason=NULL"
                " WHERE status='SKIPPED'")
        return cur.rowcount or 0

    def requeue_failed(self) -> int:
        cur = self.db.execute(
            "UPDATE urls SET status='QUEUED', attempt_count=0 WHERE status='FAILED'"
        )
        return cur.rowcount or 0

    # -- introspection --------------------------------------------------
    def is_known(self, normalized_url: str) -> bool:
        return self.db.query_one(
            "SELECT 1 FROM urls WHERE normalized_url=?", (normalized_url,)
        ) is not None

    def is_done(self, normalized_url: str) -> bool:
        row = self.db.query_one(
            "SELECT status FROM urls WHERE normalized_url=?", (normalized_url,)
        )
        return bool(row) and row["status"] in TERMINAL_STATUSES

    def pending_count(self) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM urls WHERE status IN ('DISCOVERED','QUEUED')"
        ))

    def counts_by_status(self) -> dict[str, int]:
        return {
            r["status"]: r["n"]
            for r in self.db.query("SELECT status, COUNT(*) AS n FROM urls GROUP BY status")
        }

    def counts_by_kind(self) -> dict[str, int]:
        return {
            r["kind"]: r["n"]
            for r in self.db.query("SELECT kind, COUNT(*) AS n FROM urls GROUP BY kind")
        }

    def seen_content_hash(self, digest: str, exclude_id: int | None = None) -> str | None:
        """Return an already-archived URL with the same body hash, if any."""
        row = self.db.query_one(
            "SELECT normalized_url FROM urls WHERE content_hash=? AND id<>? AND status='COMPLETED'"
            " ORDER BY id LIMIT 1",
            (digest, exclude_id or -1),
        )
        return row["normalized_url"] if row else None
