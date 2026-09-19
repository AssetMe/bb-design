#!/usr/bin/env python3
"""Serve the fixture site, including the dynamic behaviours the crawler must
cope with: redirects, transient 5xx, 429 with Retry-After, a 401 area, and the
JSON endpoints the pages fetch for themselves."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SITE = Path(__file__).resolve().parent / "site"

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from build_site import COMMENTS, ITEMS, XHR_COMMENTS
except ImportError:  # pragma: no cover
    COMMENTS, ITEMS, XHR_COMMENTS = {}, [], {}

_state_lock = threading.Lock()
_hits: dict[str, int] = {}

EXTRA_TYPES = {
    ".gz": "application/x-gzip",
    ".mp4": "video/mp4",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml",
}


def _hit(key: str) -> int:
    with _state_lock:
        _hits[key] = _hits.get(key, 0) + 1
        return _hits[key]


class Handler(SimpleHTTPRequestHandler):
    server_version = "FixtureSite/1.0"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    # -- helpers --------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def log_message(self, fmt: str, *args) -> None:
        if self.server.verbose:  # type: ignore[attr-defined]
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def guess_type(self, path):  # type: ignore[override]
        ext = Path(str(path)).suffix.lower()
        if ext in EXTRA_TYPES:
            return EXTRA_TYPES[ext]
        return super().guess_type(path)

    # -- routing --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        route = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)

        if route == "/__reset":
            # Test-support only: makes /flaky and /rate-limited repeatable.
            reset_state()
            self._json({"reset": True})
            return

        if route == "/redirect-me":
            self.send_response(301)
            self.send_header("Location", "/about")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if route == "/redirect-chain":
            self.send_response(302)
            self.send_header("Location", "/redirect-me")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if route == "/flaky":
            n = _hit("flaky")
            if n <= 2:
                self._send(503, b"<h1>503 temporarily unavailable</h1>",
                           headers={"Retry-After": "1"})
                return
            self._send(200, b"<!doctype html><html><head><title>Flaky - recovered</title>"
                            b'<link rel="canonical" href="http://127.0.0.1:8899/flaky">'
                            b"</head><body><h1>Recovered</h1>"
                            b"<p>This endpoint failed twice before succeeding, which is "
                            b"exactly what the retry/backoff path is for.</p>"
                            b'<p><a href="/about">About</a></p></body></html>')
            return

        if route == "/rate-limited":
            n = _hit("rate-limited")
            if n <= 1:
                self._send(429, b"<h1>429 slow down</h1>", headers={"Retry-After": "1"})
                return
            self._send(200, b"<!doctype html><html><head><title>Rate limited - recovered"
                            b"</title></head><body><h1>Rate limit cleared</h1>"
                            b"<p>Served after the crawler backed off.</p></body></html>")
            return

        if route == "/server-error":
            self._send(500, b"<h1>500</h1>")
            return

        if route.startswith("/private"):
            self._send(401, b"<h1>401 Unauthorized</h1><p>Members only.</p>",
                       headers={"WWW-Authenticate": 'Basic realm="members"'})
            return

        if route == "/api/comments":
            item = int((query.get("item") or ["1"])[0])
            page = int((query.get("page") or ["1"])[0])
            pool = XHR_COMMENTS.get(item, [])
            per_page = 2
            start = (page - 2) * per_page  # page 1 is server-rendered
            chunk = pool[max(0, start):max(0, start) + per_page]
            self._json({"item": item, "page": page, "comments": chunk,
                        "has_more": (max(0, start) + per_page) < len(pool)})
            return

        if route == "/api/feed":
            page = int((query.get("page") or ["1"])[0])
            total_pages = 3
            entries = [
                {"title": f"Feed entry {(page - 1) * 3 + i}",
                 "body": f"Entry {(page - 1) * 3 + i} appended by the infinite scroll "
                         f"handler on page {page}.",
                 "url": f"/item/{((page - 1) * 3 + i - 1) % len(ITEMS) + 1}"}
                for i in range(1, 4)
            ]
            self._json({"page": page, "entries": entries, "has_more": page < total_pages})
            return

        if route == "/api/items":
            self._json({"items": [{**i, "url": f"/item/{i['id']}"} for i in ITEMS]})
            return

        if route == "/slow":
            time.sleep(float((query.get("s") or ["2"])[0]))
            self._send(200, b"<h1>Slow response</h1>")
            return

        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def send_error(self, code, message=None, explain=None):  # type: ignore[override]
        if code == 404:
            body = (b"<!doctype html><html><head><title>404 Not Found</title></head>"
                    b"<body><h1>404 Not Found</h1>"
                    b'<p>Back to <a href="/">the homepage</a>.</p></body></html>')
            self._send(404, body)
            return
        super().send_error(code, message, explain)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    verbose = False


def serve(port: int = 8899, verbose: bool = False) -> Server:
    httpd = Server(("127.0.0.1", port), Handler)
    httpd.verbose = verbose
    return httpd


def reset_state() -> None:
    """Clear the flaky/rate-limited counters (used between test runs)."""
    with _state_lock:
        _hits.clear()


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the crawler fixture site")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if not SITE.exists():
        print(f"fixture site missing at {SITE}; run fixtures/build_site.py first")
        return 1
    httpd = serve(args.port, args.verbose)
    print(f"serving {SITE} at http://127.0.0.1:{args.port}/  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
