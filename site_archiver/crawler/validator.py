"""Post-crawl validation: does the database actually match what is on disk?"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .database import Database
from .storage import Storage, sha256_file


@dataclass
class ValidationReport:
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    checked_pages: int = 0
    checked_assets: int = 0
    missing_files: list[dict[str, Any]] = field(default_factory=list)
    zero_byte_files: list[dict[str, Any]] = field(default_factory=list)
    hash_mismatches: list[dict[str, Any]] = field(default_factory=list)
    failed_urls: list[dict[str, Any]] = field(default_factory=list)
    unprocessed_urls: list[dict[str, Any]] = field(default_factory=list)
    skipped_urls: list[dict[str, Any]] = field(default_factory=list)
    duplicate_pages: list[dict[str, Any]] = field(default_factory=list)
    duplicate_assets: list[dict[str, Any]] = field(default_factory=list)
    broken_references: list[dict[str, Any]] = field(default_factory=list)
    unclassified_pages: list[dict[str, Any]] = field(default_factory=list)
    totals: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not (self.missing_files or self.zero_byte_files or self.hash_mismatches
                    or self.broken_references)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round((self.finished_at or time.time()) - self.started_at, 2),
            "checked_pages": self.checked_pages,
            "checked_assets": self.checked_assets,
            "counts": {
                "missing_files": len(self.missing_files),
                "zero_byte_files": len(self.zero_byte_files),
                "hash_mismatches": len(self.hash_mismatches),
                "failed_urls": len(self.failed_urls),
                "unprocessed_urls": len(self.unprocessed_urls),
                "skipped_urls": len(self.skipped_urls),
                "duplicate_pages": len(self.duplicate_pages),
                "duplicate_assets": len(self.duplicate_assets),
                "broken_references": len(self.broken_references),
                "unclassified_pages": len(self.unclassified_pages),
            },
            "totals": self.totals,
            "missing_files": self.missing_files[:500],
            "zero_byte_files": self.zero_byte_files[:500],
            "hash_mismatches": self.hash_mismatches[:500],
            "broken_references": self.broken_references[:500],
        }


class Validator:
    def __init__(self, config: Config, db: Database | None = None, logger: Any = None):
        self.config = config
        self.db = db or Database(config.database_path)
        self.storage = Storage(config.archive_path, config.compress_raw)
        self.log = logger

    def _abs(self, rel: str | None) -> Path | None:
        if not rel:
            return None
        p = Path(rel)
        return p if p.is_absolute() else self.storage.root / p

    def run(self, verify_hashes: bool = True) -> ValidationReport:
        report = ValidationReport()

        # --- pages ------------------------------------------------------
        for row in self.db.query(
            "SELECT id, normalized_url, raw_path, rendered_path, text_path, sha256,"
            " page_type, duplicate_of, content_length FROM pages"
        ):
            report.checked_pages += 1
            for label in ("raw_path", "rendered_path", "text_path"):
                rel = row[label]
                if not rel:
                    continue
                path = self._abs(rel)
                if path is None or not path.exists():
                    report.missing_files.append(
                        {"kind": "page", "url": row["normalized_url"],
                         "field": label, "path": rel})
                    continue
                if path.stat().st_size == 0:
                    report.zero_byte_files.append(
                        {"kind": "page", "url": row["normalized_url"],
                         "field": label, "path": rel})
            if verify_hashes and row["sha256"] and row["raw_path"]:
                path = self._abs(row["raw_path"])
                if path and path.exists():
                    try:
                        if path.suffix == ".gz":
                            import hashlib
                            actual = hashlib.sha256(self.storage.read_bytes(path)).hexdigest()
                        else:
                            actual = sha256_file(path)
                        if actual != row["sha256"]:
                            report.hash_mismatches.append({
                                "kind": "page", "url": row["normalized_url"],
                                "expected": row["sha256"], "actual": actual,
                                "path": row["raw_path"]})
                    except OSError as exc:
                        report.missing_files.append(
                            {"kind": "page", "url": row["normalized_url"],
                             "field": "raw_path", "path": row["raw_path"],
                             "error": str(exc)})
            if row["duplicate_of"]:
                report.duplicate_pages.append(
                    {"url": row["normalized_url"], "duplicate_of_page_id": row["duplicate_of"]})
            if not row["page_type"]:
                report.unclassified_pages.append(
                    {"url": row["normalized_url"], "page_type": row["page_type"],
                     "reason": "no page type assigned"})

        # --- assets -----------------------------------------------------
        by_hash: dict[str, list[str]] = defaultdict(list)
        for row in self.db.query(
            "SELECT id, normalized_url, local_path, sha256, byte_size, error FROM assets"
        ):
            if not row["local_path"]:
                if row["error"]:
                    report.failed_urls.append(
                        {"kind": "asset", "url": row["normalized_url"], "error": row["error"]})
                else:
                    report.unprocessed_urls.append(
                        {"kind": "asset", "url": row["normalized_url"],
                         "reason": "referenced but never downloaded"})
                continue
            report.checked_assets += 1
            path = self._abs(row["local_path"])
            if path is None or not path.exists():
                report.missing_files.append(
                    {"kind": "asset", "url": row["normalized_url"], "path": row["local_path"]})
                continue
            size = path.stat().st_size
            if size == 0:
                report.zero_byte_files.append(
                    {"kind": "asset", "url": row["normalized_url"], "path": row["local_path"]})
            if row["byte_size"] is not None and size != row["byte_size"]:
                report.hash_mismatches.append({
                    "kind": "asset", "url": row["normalized_url"],
                    "expected_size": row["byte_size"], "actual_size": size,
                    "path": row["local_path"]})
            elif verify_hashes and row["sha256"]:
                actual = sha256_file(path)
                if actual != row["sha256"]:
                    report.hash_mismatches.append({
                        "kind": "asset", "url": row["normalized_url"],
                        "expected": row["sha256"], "actual": actual,
                        "path": row["local_path"]})
            if row["sha256"]:
                by_hash[row["sha256"]].append(row["normalized_url"])

        for digest, urls in by_hash.items():
            if len(urls) > 1:
                report.duplicate_assets.append({"sha256": digest, "urls": urls,
                                                "count": len(urls)})

        # --- frontier ---------------------------------------------------
        for row in self.db.query(
            "SELECT normalized_url, status, http_status, last_error, attempt_count,"
            " skip_reason, kind, discovery_source FROM urls"
            " WHERE status IN ('FAILED','SKIPPED','DISCOVERED','QUEUED','FETCHING')"
        ):
            record = {
                "url": row["normalized_url"], "kind": row["kind"], "status": row["status"],
                "http_status": row["http_status"], "error": row["last_error"],
                "attempts": row["attempt_count"], "skip_reason": row["skip_reason"],
                "discovery_source": row["discovery_source"],
            }
            if row["status"] == "FAILED":
                report.failed_urls.append(record)
            elif row["status"] == "SKIPPED":
                report.skipped_urls.append(record)
            else:
                report.unprocessed_urls.append(record)

        # --- referential integrity --------------------------------------
        for row in self.db.query(
            "SELECT ar.id, ar.page_url, ar.asset_id FROM asset_references ar"
            " LEFT JOIN assets a ON a.id = ar.asset_id WHERE a.id IS NULL"
        ):
            report.broken_references.append(
                {"kind": "asset_reference", "page_url": row["page_url"],
                 "asset_id": row["asset_id"]})
        for row in self.db.query(
            "SELECT c.id, c.page_url FROM comments c"
            " LEFT JOIN pages p ON p.id = c.page_id WHERE c.page_id IS NOT NULL AND p.id IS NULL"
        ):
            report.broken_references.append(
                {"kind": "comment", "page_url": row["page_url"]})

        # --- totals -----------------------------------------------------
        scalar = self.db.scalar
        report.totals = {
            "urls_total": scalar("SELECT COUNT(*) FROM urls"),
            "urls_completed": scalar("SELECT COUNT(*) FROM urls WHERE status='COMPLETED'"),
            "urls_failed": scalar("SELECT COUNT(*) FROM urls WHERE status='FAILED'"),
            "urls_skipped": scalar("SELECT COUNT(*) FROM urls WHERE status='SKIPPED'"),
            "urls_pending": scalar(
                "SELECT COUNT(*) FROM urls WHERE status IN ('DISCOVERED','QUEUED','FETCHING')"),
            "pages": scalar("SELECT COUNT(*) FROM pages"),
            "pages_rendered": scalar("SELECT COUNT(*) FROM pages WHERE rendered=1"),
            "pages_generic_type": scalar("SELECT COUNT(*) FROM pages WHERE page_type='OTHER'"),
            "pages_with_comments": scalar(
                "SELECT COUNT(DISTINCT page_id) FROM comments"),
            "pages_with_images": scalar(
                "SELECT COUNT(DISTINCT ar.page_id) FROM asset_references ar"
                " JOIN assets a ON a.id=ar.asset_id WHERE a.asset_type='image'"),
            "comments": scalar("SELECT COUNT(*) FROM comments"),
            "assets_referenced": scalar("SELECT COUNT(*) FROM assets"),
            "assets_downloaded": scalar(
                "SELECT COUNT(*) FROM assets WHERE local_path IS NOT NULL"),
            "assets_unique_hashes": scalar(
                "SELECT COUNT(DISTINCT sha256) FROM assets WHERE sha256 IS NOT NULL"),
            "images": scalar("SELECT COUNT(*) FROM assets WHERE asset_type='image'"),
            "media": scalar(
                "SELECT COUNT(*) FROM assets WHERE asset_type IN ('video','audio')"),
            "documents": scalar("SELECT COUNT(*) FROM assets WHERE asset_type='document'"),
            "links": scalar("SELECT COUNT(*) FROM links"),
            "structured_items": scalar("SELECT COUNT(*) FROM structured_data"),
            "errors": scalar("SELECT COUNT(*) FROM errors"),
            "bytes_on_disk": self.storage.disk_usage(),
        }
        report.finished_at = time.time()
        return report

    def write_reports(self, report: ValidationReport) -> dict[str, Path]:
        import csv

        out = self.config.exports_path
        out.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}

        def write_csv(name: str, rows: list[dict[str, Any]], columns: list[str]) -> None:
            path = out / name
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
            written[name] = path

        write_csv("missing.csv", report.missing_files + report.zero_byte_files,
                  ["kind", "url", "field", "path", "error"])
        write_csv("failed.csv", report.failed_urls,
                  ["kind", "url", "status", "http_status", "error", "attempts",
                   "discovery_source"])
        write_csv("duplicates.csv",
                  [{"kind": "asset", "sha256": d["sha256"], "count": d["count"],
                    "urls": " ".join(d["urls"])} for d in report.duplicate_assets] +
                  [{"kind": "page", "sha256": "", "count": 2,
                    "urls": d["url"]} for d in report.duplicate_pages],
                  ["kind", "sha256", "count", "urls"])
        write_csv("skipped.csv", report.skipped_urls,
                  ["kind", "url", "skip_reason", "discovery_source"])
        write_csv("unclassified.csv", report.unclassified_pages, ["url", "page_type"])

        path = out / "validation.json"
        path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
        written["validation.json"] = path

        # The headline completeness check: discovered but not archived.
        missing_urls = [
            r["url"] for r in (report.failed_urls + report.unprocessed_urls)
            if r.get("kind") != "asset" or True
        ]
        path = out / "missing_urls.txt"
        path.write_text("\n".join(sorted(set(missing_urls))) + ("\n" if missing_urls else ""),
                        encoding="utf-8")
        written["missing_urls.txt"] = path
        return written
