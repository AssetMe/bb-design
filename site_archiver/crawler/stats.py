"""Live crawl statistics, persisted to stats.json."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Stats:
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    urls_discovered: int = 0
    urls_fetched: int = 0
    urls_completed: int = 0
    urls_failed: int = 0
    urls_skipped: int = 0
    urls_access_restricted: int = 0
    pages_html: int = 0
    pages_json: int = 0
    pages_other: int = 0
    pages_rendered: int = 0
    pages_duplicate: int = 0
    assets_downloaded: int = 0
    assets_deduplicated: int = 0
    assets_failed: int = 0
    images: int = 0
    media: int = 0
    documents: int = 0
    comments: int = 0
    structured_items: int = 0
    links_recorded: int = 0
    bytes_downloaded: int = 0
    http_requests: int = 0
    retries: int = 0
    parse_problems: int = 0
    errors: int = 0
    sitemaps_parsed: int = 0
    robots_fetched: int = 0
    browser_restarts: int = 0
    http_status_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def bump(self, field_name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, field_name, getattr(self, field_name) + amount)

    def record_status(self, status: int | None) -> None:
        if status is None:
            return
        with self._lock:
            key = str(status)
            self.http_status_counts[key] = self.http_status_counts.get(key, 0) + 1

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        d["elapsed_seconds"] = round(self.elapsed, 2)
        rate = self.urls_completed / self.elapsed if self.elapsed > 0 else 0.0
        d["urls_per_second"] = round(rate, 3)
        d["megabytes_downloaded"] = round(self.bytes_downloaded / 1_048_576, 2)
        return d

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)
