#!/usr/bin/env python3
"""Measure crawl throughput, memory and database growth against the fixtures."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.config import Config
from crawler.database import Database
from crawler.engine import Engine

BASE = "http://127.0.0.1:8899/"


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def run_once(root: Path, *, concurrency: int, render: bool, delay: float) -> dict:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    cfg = Config(roots=[BASE], project_root=str(root), min_delay=delay,
                 concurrency=concurrency, per_host_concurrency=max(1, concurrency // 2),
                 render=render)
    cfg.ensure_dirs()
    started = time.monotonic()
    engine = Engine(cfg)
    stats = asyncio.run(engine.run())
    engine.close()
    elapsed = time.monotonic() - started
    db_bytes = cfg.database_path.stat().st_size
    return {
        "concurrency": concurrency,
        "render": render,
        "min_delay": delay,
        "elapsed_seconds": round(elapsed, 2),
        "urls_completed": stats.urls_completed,
        "pages": stats.pages_html,
        "assets": stats.assets_downloaded,
        "urls_per_second": round(stats.urls_completed / elapsed, 2),
        "pages_per_second": round(stats.pages_html / elapsed, 2),
        "assets_per_second": round(stats.assets_downloaded / elapsed, 2),
        "http_requests": stats.http_requests,
        "bytes_downloaded": stats.bytes_downloaded,
        "archive_bytes": dir_size(cfg.archive_path),
        "database_bytes": db_bytes,
        "database_bytes_per_url": round(db_bytes / max(1, stats.urls_completed)),
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None, help="write results as JSON here")
    args = parser.parse_args()

    root = Path("/tmp/site_archiver_bench")
    results = []
    for concurrency, render in ((1, False), (4, False), (8, False), (4, True)):
        result = run_once(root / f"c{concurrency}{'r' if render else ''}",
                          concurrency=concurrency, render=render, delay=0.0)
        results.append(result)
        print(f"concurrency={concurrency:<2} render={str(render):<5} "
              f"{result['elapsed_seconds']:>6.2f}s  "
              f"{result['urls_per_second']:>6.2f} urls/s  "
              f"{result['pages_per_second']:>5.2f} pages/s  "
              f"rss={result['peak_rss_mb']}MB  "
              f"db={result['database_bytes'] / 1024:.0f}KiB "
              f"({result['database_bytes_per_url']}B/url)")

    # Resume cost: a second run over a fully completed archive should be cheap.
    resume_root = root / "c4"
    cfg = Config(roots=[BASE], project_root=str(resume_root), min_delay=0.0,
                 concurrency=4, render=False)
    started = time.monotonic()
    engine = Engine(cfg)
    asyncio.run(engine.run())
    engine.close()
    resume_seconds = round(time.monotonic() - started, 2)
    print(f"resume over a complete archive: {resume_seconds}s (no work to redo)")
    results.append({"resume_noop_seconds": resume_seconds})

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"wrote {args.out}")
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
