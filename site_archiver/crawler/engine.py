"""Crawl orchestration: discovery, fetching, rendering, extraction, storage."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .browser import Renderer
from .config import Config, Mode
from .database import Database
from .discovery import RobotsPolicy, parse_sitemap, robots_url_for
from .fetcher import Fetcher, FetchResult
from .frontier import ASSET, PAGE, Frontier, FrontierItem
from .media import extract_assets, image_dimensions, urls_from_css
from .parser import (
    ExtractedComment,
    ParseResult,
    classify_page,
    comments_from_structured,
    links_from_structured,
    make_soup,
    needs_rendering,
    parse_html,
)
from .stats import Stats
from .storage import Storage, classify_asset, guess_extension, sha256_bytes
from .urlnorm import Scope, host_of, try_normalize

LOG = logging.getLogger("archiver")


def setup_logging(log_dir: Path, verbose: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("archiver")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_dir / "crawler.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    eh = logging.FileHandler(log_dir / "errors.log", encoding="utf-8")
    eh.setLevel(logging.WARNING)
    eh.setFormatter(fmt)
    logger.addHandler(eh)

    sh = logging.StreamHandler()
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(sh)
    logger.propagate = False
    return logger


@dataclass(slots=True)
class PageRecord:
    page_id: int
    normalized_url: str


class Engine:
    def __init__(self, config: Config, verbose: bool = False):
        self.config = config
        config.ensure_dirs()
        self.log = setup_logging(config.logs_path, verbose)
        self.db = Database(config.database_path)
        # Resuming without repeating the roots must not lose the crawl scope:
        # an empty scope would mark every pending URL out-of-scope.
        if not config.roots:
            stored = self.db.get_meta("roots")
            if stored:
                try:
                    config.roots = json.loads(stored)
                    self.log.info("Resuming with stored root(s): %s",
                                  ", ".join(config.roots))
                except (json.JSONDecodeError, TypeError):
                    pass
        self.storage = Storage(config.archive_path, config.compress_raw)
        self.frontier = Frontier(self.db, config.normalization_kwargs())
        self.scope = Scope.from_roots(
            config.roots,
            include_subdomains=config.include_subdomains,
            allow_patterns=config.allow_patterns,
            deny_patterns=config.deny_patterns,
        )
        if not self.scope.allowed_hosts:
            raise ValueError(
                "no crawl scope: pass at least one root URL, or point --db at an "
                "existing archive whose roots can be restored"
            )
        self.stats = Stats()
        self.frontier.on_new = lambda _url: self.stats.bump("urls_discovered")
        self.renderer = Renderer(config, self.log)
        self._robots: dict[str, RobotsPolicy] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._stop = asyncio.Event()
        self._active = 0
        self._processed = 0
        self._inflight: set[int] = set()
        self._fetcher: Fetcher | None = None

    # ==================================================================
    # lifecycle
    # ==================================================================
    async def run(self) -> Stats:
        cfg = self.config
        self.db.set_meta("config", cfg.to_dict())
        self.db.set_meta("roots", cfg.roots)
        self.db.set_meta("last_run_started", time.time())
        requeued = self.frontier.reset_stale()
        if requeued:
            self.log.info("Requeued %d URL(s) left in flight by a previous run", requeued)
        if cfg.force:
            n = self.frontier.force_requeue_completed()
            self.log.info("--force: requeued %d completed/failed URL(s)", n)

        self._install_signal_handlers()

        async with Fetcher(cfg, self.log) as fetcher:
            self._fetcher = fetcher
            for root in cfg.roots:
                normalized = self.frontier.add(root, source="root", depth=0)
                if normalized:
                    self.log.info("Root: %s", normalized)

            if cfg.follow_sitemaps:
                await self._discovery_phase()

            if cfg.mode is Mode.DISCOVERY_ONLY:
                self.log.info("DISCOVERY_ONLY: %d URL(s) in frontier",
                              self.frontier.pending_count())
            else:
                self.frontier.mark_queued_all()
                await self._crawl_phase()

        await self.renderer.stop()
        self.stats.browser_restarts = self.renderer.restarts
        self.stats.pages_rendered = self.db.scalar(
            "SELECT COUNT(*) FROM pages WHERE rendered=1")
        self.stats.finished_at = time.time()
        self.stats.write(cfg.logs_path / "statistics.json")
        self.db.set_meta("last_run_finished", time.time())
        self.db.set_meta("stats", self.stats.to_dict())
        return self.stats

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self._request_stop)

    def _stop_for_limit(self, item: FrontierItem, reason: str) -> None:
        """Hand the item back and wind the crawl down cleanly."""
        self.frontier.release(item)
        if not self._stop.is_set():
            self.log.info("%s - stopping; re-run to continue from here", reason)
            self._stop.set()

    def _request_stop(self) -> None:
        if not self._stop.is_set():
            self.log.warning("Shutdown requested - finishing in-flight work, "
                             "state is already on disk (re-run with 'crawl --resume')")
            self._stop.set()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.db.close()

    # ==================================================================
    # discovery: robots + sitemaps
    # ==================================================================
    async def _discovery_phase(self) -> None:
        hosts: list[str] = []
        for root in self.config.roots:
            host = host_of(root)
            if host and host not in hosts:
                hosts.append(host)

        sitemap_urls: list[str] = []
        for root in self.config.roots:
            policy = await self._get_robots(root)
            for sm in policy.sitemaps:
                if sm not in sitemap_urls:
                    sitemap_urls.append(sm)
            parts = urlsplit(root)
            base = f"{parts.scheme}://{parts.netloc}"
            for path in self.config.sitemap_candidates:
                candidate = base + path
                if candidate not in sitemap_urls:
                    sitemap_urls.append(candidate)
            # A root that is itself a sitemap.
            if root.endswith((".xml", ".xml.gz", "sitemap.txt")) and root not in sitemap_urls:
                sitemap_urls.insert(0, root)

        seen: set[str] = set()
        queue = list(sitemap_urls)
        found = 0
        while queue and not self._stop.is_set():
            url = queue.pop(0)
            normalized = try_normalize(url, **self.config.normalization_kwargs())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result = await self._fetch_sitemap(normalized)
            if result is None:
                continue
            self.stats.bump("sitemaps_parsed")
            for child in result.sitemaps:
                queue.append(child)
                self.db.record_discovery(child, "sitemap_index", normalized)
            for entry in result.entries:
                added = self.frontier.add(
                    entry.loc, source="sitemap", parent_url=normalized,
                    detail=entry.lastmod, depth=1,
                )
                if added:
                    found += 1
                for image_url in entry.images:
                    if self._asset_in_scope(image_url):
                        self.frontier.add(image_url, kind=ASSET, source="sitemap_image",
                                          parent_url=normalized, depth=2)
                for video_url in entry.videos:
                    if self._asset_in_scope(video_url):
                        self.frontier.add(video_url, kind=ASSET, source="sitemap_video",
                                          parent_url=normalized, depth=2)
            if result.error:
                self.log.warning("sitemap %s: %s", normalized, result.error)
        if found:
            self.log.info("Sitemap discovery: %d URL(s) from %d sitemap(s)",
                          found, self.stats.sitemaps_parsed)
        else:
            self.log.info("Sitemap discovery: no sitemap URLs found")

    async def _fetch_sitemap(self, url: str):
        assert self._fetcher is not None
        try:
            result = await self._fetcher.fetch(url)
        except Exception as exc:
            self.log.debug("sitemap fetch failed %s: %s", url, exc)
            return None
        self.stats.record_status(result.status)
        if not result.ok or not result.content:
            return None
        ctype = result.content_type
        if ctype and not any(t in ctype for t in ("xml", "text", "gzip", "octet-stream")):
            return None
        self.storage.store_site_file(
            Path(urlsplit(url).path).name or "sitemap.xml", result.content)
        parsed = parse_sitemap(result.content, url, ctype)
        if parsed.kind == "unknown" and not parsed.entries:
            return None
        self.log.info("sitemap %s -> %s (%d urls, %d children)",
                      url, parsed.kind, len(parsed.entries), len(parsed.sitemaps))
        return parsed

    async def _get_robots(self, url: str) -> RobotsPolicy:
        host = host_of(url)
        if host in self._robots:
            return self._robots[host]
        lock = self._robots_locks.setdefault(host, asyncio.Lock())
        async with lock:
            if host in self._robots:
                return self._robots[host]
            robots_url = robots_url_for(url)
            assert self._fetcher is not None
            try:
                result = await self._fetcher.fetch(robots_url, max_bytes=2_000_000)
                body = result.text() if result.content else ""
                policy = RobotsPolicy.parse(host, robots_url, result.status, body,
                                            self.config.user_agent)
                self.storage.store_site_file(f"robots-{host}.txt", result.content)
                self.db.execute(
                    "INSERT INTO robots(host, url, http_status, body, crawl_delay, fetched_at)"
                    " VALUES(?,?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET"
                    " http_status=excluded.http_status, body=excluded.body,"
                    " crawl_delay=excluded.crawl_delay, fetched_at=excluded.fetched_at",
                    (host, robots_url, result.status, body, policy.crawl_delay, time.time()),
                )
                self.stats.bump("robots_fetched")
                self.log.info("robots.txt %s -> HTTP %s (%d sitemap directive(s)%s)",
                              host, result.status, len(policy.sitemaps),
                              f", crawl-delay {policy.crawl_delay}s" if policy.crawl_delay else "")
            except Exception as exc:
                self.log.warning("robots.txt unavailable for %s: %s", host, exc)
                policy = RobotsPolicy.permissive(host, robots_url, str(exc))
            if policy.crawl_delay and self.config.respect_crawl_delay and self._fetcher:
                self._fetcher.set_host_delay(host, policy.crawl_delay)
            self._robots[host] = policy
            return policy

    async def _robots_allows(self, url: str) -> bool:
        if not self.config.honor_robots:
            return True
        policy = await self._get_robots(url)
        return policy.can_fetch(url, self.config.user_agent)

    # ==================================================================
    # crawl loop
    # ==================================================================
    async def _crawl_phase(self) -> None:
        workers = [
            asyncio.create_task(self._worker(i))
            for i in range(max(1, self.config.concurrency))
        ]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:  # pragma: no cover
            for w in workers:
                w.cancel()
            raise

    async def _worker(self, worker_id: int) -> None:
        idle_rounds = 0
        while not self._stop.is_set():
            item = self.frontier.claim()
            if item is None:
                if self._active == 0:
                    idle_rounds += 1
                    if idle_rounds >= 3:
                        return
                else:
                    idle_rounds = 0
                await asyncio.sleep(0.2)
                continue
            idle_rounds = 0
            self._active += 1
            self._inflight.add(item.id)
            try:
                await self._process(item)
            except asyncio.CancelledError:
                self.frontier.release(item)
                raise
            except Exception as exc:  # one bad URL must never end the crawl
                tb = traceback.format_exc()
                self.log.error("unhandled error on %s: %s", item.normalized_url, exc)
                self.db.log_error(item.normalized_url, "process", exc, tb)
                self.stats.bump("errors")
                retried = self.frontier.fail(item, f"{type(exc).__name__}: {exc}",
                                             max_retries=self.config.max_retries)
                if not retried:
                    self.stats.bump("urls_failed")
            finally:
                self._active -= 1
                self._inflight.discard(item.id)
                self._processed += 1
                if self._processed % self.config.checkpoint_every == 0:
                    self._checkpoint()
        # Clean shutdown: hand anything still claimed back to the frontier.
        self.frontier.reset_stale()

    def _checkpoint(self) -> None:
        self.stats.write(self.config.logs_path / "statistics.json")
        pending = self.frontier.pending_count()
        total = self._processed + pending
        self.log.info(
            "[%d/%d] done=%d failed=%d assets=%d comments=%d %.1fMB",
            self._processed, total, self.stats.urls_completed, self.stats.urls_failed,
            self.stats.assets_downloaded, self.stats.comments,
            self.stats.bytes_downloaded / 1_048_576,
        )

    # ==================================================================
    # per-URL processing
    # ==================================================================
    async def _process(self, item: FrontierItem) -> None:
        url = item.normalized_url
        if not self.scope.allows(url) and item.kind == PAGE:
            self.frontier.skip(item, "out_of_scope")
            self.stats.bump("urls_skipped")
            return
        if item.kind == ASSET and not self._asset_in_scope(url):
            self.frontier.skip(item, "asset_out_of_scope")
            self.stats.bump("urls_skipped")
            return
        if item.depth > self.config.max_depth:
            self.frontier.skip(item, f"max_depth({self.config.max_depth})")
            self.stats.bump("urls_skipped")
            return
        # A reached limit stops the crawl; it must not burn through the
        # frontier marking everything SKIPPED, or the crawl could not resume.
        if self.config.max_pages and item.kind == PAGE and \
                self.stats.urls_completed >= self.config.max_pages:
            self._stop_for_limit(item, f"max_pages ({self.config.max_pages}) reached")
            return
        if self.config.max_assets and item.kind == ASSET and \
                self.stats.assets_downloaded >= self.config.max_assets:
            self._stop_for_limit(item, f"max_assets ({self.config.max_assets}) reached")
            return
        if not await self._robots_allows(url):
            self.frontier.skip(item, "robots_disallow")
            self.stats.bump("urls_skipped")
            self.log.debug("robots disallow: %s", url)
            return

        if item.kind == ASSET:
            await self._process_asset(item)
        else:
            await self._process_page(item)

    # -- assets ---------------------------------------------------------
    async def _process_asset(self, item: FrontierItem) -> None:
        assert self._fetcher is not None
        url = item.normalized_url
        started = time.time()
        try:
            result = await self._fetcher.fetch(url)
        except Exception as exc:
            self._record_attempt(item, started, None, 0, "failed", str(exc), "http")
            retried = self.frontier.fail(item, f"{type(exc).__name__}: {exc}",
                                         max_retries=self.config.max_retries)
            if not retried:
                self.stats.bump("assets_failed")
                self.stats.bump("urls_failed")
                self.db.execute("UPDATE assets SET error=? WHERE normalized_url=?",
                                (str(exc)[:500], url))
            return

        self.stats.record_status(result.status)
        self.stats.bump("http_requests")
        self.stats.bump("urls_fetched")
        self.stats.bump("bytes_downloaded", len(result.content))
        if not result.ok:
            permanent, label = Fetcher.classify_failure(result.status)
            self._record_attempt(item, started, result.status, len(result.content),
                                 "failed" if permanent else "retry", label, "http",
                                 result.attempts)
            if label == "ACCESS_RESTRICTED":
                self.stats.bump("urls_access_restricted")
                self.frontier.skip(item, "ACCESS_RESTRICTED")
                self.db.execute("UPDATE assets SET error=?, http_status=? WHERE normalized_url=?",
                                ("ACCESS_RESTRICTED", result.status, url))
                return
            retried = self.frontier.fail(item, label, http_status=result.status,
                                         permanent=permanent,
                                         max_retries=self.config.max_retries)
            if not retried:
                self.stats.bump("assets_failed")
                self.stats.bump("urls_failed")
                self.db.execute("UPDATE assets SET error=?, http_status=? WHERE normalized_url=?",
                                (label, result.status, url))
            return

        mime = result.content_type or None
        asset_type = classify_asset(mime, url)
        path, digest, was_new = self.storage.store_asset(result.content, mime, url, asset_type)
        width = height = None
        if asset_type == "image":
            width, height = image_dimensions(result.content)

        self.db.execute(
            "INSERT INTO assets(url, normalized_url, final_url, asset_type, mime_type,"
            " extension, http_status, byte_size, sha256, width, height, local_path, downloaded_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(normalized_url) DO UPDATE SET"
            "  final_url=excluded.final_url, asset_type=excluded.asset_type,"
            "  mime_type=excluded.mime_type, extension=excluded.extension,"
            "  http_status=excluded.http_status, byte_size=excluded.byte_size,"
            "  sha256=excluded.sha256, width=excluded.width, height=excluded.height,"
            "  local_path=excluded.local_path, downloaded_at=excluded.downloaded_at, error=NULL",
            (item.url, url, result.final_url, asset_type, mime,
             guess_extension(mime, url), result.status, len(result.content), digest,
             width, height, self.storage.rel(path), time.time()),
        )
        self.stats.bump("assets_downloaded")
        if not was_new:
            self.stats.bump("assets_deduplicated")
        if asset_type == "image":
            self.stats.bump("images")
        elif asset_type in ("video", "audio"):
            self.stats.bump("media")
        elif asset_type == "document":
            self.stats.bump("documents")

        # Stylesheets reference further assets (fonts, background images).
        if asset_type == "stylesheet" and result.content:
            for css_url in urls_from_css(result.text(), result.final_url):
                if self._asset_in_scope(css_url):
                    self.frontier.add(css_url, kind=ASSET, source="css",
                                      parent_url=url, depth=item.depth + 1)

        self._record_attempt(item, started, result.status, len(result.content), "ok",
                             None, "http", result.attempts)
        self.frontier.complete(item, http_status=result.status, content_type=mime,
                               content_hash=digest, final_url=result.final_url)
        self.stats.bump("urls_completed")

    # -- pages ----------------------------------------------------------
    async def _process_page(self, item: FrontierItem) -> None:
        assert self._fetcher is not None
        url = item.normalized_url
        started = time.time()
        try:
            result = await self._fetcher.fetch(url)
        except Exception as exc:
            self._record_attempt(item, started, None, 0, "failed", str(exc), "http")
            self.db.log_error(url, "fetch", exc)
            retried = self.frontier.fail(item, f"{type(exc).__name__}: {exc}",
                                         max_retries=self.config.max_retries)
            if not retried:
                self.stats.bump("urls_failed")
            return

        self.stats.record_status(result.status)
        self.stats.bump("http_requests")
        self.stats.bump("urls_fetched")
        self.stats.bump("bytes_downloaded", len(result.content))

        if not result.ok:
            permanent, label = Fetcher.classify_failure(result.status)
            self._record_attempt(item, started, result.status, len(result.content),
                                 "failed" if permanent else "retry", label, "http",
                                 result.attempts)
            if label == "ACCESS_RESTRICTED":
                self.stats.bump("urls_access_restricted")
                self.frontier.skip(item, "ACCESS_RESTRICTED")
                self.log.info("ACCESS_RESTRICTED %s (HTTP %s)", url, result.status)
                return
            retried = self.frontier.fail(item, label, http_status=result.status,
                                         permanent=permanent,
                                         max_retries=self.config.max_retries)
            if not retried:
                self.stats.bump("urls_failed")
                self.log.warning("FAILED %s -> %s", url, label)
            return

        ctype = result.content_type
        digest = sha256_bytes(result.content)

        if not (result.is_html or "xml" in ctype or ctype in ("", "text/plain")
                or "json" in ctype):
            # A non-page content type reached the page queue (e.g. a PDF link).
            await self._store_as_asset(item, result, digest)
            return

        duplicate_of_url = self.frontier.seen_content_hash(digest, item.id)
        paths = self.storage.page_paths(url)
        paths.directory.mkdir(parents=True, exist_ok=True)
        self.storage.write_bytes(paths.raw, result.content)

        parse_result: ParseResult | None = None
        render_reason: str | None = None
        rendered_html: str | None = None
        render_info: dict[str, Any] = {}

        if result.is_html or "xml" in ctype or ctype == "":
            parse_result = parse_html(result.text(), result.final_url,
                                      extract_comment_data=self.config.extract_comments)
            if parse_result.problems:
                self.stats.bump("parse_problems", len(parse_result.problems))
                for problem in parse_result.problems:
                    self.db.log_error(url, "parse", problem)

            if self.config.render and self.renderer.available and self.config.mode is not Mode.DOWNLOAD_ONLY:
                render_reason = ("forced" if self.config.render_always
                                 else needs_rendering(parse_result, len(result.content)))
                if render_reason:
                    rendered_html, render_info = await self._render_page(url, paths)
                    if rendered_html:
                        merged = parse_html(rendered_html, result.final_url,
                                            extract_comment_data=self.config.extract_comments,
                                            source="rendered")
                        parse_result = self._merge_parses(parse_result, merged)
                    else:
                        render_reason = None

        page_id = self._write_page_row(
            item, result, digest, paths, parse_result, render_reason,
            bool(rendered_html), duplicate_of_url,
        )

        if parse_result is not None:
            self._persist_extraction(page_id, item, result, parse_result, paths)
            if duplicate_of_url:
                self.stats.bump("pages_duplicate")
                self.log.debug("duplicate content: %s == %s", url, duplicate_of_url)
            else:
                await self._enqueue_discovered(page_id, item, result, parse_result)
        if render_info.get("json_payloads"):
            self._persist_xhr_payloads(page_id, url, render_info["json_payloads"], item)
        if render_info.get("network"):
            self._persist_network(url, render_info["network"], item)

        if result.is_html:
            self.stats.bump("pages_html")
        elif "json" in ctype:
            self.stats.bump("pages_json")
        else:
            self.stats.bump("pages_other")

        self._record_attempt(item, started, result.status, len(result.content), "ok", None,
                             "browser" if rendered_html else "http", result.attempts)
        self.frontier.complete(
            item, http_status=result.status, content_type=ctype, content_hash=digest,
            final_url=result.final_url,
            canonical_url=parse_result.canonical if parse_result else None,
        )
        self.stats.bump("urls_completed")

    async def _store_as_asset(self, item: FrontierItem, result: FetchResult,
                              digest: str) -> None:
        """A URL queued as a page turned out to be a binary resource."""
        mime = result.content_type or None
        asset_type = classify_asset(mime, item.normalized_url)
        path, _digest, was_new = self.storage.store_asset(
            result.content, mime, item.normalized_url, asset_type)
        width, height = image_dimensions(result.content) if asset_type == "image" else (None, None)
        self.db.execute(
            "INSERT OR REPLACE INTO assets(url, normalized_url, final_url, asset_type,"
            " mime_type, extension, http_status, byte_size, sha256, width, height,"
            " local_path, downloaded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item.url, item.normalized_url, result.final_url, asset_type, mime,
             guess_extension(mime, item.normalized_url), result.status,
             len(result.content), digest, width, height, self.storage.rel(path), time.time()),
        )
        self.db.execute("UPDATE urls SET kind='asset', priority=1 WHERE id=?", (item.id,))
        self.stats.bump("assets_downloaded")
        if not was_new:
            self.stats.bump("assets_deduplicated")
        if asset_type == "image":
            self.stats.bump("images")
        elif asset_type in ("video", "audio"):
            self.stats.bump("media")
        elif asset_type == "document":
            self.stats.bump("documents")
        self.frontier.complete(item, http_status=result.status, content_type=mime,
                               content_hash=digest, final_url=result.final_url)
        self.stats.bump("urls_completed")

    async def _render_page(self, url: str, paths: Any) -> tuple[str | None, dict[str, Any]]:
        render = await self.renderer.render(url)
        if render.error or not render.html:
            if render.error:
                self.log.debug("render failed %s: %s", url, render.error)
                self.db.log_error(url, "render", render.error)
            return None, {}
        self.storage.write_text(paths.rendered, render.html)
        self.log.debug("rendered %s (%d scroll, %d load-more, %d xhr json)",
                       url, render.scrolls, render.load_more_clicks, len(render.json_payloads))
        return render.html, {
            "json_payloads": render.json_payloads,
            "network": render.network,
            "scrolls": render.scrolls,
            "clicks": render.load_more_clicks,
        }

    @staticmethod
    def _merge_parses(base: ParseResult, rendered: ParseResult) -> ParseResult:
        """Keep the richer of the two extractions, field by field."""
        merged = rendered
        merged.title = rendered.title or base.title
        merged.canonical = rendered.canonical or base.canonical
        merged.meta = {**base.meta, **rendered.meta}
        merged.parser_used = f"{base.parser_used}+rendered"
        merged.problems = base.problems + rendered.problems

        seen = {(l.url, l.link_type) for l in merged.links}
        merged.links.extend(l for l in base.links if (l.url, l.link_type) not in seen)
        seen_pag = {l.url for l in merged.pagination}
        merged.pagination.extend(l for l in base.pagination if l.url not in seen_pag)
        seen_sd = {(s.kind, s.subtype, s.raw[:200]) for s in merged.structured}
        merged.structured.extend(
            s for s in base.structured if (s.kind, s.subtype, s.raw[:200]) not in seen_sd)
        if len(base.comments) > len(merged.comments):
            merged.comments = base.comments
            merged.comments_html = base.comments_html or merged.comments_html
        if len(base.text) > len(merged.text):
            merged.text = base.text
            merged.body_text_length = base.body_text_length
        return merged

    # ==================================================================
    # persistence
    # ==================================================================
    def _write_page_row(self, item: FrontierItem, result: FetchResult, digest: str,
                        paths: Any, parse_result: ParseResult | None,
                        render_reason: str | None, rendered: bool,
                        duplicate_of_url: str | None) -> int:
        url = item.normalized_url
        page_type = None
        if parse_result is not None:
            try:
                soup, _ = make_soup(result.text())
                page_type = classify_page(url, soup, parse_result.structured, parse_result.text)
            except Exception:
                page_type = None
        dup_id = None
        if duplicate_of_url:
            row = self.db.query_one("SELECT id FROM pages WHERE normalized_url=?",
                                    (duplicate_of_url,))
            dup_id = row["id"] if row else None
        headers_json = (json.dumps(result.headers, ensure_ascii=False)
                        if self.config.store_response_headers else None)
        text = parse_result.text if parse_result else ""
        self.db.execute(
            "INSERT INTO pages(url_id, url, normalized_url, canonical_url, final_url, title,"
            " page_type, status, http_status, content_type, content_length, encoding, sha256,"
            " rendered, render_reason, depth, first_seen, last_crawled, local_dir, raw_path,"
            " rendered_path, text_path, text_length, word_count, link_count, asset_count,"
            " comment_count, duplicate_of, headers_json, redirect_chain)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(normalized_url) DO UPDATE SET"
            "  canonical_url=excluded.canonical_url, final_url=excluded.final_url,"
            "  title=excluded.title, page_type=excluded.page_type, status=excluded.status,"
            "  http_status=excluded.http_status, content_type=excluded.content_type,"
            "  content_length=excluded.content_length, sha256=excluded.sha256,"
            "  rendered=excluded.rendered, render_reason=excluded.render_reason,"
            "  last_crawled=excluded.last_crawled, text_length=excluded.text_length,"
            "  word_count=excluded.word_count, link_count=excluded.link_count,"
            "  asset_count=excluded.asset_count, comment_count=excluded.comment_count,"
            "  duplicate_of=excluded.duplicate_of, headers_json=excluded.headers_json,"
            "  redirect_chain=excluded.redirect_chain, rendered_path=excluded.rendered_path",
            (
                item.id, item.url, url,
                parse_result.canonical if parse_result else None,
                result.final_url,
                parse_result.title if parse_result else None,
                page_type, "COMPLETED", result.status, result.content_type,
                len(result.content), result.encoding, digest,
                int(rendered), render_reason, item.depth, time.time(), time.time(),
                self.storage.rel(paths.directory), self.storage.rel(paths.raw),
                self.storage.rel(paths.rendered) if rendered else None,
                self.storage.rel(paths.text), len(text), len(text.split()),
                len(parse_result.links) if parse_result else 0, 0,
                len(parse_result.comments) if parse_result else 0,
                dup_id, headers_json,
                json.dumps(result.redirect_chain) if result.redirect_chain else None,
            ),
        )
        row = self.db.query_one("SELECT id FROM pages WHERE normalized_url=?", (url,))
        return int(row["id"]) if row else 0

    def _persist_extraction(self, page_id: int, item: FrontierItem, result: FetchResult,
                            parse_result: ParseResult, paths: Any) -> None:
        url = item.normalized_url

        # text.txt - normalized, section-headed, LLM friendly.
        self.storage.write_text(paths.text, self._render_text_export(
            url, result, parse_result))

        # structured.json - parsed representations plus originals.
        structured_payload = [
            {
                "kind": s.kind, "subtype": s.subtype, "position": s.position,
                "parsed": s.parsed, "parse_error": s.parse_error,
                "raw": s.raw if s.parsed is None else None,
            }
            for s in parse_result.structured
        ]
        self.storage.write_json(paths.structured, structured_payload)
        jsonld_raw = [s.raw for s in parse_result.structured if s.kind == "jsonld"]
        if jsonld_raw:
            self.storage.write_text(paths.directory / "jsonld.raw.json",
                                    "\n\n".join(jsonld_raw))

        rows = [
            (page_id, url, s.kind, s.subtype, s.raw[:1_000_000],
             json.dumps(s.parsed, ensure_ascii=False, default=str) if s.parsed is not None else None,
             s.parse_error, s.position)
            for s in parse_result.structured
        ]
        if rows:
            self.db.executemany(
                "INSERT INTO structured_data(page_id, page_url, kind, subtype, raw,"
                " parsed_json, parse_error, position) VALUES(?,?,?,?,?,?,?,?)", rows)
            self.stats.bump("structured_items", len(rows))

        # comments
        if parse_result.comments:
            self._persist_comments(page_id, url, parse_result.comments,
                                   parse_result.comments_html, paths)

        # metadata.json - everything needed to analyse offline.
        metadata = {
            "url": item.url,
            "normalized_url": url,
            "final_url": result.final_url,
            "canonical_url": parse_result.canonical,
            "http_status": result.status,
            "content_type": result.content_type,
            "content_length": len(result.content),
            "encoding": result.encoding,
            "sha256": sha256_bytes(result.content),
            "title": parse_result.title,
            "lang": parse_result.lang,
            "downloaded_at": time.time(),
            "downloaded_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "depth": item.depth,
            "parent_url": item.parent_url,
            "discovery_source": item.discovery_source,
            "redirect_chain": result.redirect_chain,
            "response_headers": result.headers if self.config.store_response_headers else None,
            "meta": parse_result.meta,
            "link_count": len(parse_result.links),
            "comment_count": len(parse_result.comments),
            "parser": parse_result.parser_used,
            "parse_problems": parse_result.problems,
            "has_load_more": parse_result.has_load_more,
        }
        self.storage.write_json(paths.metadata, metadata)

    def _persist_comments(self, page_id: int, url: str,
                          comments: list[ExtractedComment], region_html: str | None,
                          paths: Any) -> None:
        payload = [c.to_dict() for c in comments]
        self.storage.write_json(paths.comments_json, payload)
        lines: list[str] = []
        for c in comments:
            lines.append(f"--- comment {c.position} ---")
            if c.comment_key:
                lines.append(f"id: {c.comment_key}")
            if c.author:
                lines.append(f"author: {c.author}")
            if c.author_url:
                lines.append(f"author_url: {c.author_url}")
            if c.published:
                lines.append(f"date: {c.published}")
            if c.rating:
                lines.append(f"rating: {c.rating}")
            lines.append(f"source: {c.source}")
            lines.append("")
            lines.append(c.body or "")
            for reply in c.replies:
                lines.append(f"  [reply] {reply.author or '?'} {reply.published or ''}: "
                             f"{(reply.body or '')[:2000]}")
            lines.append("")
        self.storage.write_text(paths.comments_text, "\n".join(lines))
        if region_html:
            self.storage.write_text(paths.comments_raw, region_html)

        rows = []
        for c in comments:
            rows.append((page_id, url, c.comment_key, c.parent_key, c.author, c.author_url,
                         c.published, c.rating, c.body, c.source, c.position,
                         (c.raw_html or "")[:200_000]))
            for reply in c.replies:
                rows.append((page_id, url, reply.comment_key, c.comment_key, reply.author,
                             reply.author_url, reply.published, reply.rating, reply.body,
                             reply.source + ":reply", c.position * 1000 + reply.position,
                             (reply.raw_html or "")[:100_000]))
        self.db.executemany(
            "INSERT OR IGNORE INTO comments(page_id, page_url, comment_key, parent_key,"
            " author, author_url, published, rating, body, source, position, raw_html)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.stats.bump("comments", len(rows))

    def _persist_xhr_payloads(self, page_id: int, url: str,
                              payloads: list[tuple[str, str]], item: FrontierItem) -> None:
        """JSON the page fetched for itself: listings, comments, galleries."""
        directory = self.storage.page_paths(url).directory / "xhr"
        for i, (endpoint, body) in enumerate(payloads, 1):
            try:
                parsed = json.loads(body)
                error = None
            except (json.JSONDecodeError, ValueError) as exc:
                parsed, error = None, str(exc)
            self.db.execute(
                "INSERT INTO structured_data(page_id, page_url, kind, subtype, raw,"
                " parsed_json, parse_error, position) VALUES(?,?,?,?,?,?,?,?)",
                (page_id, url, "xhr_json", endpoint[:500], body[:1_000_000],
                 json.dumps(parsed, ensure_ascii=False, default=str) if parsed is not None else None,
                 error, i),
            )
            self.storage.write_text(directory / f"{i:03d}.json", body)
            self.stats.bump("structured_items")
            if parsed is not None:
                for link in links_from_structured(
                    [type("S", (), {"kind": "jsonld", "parsed": parsed})()], url  # type: ignore
                ):
                    self._enqueue_url(link.url, item, "xhr_json", depth_delta=1)
                extra = comments_from_structured(
                    [type("S", (), {"kind": "jsonld", "parsed": parsed})()]  # type: ignore
                )
                if extra:
                    paths = self.storage.page_paths(url)
                    for c in extra:
                        c.source = "xhr"
                    self._persist_comments(page_id, url, extra, None, paths)

    def _persist_network(self, url: str, network: list[Any], item: FrontierItem) -> None:
        rows = []
        for rec in network:
            rows.append((url, rec.url, rec.resource_type, rec.mime_type, rec.status, time.time()))
            if rec.resource_type in ("image", "media", "font", "stylesheet", "script") \
                    and self._asset_in_scope(rec.url):
                self.frontier.add(rec.url, kind=ASSET, source="network",
                                  parent_url=url, depth=item.depth + 1)
            elif rec.resource_type in ("xhr", "fetch") and self.scope.allows(rec.url):
                self.db.record_discovery(rec.url, "api", url, rec.mime_type)
        if rows:
            self.db.executemany(
                "INSERT OR IGNORE INTO media_events(page_url, url, resource_type, mime_type,"
                " http_status, observed_at) VALUES(?,?,?,?,?,?)", rows)

    # ==================================================================
    # link / asset enqueueing
    # ==================================================================
    async def _enqueue_discovered(self, page_id: int, item: FrontierItem,
                                  result: FetchResult, parse_result: ParseResult) -> None:
        url = item.normalized_url
        link_rows = []
        for link in parse_result.links:
            normalized = try_normalize(link.url, **self.config.normalization_kwargs())
            if not normalized:
                continue
            in_scope = self.scope.allows(normalized)
            link_rows.append((page_id, url, link.url, normalized, link.text, link.rel,
                              link.link_type, int(in_scope), time.time()))
            if in_scope:
                self._enqueue_url(normalized, item, f"link:{link.link_type}")
        for link in parse_result.pagination:
            normalized = try_normalize(link.url, **self.config.normalization_kwargs())
            if not normalized:
                continue
            in_scope = self.scope.allows(normalized)
            link_rows.append((page_id, url, link.url, normalized, link.text, link.rel,
                              "pagination", int(in_scope), time.time()))
            if in_scope:
                self._enqueue_url(normalized, item, "pagination")
        if link_rows:
            self.db.executemany(
                "INSERT OR IGNORE INTO links(source_page_id, source_url, destination_url,"
                " normalized_destination, link_text, rel, link_type, in_scope, discovered_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)", link_rows)
            self.stats.bump("links_recorded", len(link_rows))

        if parse_result.canonical:
            canonical = try_normalize(parse_result.canonical,
                                      **self.config.normalization_kwargs())
            if canonical and canonical != url and self.scope.allows(canonical):
                self._enqueue_url(canonical, item, "canonical")

        # Assets: record the reference first so it survives a failed download.
        markup = result.text()
        if parse_result.parser_used.endswith("+rendered"):
            rendered_path = self.storage.page_paths(url).rendered
            if rendered_path.exists():
                markup = rendered_path.read_text(encoding="utf-8", errors="replace")
        try:
            soup, _ = make_soup(markup)
            refs = extract_assets(soup, result.final_url)
        except Exception as exc:
            self.db.log_error(url, "parse", f"asset extraction: {exc}")
            refs = []

        asset_count = 0
        for ref in refs:
            normalized = try_normalize(ref.url, **self.config.normalization_kwargs())
            if not normalized:
                continue
            if not self._asset_in_scope(normalized):
                continue
            hint = ref.asset_type_hint or classify_asset(None, normalized)
            self.db.execute(
                "INSERT OR IGNORE INTO assets(url, normalized_url, asset_type)"
                " VALUES(?,?,?)", (ref.url, normalized, hint))
            row = self.db.query_one("SELECT id FROM assets WHERE normalized_url=?", (normalized,))
            if row:
                self.db.execute(
                    "INSERT OR IGNORE INTO asset_references(asset_id, page_id, page_url,"
                    " attribute, element, position, alt_text) VALUES(?,?,?,?,?,?,?)",
                    (row["id"], page_id, url, ref.attribute, ref.element, ref.position,
                     ref.alt_text))
            self.frontier.add(normalized, kind=ASSET, source=f"asset:{ref.attribute}",
                              parent_url=url, depth=item.depth + 1)
            asset_count += 1
        if asset_count:
            self.db.execute("UPDATE pages SET asset_count=? WHERE id=?", (asset_count, page_id))

    def _enqueue_url(self, url: str, parent: FrontierItem, source: str,
                     depth_delta: int = 1) -> None:
        depth = parent.depth + depth_delta
        if depth > self.config.max_depth:
            return
        self.frontier.add(url, kind=PAGE, depth=depth,
                          parent_url=parent.normalized_url, source=source)

    def _asset_in_scope(self, url: str) -> bool:
        if self.config.download_offsite_assets:
            return url.startswith(("http://", "https://"))
        return self.scope.host_allowed(host_of(url))

    def _record_attempt(self, item: FrontierItem, started: float, status: int | None,
                        size: int, outcome: str, error: str | None, via: str,
                        history: list[dict[str, Any]] | None = None) -> None:
        """Persist the attempt(s) this URL took, including retried ones."""
        finished = time.time()
        rows: list[tuple[Any, ...]] = []
        # Retries happen inside the fetcher; record each so backoff is visible.
        for entry in (history or [])[:-1]:
            rows.append((
                item.id, item.normalized_url, entry.get("attempt"), started, finished,
                entry.get("elapsed_ms"), entry.get("status"), 0, "GET",
                entry.get("outcome", "retry"),
                (entry.get("error") or "")[:1000] or None, via,
            ))
        rows.append((
            item.id, item.normalized_url,
            (history[-1].get("attempt") if history else item.attempt_count),
            started, finished, int((finished - started) * 1000), status, size,
            "GET", outcome, (error or "")[:1000] or None, via,
        ))
        self.db.executemany(
            "INSERT INTO crawl_attempts(url_id, url, attempt_no, started_at, finished_at,"
            " duration_ms, http_status, bytes, method, outcome, error, via)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        retried = max(0, len(rows) - 1)
        if retried:
            self.stats.bump("retries", retried)

    # ==================================================================
    # text export
    # ==================================================================
    @staticmethod
    def _render_text_export(url: str, result: FetchResult,
                            parse_result: ParseResult) -> str:
        out: list[str] = []
        add = out.append
        add("=" * 70)
        add("PAGE")
        add("=" * 70)
        add(f"URL: {url}")
        add(f"FINAL URL: {result.final_url}")
        if parse_result.canonical:
            add(f"CANONICAL URL: {parse_result.canonical}")
        add(f"TITLE: {parse_result.title or ''}")
        add(f"HTTP STATUS: {result.status}")
        add(f"CONTENT TYPE: {result.content_type}")
        add(f"LANGUAGE: {parse_result.lang or ''}")
        add(f"RETRIEVED: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
        add("")
        if parse_result.meta:
            add("-" * 70)
            add("METADATA")
            add("-" * 70)
            for key in sorted(parse_result.meta):
                add(f"{key}: {parse_result.meta[key]}")
            add("")
        structured = [s for s in parse_result.structured
                      if s.kind in ("jsonld", "microdata") and s.parsed is not None]
        if structured:
            add("-" * 70)
            add("STRUCTURED DATA")
            add("-" * 70)
            for s in structured:
                add(f"[{s.kind} {s.subtype or ''}]")
                add(json.dumps(s.parsed, indent=2, ensure_ascii=False, default=str)[:20000])
                add("")
        add("-" * 70)
        add("TEXT")
        add("-" * 70)
        add(parse_result.text)
        add("")
        if parse_result.comments:
            add("-" * 70)
            add(f"COMMENTS ({len(parse_result.comments)})")
            add("-" * 70)
            for c in parse_result.comments:
                header = " | ".join(filter(None, [
                    f"#{c.position}",
                    f"id={c.comment_key}" if c.comment_key else None,
                    f"author={c.author}" if c.author else None,
                    f"author_url={c.author_url}" if c.author_url else None,
                    f"date={c.published}" if c.published else None,
                    f"rating={c.rating}" if c.rating else None,
                    f"source={c.source}",
                ]))
                add(header)
                add(c.body or "")
                for reply in c.replies:
                    add(f"    [reply] {reply.author or '?'} {reply.published or ''}")
                    add(f"    {reply.body or ''}")
                add("")
        links = [l for l in parse_result.links if l.link_type in ("a", "area")]
        if links:
            add("-" * 70)
            add(f"LINKS ({len(links)})")
            add("-" * 70)
            for l in links[:500]:
                add(f"{l.text or ''} -> {l.url}")
            add("")
        return "\n".join(out)
