from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "fixtures"))

FIXTURE_PORT = 8899
BASE_URL = f"http://127.0.0.1:{FIXTURE_PORT}"


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _reset_server_state() -> None:
    """Clear /flaky and /rate-limited counters so retry paths are exercised."""
    import urllib.request

    try:
        urllib.request.urlopen(f"{BASE_URL}/__reset", timeout=2).read()
    except Exception:  # pragma: no cover - reset is best effort
        pass


@pytest.fixture(scope="session")
def fixture_site() -> Path:
    """Ensure the static fixture site exists on disk."""
    site = PROJECT / "fixtures" / "site"
    if not site.exists() or not (site / "index.html").exists():
        import build_site

        build_site.main()
    return site


@pytest.fixture(scope="session")
def live_server(fixture_site: Path) -> str:
    """A running fixture server; reuses one already listening on the port."""
    if _port_open(FIXTURE_PORT):
        _reset_server_state()
        yield BASE_URL
        return
    import server as fixture_server

    httpd = fixture_server.serve(FIXTURE_PORT)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    for _ in range(50):
        if _port_open(FIXTURE_PORT):
            break
        time.sleep(0.1)
    else:  # pragma: no cover
        httpd.shutdown()
        pytest.fail("fixture server did not start")
    _reset_server_state()
    yield BASE_URL
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def config(tmp_path):
    from crawler.config import Config

    cfg = Config(
        roots=[BASE_URL + "/"],
        project_root=str(tmp_path),
        output_dir="archive",
        db_path="archive/archive.db",
        log_dir="logs",
        export_dir="exports",
        mirror_dir="mirror",
        concurrency=3,
        per_host_concurrency=2,
        min_delay=0.0,
        max_retries=2,
        render=False,
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def db(tmp_path):
    from crawler.database import Database

    database = Database(tmp_path / "test.db")
    yield database
    database.close()
