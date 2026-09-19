"""Export the archive to JSONL / CSV / JSON for downstream analysis."""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import Config
from .database import Database
from .storage import Storage


def _rows_to_dicts(rows: Iterable[Any]) -> Iterator[dict[str, Any]]:
    for row in rows:
        yield {k: row[k] for k in row.keys()}


class Exporter:
    def __init__(self, config: Config, db: Database | None = None, logger: Any = None):
        self.config = config
        self.db = db or Database(config.database_path)
        self.storage = Storage(config.archive_path, config.compress_raw)
        self.log = logger
        self.out = config.exports_path
        self.out.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _jsonl(self, name: str, rows: Iterable[dict[str, Any]]) -> tuple[Path, int]:
        path = self.out / name
        count = 0
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                count += 1
        return path, count

    def _csv(self, name: str, rows: Iterable[dict[str, Any]],
             columns: list[str]) -> tuple[Path, int]:
        path = self.out / name
        count = 0
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k) for k in columns})
                count += 1
        return path, count

    # ------------------------------------------------------------------
    def export_all(self) -> dict[str, int]:
        results: dict[str, int] = {}

        pages = list(_rows_to_dicts(self.db.query(
            "SELECT p.*, "
            " (SELECT COUNT(*) FROM comments c WHERE c.page_id=p.id) AS comments_total,"
            " (SELECT COUNT(*) FROM asset_references ar WHERE ar.page_id=p.id) AS assets_total,"
            " (SELECT COUNT(*) FROM asset_references ar JOIN assets a ON a.id=ar.asset_id"
            "   WHERE ar.page_id=p.id AND a.asset_type='image') AS images_total,"
            " (SELECT COUNT(*) FROM asset_references ar JOIN assets a ON a.id=ar.asset_id"
            "   WHERE ar.page_id=p.id AND a.asset_type IN ('video','audio')) AS media_total"
            " FROM pages p ORDER BY p.id"
        )))
        for page in pages:
            page["headers"] = json.loads(page["headers_json"]) if page["headers_json"] else None
            page.pop("headers_json", None)
            page["redirect_chain"] = (json.loads(page["redirect_chain"])
                                      if page["redirect_chain"] else [])
        _, results["pages.jsonl"] = self._jsonl("pages.jsonl", pages)

        path = self.out / "pages.json"
        path.write_text(json.dumps(pages, indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8")
        results["pages.json"] = len(pages)

        _, results["pages.csv"] = self._csv("pages.csv", pages, [
            "id", "url", "normalized_url", "canonical_url", "final_url", "title",
            "page_type", "http_status", "content_type", "content_length", "sha256",
            "rendered", "render_reason", "depth", "text_length", "word_count",
            "link_count", "assets_total", "images_total", "media_total",
            "comments_total", "duplicate_of", "last_crawled", "local_dir",
        ])

        _, results["assets.jsonl"] = self._jsonl("assets.jsonl", _rows_to_dicts(self.db.query(
            "SELECT a.*, (SELECT COUNT(*) FROM asset_references r WHERE r.asset_id=a.id)"
            " AS reference_count FROM assets a ORDER BY a.id")))
        _, results["assets.csv"] = self._csv("assets.csv", list(_rows_to_dicts(self.db.query(
            "SELECT a.*, (SELECT COUNT(*) FROM asset_references r WHERE r.asset_id=a.id)"
            " AS reference_count FROM assets a ORDER BY a.id"))), [
            "id", "url", "normalized_url", "asset_type", "mime_type", "extension",
            "http_status", "byte_size", "sha256", "width", "height", "local_path",
            "reference_count", "error",
        ])

        _, results["asset_references.jsonl"] = self._jsonl(
            "asset_references.jsonl", _rows_to_dicts(self.db.query(
                "SELECT ar.*, a.normalized_url AS asset_url, a.sha256, a.local_path,"
                " a.asset_type FROM asset_references ar JOIN assets a ON a.id=ar.asset_id"
                " ORDER BY ar.page_id, ar.position")))

        _, results["links.jsonl"] = self._jsonl("links.jsonl", _rows_to_dicts(self.db.query(
            "SELECT * FROM links ORDER BY id")))

        comments = list(_rows_to_dicts(self.db.query(
            "SELECT id, page_id, page_url, comment_key, parent_key, author, author_url,"
            " published, rating, body, source, position FROM comments ORDER BY page_id, position")))
        _, results["comments.jsonl"] = self._jsonl("comments.jsonl", comments)
        _, results["comments.csv"] = self._csv("comments.csv", comments, [
            "id", "page_url", "comment_key", "parent_key", "author", "author_url",
            "published", "rating", "source", "position", "body",
        ])

        _, results["structured_data.jsonl"] = self._jsonl(
            "structured_data.jsonl", _rows_to_dicts(self.db.query(
                "SELECT id, page_id, page_url, kind, subtype, parsed_json, parse_error,"
                " position FROM structured_data ORDER BY page_id, position")))

        _, results["errors.jsonl"] = self._jsonl("errors.jsonl", _rows_to_dicts(self.db.query(
            "SELECT * FROM errors ORDER BY id")))

        _, results["urls.jsonl"] = self._jsonl("urls.jsonl", _rows_to_dicts(self.db.query(
            "SELECT * FROM urls ORDER BY id")))

        url_rows = self.db.query("SELECT normalized_url FROM urls ORDER BY normalized_url")
        path = self.out / "urls.txt"
        path.write_text("\n".join(r["normalized_url"] for r in url_rows) + "\n",
                        encoding="utf-8")
        results["urls.txt"] = len(url_rows)

        _, results["discovery_sources.jsonl"] = self._jsonl(
            "discovery_sources.jsonl", _rows_to_dicts(self.db.query(
                "SELECT * FROM discovery_sources ORDER BY id")))

        self.write_manifest()
        self.write_readme(results)
        return results

    # ------------------------------------------------------------------
    def write_manifest(self) -> Path:
        scalar = self.db.scalar
        config = self.db.get_meta("config")
        try:
            config = json.loads(config) if isinstance(config, str) else config
        except (json.JSONDecodeError, TypeError):
            pass
        manifest = {
            "crawl_version": 1,
            "archive_version": 1,
            "generator": "site_archiver",
            "target": self.config.roots,
            "crawl_start": self.db.get_meta("last_run_started"),
            "crawl_end": self.db.get_meta("last_run_finished"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "crawler_configuration": config,
            "total_urls": scalar("SELECT COUNT(*) FROM urls"),
            "completed_urls": scalar("SELECT COUNT(*) FROM urls WHERE status='COMPLETED'"),
            "failed_urls": scalar("SELECT COUNT(*) FROM urls WHERE status='FAILED'"),
            "skipped_urls": scalar("SELECT COUNT(*) FROM urls WHERE status='SKIPPED'"),
            "page_count": scalar("SELECT COUNT(*) FROM pages"),
            "asset_count": scalar("SELECT COUNT(*) FROM assets WHERE local_path IS NOT NULL"),
            "unique_asset_hashes": scalar(
                "SELECT COUNT(DISTINCT sha256) FROM assets WHERE sha256 IS NOT NULL"),
            "comment_count": scalar("SELECT COUNT(*) FROM comments"),
            "structured_data_count": scalar("SELECT COUNT(*) FROM structured_data"),
            "total_bytes": self.storage.disk_usage(),
            "database_path": str(self.config.database_path),
            "archive_path": str(self.config.archive_path),
            "exports_path": str(self.config.exports_path),
        }
        path = self.config.archive_path / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        (self.out / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        return path

    def write_readme(self, results: dict[str, int]) -> Path:
        lines = [
            "SITE ARCHIVE EXPORTS",
            "=" * 60,
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
            f"Target(s): {', '.join(self.config.roots)}",
            f"Database:  {self.config.database_path}",
            f"Archive:   {self.config.archive_path}",
            "",
            "FILES",
            "-" * 60,
        ]
        descriptions = {
            "pages.jsonl": "one JSON object per archived page (full metadata)",
            "pages.json": "same as pages.jsonl as a single JSON array",
            "pages.csv": "spreadsheet-friendly page index",
            "assets.jsonl": "every asset referenced, with hash/size/dimensions",
            "assets.csv": "spreadsheet-friendly asset index",
            "asset_references.jsonl": "which page references which asset, and how",
            "links.jsonl": "every link edge discovered (source -> destination)",
            "comments.jsonl": "every comment/review extracted",
            "comments.csv": "spreadsheet-friendly comment index",
            "structured_data.jsonl": "JSON-LD / OpenGraph / microdata / embedded JSON",
            "errors.jsonl": "every logged error with phase and message",
            "urls.jsonl": "the complete URL frontier with final status",
            "urls.txt": "plain list of every URL the crawler ever saw",
            "discovery_sources.jsonl": "how each URL was discovered",
            "manifest.json": "crawl-level summary",
            "validation.json": "validation results (run 'validate')",
            "missing.csv": "files recorded in the DB but absent/zero-byte on disk",
            "failed.csv": "URLs that could not be archived, with reasons",
            "missing_urls.txt": "discovered but not successfully archived",
            "duplicates.csv": "duplicate pages and assets by content hash",
            "skipped.csv": "URLs skipped, with the reason (robots, scope, depth)",
            "unclassified.csv": "pages the classifier could not type",
        }
        for name in sorted(set(list(results) + list(descriptions))):
            path = self.out / name
            if not path.exists():
                continue
            count = results.get(name)
            suffix = f"  [{count} rows]" if count is not None else ""
            lines.append(f"{name:28} {descriptions.get(name, '')}{suffix}")
        lines += [
            "",
            "ARCHIVE LAYOUT",
            "-" * 60,
            "archive/pages/<xx>/<hash>_<slug>/",
            "    raw.html          original HTTP response body (authoritative)",
            "    rendered.html     post-JavaScript DOM (only when rendering was needed)",
            "    text.txt          normalized, section-headed text export",
            "    metadata.json     status, headers, canonical, timings, discovery source",
            "    structured.json   parsed structured data",
            "    jsonld.raw.json   original JSON-LD, preserved verbatim",
            "    comments.json     extracted comments (structured)",
            "    comments.txt      extracted comments (readable)",
            "    comments_raw.html original comment-region markup",
            "    xhr/NNN.json      JSON the page fetched for itself",
            "archive/assets/<xx>/<yy>/<sha256>.<ext>   content-addressed assets",
            "archive/media/, archive/documents/        video/audio and documents",
            "archive/site/                             robots.txt and sitemaps",
            "",
            "Assets are content-addressed: identical bytes are stored once and",
            "asset_references.jsonl records every page that uses them.",
        ]
        path = self.out / "README.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
