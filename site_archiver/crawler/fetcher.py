"""Async HTTP fetching with per-host politeness and exponential backoff."""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from .config import Config

# Statuses that will never succeed on retry.
PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 405, 406, 410, 451})
# Statuses worth retrying with backoff.
TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 507, 509})

RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.LocalProtocolError,
    asyncio.TimeoutError,
)


class AccessRestricted(Exception):
    """The resource exists but requires authorization we will not attempt."""


@dataclass(slots=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    content: bytes
    encoding: str | None
    elapsed_ms: int
    redirect_chain: list[str] = field(default_factory=list)
    from_cache: bool = False
    error: str | None = None
    via: str = "http"
    #: One entry per HTTP attempt, so retries and backoff stay observable.
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def is_html(self) -> bool:
        return self.content_type in ("text/html", "application/xhtml+xml")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        enc = self.encoding or "utf-8"
        for candidate in (enc, "utf-8", "cp1251", "latin-1"):
            try:
                return self.content.decode(candidate)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.content.decode("utf-8", errors="replace")


class HostLimiter:
    """Per-host concurrency cap plus a minimum gap between requests."""

    def __init__(self, concurrency: int, min_delay: float):
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.min_delay = min_delay
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    def set_delay(self, delay: float) -> None:
        self.min_delay = max(self.min_delay, delay)

    async def acquire(self) -> None:
        await self.sem.acquire()
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            # Small jitter avoids lock-step request bursts.
            self._next_allowed = now + self.min_delay * random.uniform(0.85, 1.15)

    def release(self) -> None:
        self.sem.release()


class Fetcher:
    def __init__(self, config: Config, logger: Any = None):
        self.config = config
        self.log = logger
        self._limiters: dict[str, HostLimiter] = {}
        self._global_sem = asyncio.Semaphore(max(1, config.concurrency))
        self._client: httpx.AsyncClient | None = None
        self.bytes_downloaded = 0
        self.requests_made = 0

    async def __aenter__(self) -> "Fetcher":
        limits = httpx.Limits(
            max_connections=max(4, self.config.concurrency * 2),
            max_keepalive_connections=max(2, self.config.concurrency),
            keepalive_expiry=30.0,
        )
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=10,
            timeout=httpx.Timeout(self.config.timeout, connect=min(15.0, self.config.timeout)),
            limits=limits,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
            },
            verify=self.config.verify_tls,
            http2=True,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def limiter_for(self, host: str) -> HostLimiter:
        if host not in self._limiters:
            self._limiters[host] = HostLimiter(
                self.config.per_host_concurrency, self.config.min_delay
            )
        return self._limiters[host]

    def set_host_delay(self, host: str, delay: float) -> None:
        self.limiter_for(host).set_delay(min(delay, self.config.max_crawl_delay))

    # ------------------------------------------------------------------
    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> FetchResult:
        """Fetch with retries.  Always returns a FetchResult or raises the
        last exception after exhausting retries."""
        if self._client is None:
            raise RuntimeError("Fetcher used outside its async context manager")
        host = httpx.URL(url).host or ""
        attempt = 0
        last_exc: Exception | None = None
        history: list[dict[str, Any]] = []
        while attempt <= self.config.max_retries:
            attempt += 1
            limiter = self.limiter_for(host)
            await self._global_sem.acquire()
            await limiter.acquire()
            started = time.monotonic()
            try:
                result = await self._do_request(url, method, headers, max_bytes)
            except RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                history.append({
                    "attempt": attempt, "status": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "outcome": "retry" if attempt <= self.config.max_retries else "failed",
                })
                if attempt > self.config.max_retries:
                    break
                await self._sleep_backoff(attempt)
                continue
            except httpx.HTTPError as exc:  # non-retryable protocol errors
                raise
            finally:
                limiter.release()
                self._global_sem.release()

            result.elapsed_ms = int((time.monotonic() - started) * 1000)
            self.requests_made += 1
            self.bytes_downloaded += len(result.content)

            if result.status in TRANSIENT_STATUSES and attempt <= self.config.max_retries:
                delay = self._retry_after(result.headers) or self._backoff_delay(attempt)
                history.append({
                    "attempt": attempt, "status": result.status, "error": None,
                    "elapsed_ms": result.elapsed_ms, "outcome": "retry",
                    "retry_in": round(delay, 2),
                })
                if result.status == 429:
                    # Slow this host down permanently for the rest of the run.
                    self.set_host_delay(host, min(self.config.max_crawl_delay,
                                                  limiter.min_delay * 2))
                if self.log:
                    self.log.debug("retry %s after %s in %.1fs", url, result.status, delay)
                await asyncio.sleep(delay)
                continue
            history.append({
                "attempt": attempt, "status": result.status, "error": None,
                "elapsed_ms": result.elapsed_ms, "outcome": "final",
            })
            result.attempts = history
            return result

        assert last_exc is not None
        last_exc.attempt_history = history  # type: ignore[attr-defined]
        raise last_exc

    async def _do_request(
        self,
        url: str,
        method: str,
        headers: Mapping[str, str] | None,
        max_bytes: int | None,
    ) -> FetchResult:
        assert self._client is not None
        cap = max_bytes if max_bytes is not None else self.config.max_asset_bytes
        chain: list[str] = []
        async with self._client.stream(method, url, headers=dict(headers or {})) as resp:
            for r in resp.history:
                chain.append(str(r.url))
            buf = bytearray()
            truncated = False
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if cap and len(buf) > cap:
                    truncated = True
                    break
            return FetchResult(
                url=url,
                final_url=str(resp.url),
                status=resp.status_code,
                headers={k.lower(): v for k, v in resp.headers.items()},
                content=bytes(buf),
                encoding=resp.charset_encoding,
                elapsed_ms=0,
                redirect_chain=chain,
                error="truncated: exceeded max_asset_bytes" if truncated else None,
            )

    # ------------------------------------------------------------------
    def _backoff_delay(self, attempt: int) -> float:
        delay = min(self.config.backoff_base ** attempt, self.config.backoff_cap)
        return delay * random.uniform(0.7, 1.3)

    async def _sleep_backoff(self, attempt: int) -> None:
        await asyncio.sleep(self._backoff_delay(attempt))

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> float | None:
        value = headers.get("retry-after")
        if not value:
            return None
        try:
            return min(float(value), 120.0)
        except ValueError:
            return None

    @staticmethod
    def classify_failure(status: int) -> tuple[bool, str]:
        """Return (is_permanent, label) for a non-2xx status."""
        if status in (401, 403, 407):
            return True, "ACCESS_RESTRICTED"
        if status in PERMANENT_STATUSES:
            return True, f"HTTP_{status}"
        if status in TRANSIENT_STATUSES:
            return False, f"HTTP_{status}"
        if 300 <= status < 400:
            return True, f"HTTP_{status}"
        return status >= 400, f"HTTP_{status}"
