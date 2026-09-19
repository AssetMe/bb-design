"""Command-line interface."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .config import Config, Mode
from .database import Database
from .engine import Engine, setup_logging
from .exporter import Exporter
from .mirror import MirrorBuilder
from .validator import Validator

DEFAULT_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Resumable website archival crawler.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python run.py crawl https://example.com/
  python run.py crawl --config config.json --resume
  python run.py crawl --resume                 # roots restored from the archive
  python run.py discover https://example.com/
  python run.py validate
  python run.py export
  python run.py mirror
  python run.py status
""",
    )
    parser.add_argument("command", nargs="?", default="crawl",
                        choices=["crawl", "discover", "download", "render", "validate",
                                 "export", "mirror", "status", "resume", "retry-failed",
                                 "requeue-skipped"],
                        help="what to do (default: crawl)")
    parser.add_argument("roots", nargs="*", help="one or more crawl roots")
    parser.add_argument("--config", "-c", help="path to a JSON/YAML config file")
    parser.add_argument("--output", "-o", help="archive directory")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--project-root", default=DEFAULT_PROJECT_ROOT,
                        help="base directory for relative paths")

    g = parser.add_argument_group("scope")
    g.add_argument("--max-depth", type=int)
    g.add_argument("--max-pages", type=int)
    g.add_argument("--max-assets", type=int)
    g.add_argument("--allow", action="append", dest="allow_patterns",
                   help="regex a URL must match (repeatable)")
    g.add_argument("--deny", action="append", dest="deny_patterns",
                   help="regex that excludes a URL (repeatable)")
    g.add_argument("--no-subdomains", dest="include_subdomains", action="store_false",
                   default=None)
    g.add_argument("--offsite-assets", dest="download_offsite_assets",
                   action="store_true", default=None,
                   help="also download assets hosted on other domains")

    g = parser.add_argument_group("politeness")
    g.add_argument("--concurrency", type=int)
    g.add_argument("--per-host-concurrency", type=int)
    g.add_argument("--delay", type=float, dest="min_delay")
    g.add_argument("--timeout", type=float)
    g.add_argument("--retries", type=int, dest="max_retries")
    g.add_argument("--user-agent", dest="user_agent")
    g.add_argument("--ignore-robots", dest="honor_robots", action="store_false",
                   default=None,
                   help="do not apply robots.txt rules (only for sites you operate)")

    g = parser.add_argument_group("rendering")
    g.add_argument("--no-render", dest="render", action="store_false", default=None)
    g.add_argument("--render-all", dest="render_always", action="store_true", default=None)
    g.add_argument("--render-timeout", type=float)
    g.add_argument("--browser", dest="browser_executable")

    g = parser.add_argument_group("behaviour")
    g.add_argument("--resume", action="store_true",
                   help="continue a previous crawl (this is the default)")
    g.add_argument("--force", action="store_true",
                   help="re-fetch URLs that already completed")
    g.add_argument("--no-sitemaps", dest="follow_sitemaps", action="store_false", default=None)
    g.add_argument("--no-comments", dest="extract_comments", action="store_false", default=None)
    g.add_argument("--no-verify-hashes", dest="verify_hashes", action="store_false",
                   default=True, help="validate: skip re-hashing every file")
    g.add_argument("--reason", help="requeue-skipped: only this skip_reason "
                                    "(e.g. out_of_scope, max_depth)")
    g.add_argument("--verbose", "-v", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    overrides = {
        k: v for k, v in vars(args).items()
        if v is not None and k in {
            "output_dir", "db_path", "max_depth", "max_pages", "max_assets",
            "allow_patterns", "deny_patterns", "include_subdomains",
            "download_offsite_assets", "concurrency", "per_host_concurrency",
            "min_delay", "timeout", "max_retries", "user_agent", "honor_robots",
            "render", "render_always", "render_timeout", "browser_executable",
            "follow_sitemaps", "extract_comments", "project_root",
        }
    }
    if args.output:
        overrides["output_dir"] = args.output
    if args.db:
        overrides["db_path"] = args.db
    if args.force:
        overrides["force"] = True

    config = Config.load(args.config, **overrides)
    if args.roots:
        config.roots = list(args.roots)
    if args.project_root:
        config.project_root = args.project_root
    if not config.db_path or config.db_path == Config.db_path:
        config.db_path = str(Path(config.output_dir) / "archive.db")
    return config


def _print_header(config: Config, command: str) -> None:
    print(f"site_archiver :: {command}")
    print(f"  roots       : {', '.join(config.roots) or '(none - using existing frontier)'}")
    print(f"  archive     : {config.archive_path}")
    print(f"  database    : {config.database_path}")
    print(f"  concurrency : {config.concurrency} global / {config.per_host_concurrency} per host"
          f", {config.min_delay}s min delay")
    print(f"  robots.txt  : {'honored' if config.honor_robots else 'IGNORED'}")
    print(f"  rendering   : {'on' if config.render else 'off'}"
          f"{' (forced for every page)' if config.render_always else ''}")
    print()


def cmd_crawl(config: Config, args: argparse.Namespace, mode: Mode) -> int:
    config.mode = mode
    _print_header(config, mode.value.lower())
    engine = Engine(config, verbose=args.verbose)
    try:
        stats = asyncio.run(engine.run())
    except KeyboardInterrupt:
        print("\nInterrupted - progress is saved. Re-run the same command to resume.")
        return 130
    finally:
        engine.close()
    print()
    print_stats(stats.to_dict())
    return 0


def print_stats(stats: dict) -> None:
    print("CRAWL SUMMARY")
    print("-" * 54)
    for key in ("urls_discovered", "urls_fetched", "urls_completed", "urls_failed",
                "urls_skipped", "urls_access_restricted", "pages_html", "pages_rendered",
                "pages_duplicate", "assets_downloaded", "assets_deduplicated",
                "assets_failed", "images", "media", "documents", "comments",
                "structured_items", "links_recorded", "sitemaps_parsed",
                "parse_problems", "errors", "megabytes_downloaded", "elapsed_seconds"):
        if key in stats:
            print(f"  {key:24} {stats[key]}")
    if stats.get("http_status_counts"):
        print(f"  {'http_status_counts':24} {stats['http_status_counts']}")


def cmd_validate(config: Config, args: argparse.Namespace) -> int:
    _print_header(config, "validate")
    validator = Validator(config)
    report = validator.run(verify_hashes=args.verify_hashes)
    written = validator.write_reports(report)
    data = report.to_dict()
    print("VALIDATION")
    print("-" * 54)
    print(f"  pages checked          {report.checked_pages}")
    print(f"  assets checked         {report.checked_assets}")
    for key, value in data["counts"].items():
        print(f"  {key:22} {value}")
    print()
    print("TOTALS")
    print("-" * 54)
    for key, value in data["totals"].items():
        print(f"  {key:22} {value}")
    print()
    print(f"  result: {'PASS' if report.ok else 'ISSUES FOUND'}")
    for name, path in written.items():
        print(f"  wrote {path}")
    return 0 if report.ok else 1


def cmd_export(config: Config, args: argparse.Namespace) -> int:
    _print_header(config, "export")
    exporter = Exporter(config)
    results = exporter.export_all()
    print("EXPORTS")
    print("-" * 54)
    for name, count in sorted(results.items()):
        print(f"  {name:28} {count} rows")
    print(f"  written to {config.exports_path}")
    return 0


def cmd_mirror(config: Config, args: argparse.Namespace) -> int:
    _print_header(config, "mirror")
    builder = MirrorBuilder(config)
    result = builder.build()
    print("MIRROR")
    print("-" * 54)
    for key, value in result.items():
        print(f"  {key:24} {value}")
    print(f"  written to {config.mirror_path}")
    return 0


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    if not config.database_path.exists():
        print(f"No database at {config.database_path} - nothing crawled yet.")
        return 1
    db = Database(config.database_path)
    print(f"database: {config.database_path}")
    print()
    print("FRONTIER")
    for row in db.query("SELECT status, kind, COUNT(*) n FROM urls GROUP BY status, kind"
                        " ORDER BY status, kind"):
        print(f"  {row['status']:12} {row['kind']:6} {row['n']}")
    print()
    print("CONTENT")
    for label, sql in (
        ("pages", "SELECT COUNT(*) FROM pages"),
        ("pages rendered", "SELECT COUNT(*) FROM pages WHERE rendered=1"),
        ("assets downloaded", "SELECT COUNT(*) FROM assets WHERE local_path IS NOT NULL"),
        ("unique asset hashes", "SELECT COUNT(DISTINCT sha256) FROM assets"),
        ("comments", "SELECT COUNT(*) FROM comments"),
        ("structured items", "SELECT COUNT(*) FROM structured_data"),
        ("links", "SELECT COUNT(*) FROM links"),
        ("errors", "SELECT COUNT(*) FROM errors"),
    ):
        print(f"  {label:22} {db.scalar(sql)}")
    stats = db.get_meta("stats")
    if stats:
        try:
            print()
            print("LAST RUN")
            print_stats(json.loads(stats) if isinstance(stats, str) else stats)
        except (json.JSONDecodeError, TypeError):
            pass
    db.close()
    return 0


def cmd_requeue_skipped(config: Config, args: argparse.Namespace) -> int:
    from .frontier import Frontier

    db = Database(config.database_path)
    frontier = Frontier(db, config.normalization_kwargs())
    n = frontier.requeue_skipped(args.reason)
    db.close()
    scope = f" with reason {args.reason!r}" if args.reason else ""
    print(f"Requeued {n} skipped URL(s){scope}. Run 'crawl' to process them.")
    return 0


def cmd_retry_failed(config: Config, args: argparse.Namespace) -> int:
    from .frontier import Frontier

    db = Database(config.database_path)
    frontier = Frontier(db, config.normalization_kwargs())
    n = frontier.requeue_failed()
    db.close()
    print(f"Requeued {n} failed URL(s). Run 'crawl' to retry them.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = config_from_args(args)

    if args.command in ("crawl", "resume") and not config.roots:
        db_path = config.database_path
        if not db_path.exists():
            parser.error("no roots given and no existing database to resume")

    handlers = {
        "crawl": lambda: cmd_crawl(config, args, Mode.FULL),
        "resume": lambda: cmd_crawl(config, args, Mode.FULL),
        "discover": lambda: cmd_crawl(config, args, Mode.DISCOVERY_ONLY),
        "download": lambda: cmd_crawl(config, args, Mode.DOWNLOAD_ONLY),
        "render": lambda: cmd_crawl(config, args, Mode.RENDER),
        "validate": lambda: cmd_validate(config, args),
        "export": lambda: cmd_export(config, args),
        "mirror": lambda: cmd_mirror(config, args),
        "status": lambda: cmd_status(config, args),
        "retry-failed": lambda: cmd_retry_failed(config, args),
        "requeue-skipped": lambda: cmd_requeue_skipped(config, args),
    }
    if args.command == "render":
        config.render_always = True
    return handlers[args.command]()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
