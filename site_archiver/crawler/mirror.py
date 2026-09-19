"""Build a browsable offline mirror from the archive.

The raw archive is never modified: the mirror is a separate tree of rewritten
copies whose internal links point at locally archived files.
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Config
from .database import Database
from .parser import make_soup
from .storage import Storage, slugify
from .urlnorm import try_normalize


class MirrorBuilder:
    def __init__(self, config: Config, db: Database | None = None, logger: Any = None):
        self.config = config
        self.db = db or Database(config.database_path)
        self.storage = Storage(config.archive_path, config.compress_raw)
        self.root = config.mirror_path
        self.log = logger

    def _mirror_name(self, normalized_url: str) -> str:
        parts = urlsplit(normalized_url)
        path = parts.path
        if path.endswith("/") or not path:
            path += "index.html"
        elif not Path(path).suffix:
            path += ".html"
        segments = [slugify(s, "_") for s in path.strip("/").split("/") if s]
        if parts.query:
            import hashlib
            digest = hashlib.sha1(parts.query.encode()).hexdigest()[:8]
            stem = Path(segments[-1])
            segments[-1] = f"{stem.stem}__q{digest}{stem.suffix or '.html'}"
        return str(Path(slugify(parts.netloc, "host")) / Path(*segments))

    def build(self) -> dict[str, int]:
        self.root.mkdir(parents=True, exist_ok=True)
        pages = self.db.query(
            "SELECT id, normalized_url, final_url, raw_path, rendered_path, title"
            " FROM pages WHERE raw_path IS NOT NULL ORDER BY id")
        url_to_file = {p["normalized_url"]: self._mirror_name(p["normalized_url"])
                       for p in pages}

        assets = {
            r["normalized_url"]: r["local_path"]
            for r in self.db.query(
                "SELECT normalized_url, local_path FROM assets WHERE local_path IS NOT NULL")
        }

        written = rewritten_links = rewritten_assets = 0
        for page in pages:
            source = self.storage.root / (page["rendered_path"] or page["raw_path"])
            if not source.exists():
                continue
            try:
                markup = self.storage.read_bytes(source).decode("utf-8", errors="replace")
            except OSError:
                continue
            target = self.root / url_to_file[page["normalized_url"]]
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                soup, _ = make_soup(markup)
            except Exception:
                target.write_text(markup, encoding="utf-8")
                written += 1
                continue

            base = page["final_url"] or page["normalized_url"]
            depth_prefix = "../" * len(target.relative_to(self.root).parent.parts)

            def local_for(raw: str | None, table: dict[str, str], is_asset: bool) -> str | None:
                nonlocal rewritten_links, rewritten_assets
                if not raw:
                    return None
                normalized = try_normalize(raw, base, **self.config.normalization_kwargs())
                if not normalized or normalized not in table:
                    return None
                if is_asset:
                    rel = f"{depth_prefix}_assets/{table[normalized]}"
                    rewritten_assets += 1
                else:
                    rel = depth_prefix + table[normalized]
                    rewritten_links += 1
                return rel

            for tag, attr in (("a", "href"), ("area", "href")):
                for node in soup.find_all(tag):
                    new = local_for(node.get(attr), url_to_file, False)
                    if new:
                        node["data-original-href"] = node.get(attr)
                        node[attr] = new
            for tag, attr in (("img", "src"), ("script", "src"), ("source", "src"),
                              ("video", "src"), ("audio", "src"), ("link", "href"),
                              ("embed", "src")):
                for node in soup.find_all(tag):
                    new = local_for(node.get(attr), assets, True)
                    if new:
                        node["data-original-" + attr] = node.get(attr)
                        node[attr] = new
                    if node.has_attr("srcset"):
                        del node["srcset"]

            banner = soup.new_tag("div")
            banner["style"] = ("background:#222;color:#eee;padding:6px 10px;font:12px/1.4 "
                               "system-ui,sans-serif")
            banner.string = f"Offline archive copy of {page['normalized_url']}"
            if soup.body:
                soup.body.insert(0, banner)
            target.write_text(str(soup), encoding="utf-8")
            written += 1

        # Copy the content-addressed assets under the mirror's own tree.
        asset_root = self.root / "_assets"
        copied = 0
        for normalized, local in assets.items():
            src = self.storage.root / local
            if not src.exists():
                continue
            dst = asset_root / local
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                dst.write_bytes(src.read_bytes())
                copied += 1

        self._write_index(pages, url_to_file)
        return {
            "pages_written": written,
            "assets_copied": copied,
            "links_rewritten": rewritten_links,
            "asset_refs_rewritten": rewritten_assets,
        }

    def _write_index(self, pages: list[Any], url_to_file: dict[str, str]) -> None:
        rows = []
        for page in pages:
            target = url_to_file[page["normalized_url"]]
            title = html.escape(page["title"] or page["normalized_url"])
            url = html.escape(page["normalized_url"])
            rows.append(
                f'<tr><td><a href="{html.escape(target)}">{title}</a></td>'
                f'<td><code>{url}</code></td></tr>'
            )
        doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Offline archive index</title>
<style>
 body{{font:14px/1.5 system-ui,sans-serif;margin:2rem;max-width:1100px}}
 table{{border-collapse:collapse;width:100%}}
 td{{border-bottom:1px solid #ddd;padding:6px 8px;vertical-align:top}}
 code{{color:#666;font-size:12px;word-break:break-all}}
</style></head><body>
<h1>Offline archive index</h1>
<p>{len(rows)} archived page(s).</p>
<table>{''.join(rows)}</table>
</body></html>"""
        (self.root / "index.html").write_text(doc, encoding="utf-8")
