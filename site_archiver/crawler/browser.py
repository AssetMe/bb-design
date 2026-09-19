"""Playwright rendering for JavaScript-dependent pages.

Used only when plain HTTP fetching demonstrably misses content.  It observes
the ordinary requests a page makes so the archive can include resources that
are never referenced in the initial HTML.  It never touches authentication,
CAPTCHA or any access-control mechanism.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config

try:
    from playwright.async_api import (
        Error as PlaywrightError,
        TimeoutError as PlaywrightTimeout,
        async_playwright,
    )
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False
    PlaywrightError = Exception  # type: ignore
    PlaywrightTimeout = Exception  # type: ignore
    async_playwright = None  # type: ignore

#: Bundled Chromium builds are often a different revision than the Playwright
#: package expects; probe for a usable binary instead of downloading one.
_EXECUTABLE_CANDIDATES = (
    "{root}/chromium-*/chrome-linux/chrome",
    "{root}/chromium_headless_shell-*/chrome-linux/headless_shell",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)


def find_browser_executable(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else None
    env = os.environ.get("ARCHIVER_BROWSER_PATH")
    if env and Path(env).exists():
        return env
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    import glob

    for pattern in _EXECUTABLE_CANDIDATES:
        pattern = pattern.format(root=root)
        matches = sorted(glob.glob(pattern), reverse=True)
        for match in matches:
            if os.access(match, os.X_OK):
                return match
    return None


@dataclass(slots=True)
class NetworkRecord:
    url: str
    resource_type: str
    status: int | None
    mime_type: str | None


@dataclass(slots=True)
class RenderResult:
    url: str
    final_url: str
    status: int | None
    html: str
    title: str | None
    text: str | None
    elapsed_ms: int
    network: list[NetworkRecord] = field(default_factory=list)
    json_payloads: list[tuple[str, str]] = field(default_factory=list)
    scrolls: int = 0
    load_more_clicks: int = 0
    console_errors: list[str] = field(default_factory=list)
    error: str | None = None
    via: str = "browser"


class Renderer:
    """Owns one browser process and hands out short-lived contexts."""

    def __init__(self, config: Config, logger: Any = None):
        self.config = config
        self.log = logger
        self._pw: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()
        self.available = PLAYWRIGHT_AVAILABLE
        self.executable = find_browser_executable(config.browser_executable)
        self.pages_rendered = 0
        self.restarts = 0
        self.unavailable_reason: str | None = None
        if not PLAYWRIGHT_AVAILABLE:
            self.unavailable_reason = "playwright package not installed"
        elif not self.executable:
            self.unavailable_reason = "no chromium executable found"

    async def start(self) -> bool:
        if not self.available or self.unavailable_reason:
            return False
        async with self._lock:
            if self._browser is not None:
                return True
            try:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=True,
                    executable_path=self.executable,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-background-networking",
                        "--mute-audio",
                    ],
                )
                return True
            except Exception as exc:
                self.unavailable_reason = f"launch failed: {exc}"
                self.available = False
                if self.log:
                    self.log.warning("browser unavailable: %s", exc)
                return False

    async def stop(self) -> None:
        async with self._lock:
            with contextlib.suppress(Exception):
                if self._browser is not None:
                    await self._browser.close()
            with contextlib.suppress(Exception):
                if self._pw is not None:
                    await self._pw.stop()
            self._browser = None
            self._pw = None

    async def restart(self) -> bool:
        self.restarts += 1
        await self.stop()
        self.available = PLAYWRIGHT_AVAILABLE and bool(self.executable)
        self.unavailable_reason = None if self.available else self.unavailable_reason
        return await self.start()

    # ------------------------------------------------------------------
    async def render(self, url: str, *, expand: bool = True) -> RenderResult:
        """Render a page and return its post-JavaScript DOM."""
        started = time.monotonic()
        if not await self.start():
            return RenderResult(url, url, None, "", None, None, 0,
                                error=self.unavailable_reason or "browser unavailable")
        try:
            return await self._render_once(url, expand, started)
        except (PlaywrightError, PlaywrightTimeout, asyncio.TimeoutError) as exc:
            # A crashed browser must not end the crawl - restart and retry once.
            message = str(exc)
            if any(s in message.lower() for s in
                   ("target closed", "browser has been closed", "crashed",
                    "connection closed", "browser closed")):
                if self.log:
                    self.log.warning("browser crashed on %s, restarting", url)
                if await self.restart():
                    try:
                        return await self._render_once(url, expand, started)
                    except Exception as exc2:
                        return RenderResult(url, url, None, "", None, None,
                                            int((time.monotonic() - started) * 1000),
                                            error=f"{type(exc2).__name__}: {exc2}")
            return RenderResult(url, url, None, "", None, None,
                                int((time.monotonic() - started) * 1000),
                                error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            return RenderResult(url, url, None, "", None, None,
                                int((time.monotonic() - started) * 1000),
                                error=f"{type(exc).__name__}: {exc}")

    async def _render_once(self, url: str, expand: bool, started: float) -> RenderResult:
        cfg = self.config
        context = await self._browser.new_context(
            user_agent=cfg.user_agent,
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=not cfg.verify_tls,
            locale="en-US",
        )
        network: list[NetworkRecord] = []
        json_payloads: list[tuple[str, str]] = []
        console_errors: list[str] = []
        page = await context.new_page()

        async def on_response(response: Any) -> None:
            if not cfg.capture_network:
                return
            try:
                rtype = response.request.resource_type
                mime = (response.headers or {}).get("content-type", "").split(";")[0]
                network.append(NetworkRecord(response.url, rtype, response.status, mime or None))
                # Capture JSON the page fetched for itself: this is where
                # dynamically loaded listings and comments usually live.
                if ("json" in (mime or "")) and rtype in ("xhr", "fetch"):
                    if len(json_payloads) < 60:
                        body = await response.text()
                        if body and len(body) < 4_000_000:
                            json_payloads.append((response.url, body))
            except Exception:
                pass

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        page.on("console", lambda m: console_errors.append(m.text[:500])
                if m.type == "error" and len(console_errors) < 50 else None)

        try:
            timeout_ms = int(cfg.render_timeout * 1000)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            status = response.status if response else None

            # Prefer a real signal over a fixed sleep.
            if cfg.render_wait_selector:
                with contextlib.suppress(PlaywrightTimeout):
                    await page.wait_for_selector(cfg.render_wait_selector, timeout=timeout_ms // 2)
            with contextlib.suppress(PlaywrightTimeout, PlaywrightError):
                await page.wait_for_load_state(cfg.render_wait, timeout=timeout_ms // 2)

            scrolls = clicks = 0
            if expand:
                clicks = await self._click_load_more(page)
                scrolls = await self._scroll_to_bottom(page)
                if clicks:  # new content may reveal more controls
                    clicks += await self._click_load_more(page)

            html = await page.content()
            title = await page.title()
            text = None
            with contextlib.suppress(Exception):
                text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            final_url = page.url
            self.pages_rendered += 1
            return RenderResult(
                url=url, final_url=final_url, status=status, html=html, title=title,
                text=text, elapsed_ms=int((time.monotonic() - started) * 1000),
                network=network, json_payloads=json_payloads, scrolls=scrolls,
                load_more_clicks=clicks, console_errors=console_errors,
            )
        finally:
            with contextlib.suppress(Exception):
                await page.close()
            with contextlib.suppress(Exception):
                await context.close()

    #: Growth is measured by node count as well as height: a short page can
    #: append content without ever becoming scrollable.
    _MEASURE_JS = """() => ({
        height: document.documentElement.scrollHeight,
        nodes: document.querySelectorAll('*').length
    })"""
    _SCROLL_JS = """() => {
        window.scrollTo(0, document.documentElement.scrollHeight);
        window.dispatchEvent(new Event('scroll'));
        document.dispatchEvent(new Event('scroll'));
    }"""

    async def _scroll_to_bottom(self, page: Any) -> int:
        """Drive infinite scroll until the page stops producing new content."""
        scrolls = 0
        last: dict[str, int] | None = None
        stable = 0
        for _ in range(max(0, self.config.render_max_scrolls)):
            try:
                current = await page.evaluate(self._MEASURE_JS)
            except Exception:
                break
            if last is not None and current == last:
                # Two quiet rounds in a row means the page is genuinely done;
                # one can simply mean the fetch has not resolved yet.
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last = current
            try:
                await page.evaluate(self._SCROLL_JS)
            except Exception:
                break
            scrolls += 1
            with contextlib.suppress(PlaywrightTimeout, PlaywrightError):
                await page.wait_for_load_state("networkidle", timeout=4000)
            await asyncio.sleep(0.3)
        return scrolls

    async def _click_load_more(self, page: Any) -> int:
        """Click 'load more' style controls until they stop appearing.

        Only clicks controls that are visible and enabled on the public page.
        """
        clicks = 0
        selectors = list(self.config.render_load_more_selectors)
        budget = max(0, self.config.render_load_more_clicks)
        while clicks < budget:
            clicked = False
            for selector in selectors:
                try:
                    locator = page.locator(selector).first
                    if await locator.count() == 0:
                        continue
                    if not await locator.is_visible():
                        continue
                    if not await locator.is_enabled():
                        continue
                    await locator.click(timeout=4000, no_wait_after=True)
                    clicks += 1
                    clicked = True
                    with contextlib.suppress(PlaywrightTimeout, PlaywrightError):
                        await page.wait_for_load_state("networkidle", timeout=4000)
                    await asyncio.sleep(0.2)
                    break
                except (PlaywrightTimeout, PlaywrightError):
                    continue
                except Exception:
                    continue
            if not clicked:
                break
        return clicks
