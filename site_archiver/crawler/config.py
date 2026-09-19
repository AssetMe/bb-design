"""Crawler configuration: defaults, file loading, CLI overrides."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

try:  # PyYAML is optional; JSON config always works.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class Mode(str, Enum):
    FULL = "FULL"
    DISCOVERY_ONLY = "DISCOVERY_ONLY"
    DOWNLOAD_ONLY = "DOWNLOAD_ONLY"
    RENDER = "RENDER"
    VALIDATE = "VALIDATE"
    RESUME = "RESUME"


DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 site-archiver/1.0 (+authorized archival)"
)

TEXTUAL_TYPES = (
    "text/html", "application/xhtml+xml", "application/xml", "text/xml",
    "text/plain", "application/json", "application/ld+json", "text/css",
    "application/javascript", "text/javascript", "application/rss+xml",
    "application/atom+xml",
)


@dataclass(slots=True)
class Config:
    # --- targets -------------------------------------------------------
    roots: list[str] = field(default_factory=list)
    mode: Mode = Mode.FULL

    # --- scope ---------------------------------------------------------
    include_subdomains: bool = True
    allow_patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)
    max_depth: int = 12
    max_pages: int = 0            # 0 = unlimited
    max_assets: int = 0
    download_offsite_assets: bool = False

    # --- politeness ----------------------------------------------------
    honor_robots: bool = True
    user_agent: str = DEFAULT_UA
    concurrency: int = 4          # global in-flight requests
    per_host_concurrency: int = 2
    min_delay: float = 0.5        # seconds between requests to one host
    respect_crawl_delay: bool = True
    max_crawl_delay: float = 10.0
    timeout: float = 30.0
    max_retries: int = 4
    backoff_base: float = 1.5
    backoff_cap: float = 60.0

    # --- rendering -----------------------------------------------------
    render: bool = True           # allow Playwright when a page needs it
    render_always: bool = False
    render_timeout: float = 30.0
    render_wait: str = "networkidle"
    render_wait_selector: str | None = None
    render_max_scrolls: int = 12
    render_load_more_clicks: int = 25
    render_load_more_selectors: list[str] = field(
        default_factory=lambda: [
            "button.load-more", "a.load-more", "[data-load-more]",
            "button[data-more]", ".more-comments", "#load-more",
        ]
    )
    capture_network: bool = True
    browser_executable: str | None = None

    # --- storage -------------------------------------------------------
    output_dir: str = "archive"
    db_path: str = "archive/archive.db"
    log_dir: str = "logs"
    export_dir: str = "exports"
    mirror_dir: str = "mirror"
    max_asset_bytes: int = 200 * 1024 * 1024
    store_response_headers: bool = True
    compress_raw: bool = False

    # --- behaviour -----------------------------------------------------
    force: bool = False           # re-fetch already completed URLs
    checkpoint_every: int = 25
    extract_comments: bool = True
    follow_sitemaps: bool = True
    sitemap_candidates: list[str] = field(
        default_factory=lambda: [
            "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
            "/sitemap.xml.gz", "/sitemap/sitemap.xml", "/wp-sitemap.xml",
            "/sitemap/", "/sitemap.txt",
        ]
    )
    drop_tracking_params: bool = True
    sort_query_params: bool = True
    strip_index_filenames: bool = False
    verify_tls: bool = True

    # --- runtime (not persisted as config) -----------------------------
    project_root: str = "."

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        if isinstance(self.mode, str):
            self.mode = Mode(self.mode.upper())
        self.roots = [r for r in self.roots if r and r.strip()]

    # --- path helpers --------------------------------------------------
    def _p(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else Path(self.project_root) / p

    @property
    def archive_path(self) -> Path:
        return self._p(self.output_dir)

    @property
    def database_path(self) -> Path:
        return self._p(self.db_path)

    @property
    def logs_path(self) -> Path:
        return self._p(self.log_dir)

    @property
    def exports_path(self) -> Path:
        return self._p(self.export_dir)

    @property
    def mirror_path(self) -> Path:
        return self._p(self.mirror_dir)

    def ensure_dirs(self) -> None:
        for p in (self.archive_path, self.logs_path, self.exports_path):
            p.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    # --- (de)serialisation ---------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mode"] = self.mode.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        return cls(**data)

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None, **overrides: Any) -> "Config":
        data: dict[str, Any] = {}
        if path:
            p = Path(path)
            text = p.read_text(encoding="utf-8")
            if p.suffix in (".yaml", ".yml"):
                if yaml is None:
                    raise RuntimeError("PyYAML not installed; use a .json config")
                data = yaml.safe_load(text) or {}
            else:
                data = json.loads(text)
            data.setdefault("project_root", str(p.parent))
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls.from_dict(data)

    def save(self, path: str | os.PathLike[str]) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    # --- derived -------------------------------------------------------
    def normalization_kwargs(self) -> dict[str, Any]:
        from .urlnorm import TRACKING_PARAMS

        return {
            "drop_params": TRACKING_PARAMS if self.drop_tracking_params else (),
            "sort_params": self.sort_query_params,
            "strip_index": self.strip_index_filenames,
        }


def default_config(roots: Sequence[str], project_root: str = ".") -> Config:
    return Config(roots=list(roots), project_root=project_root)
