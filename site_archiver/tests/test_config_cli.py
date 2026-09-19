from __future__ import annotations

import json
from pathlib import Path

import pytest

from crawler.config import Config, Mode
from crawler.fetcher import Fetcher, HostLimiter


class TestConfig:
    def test_defaults_are_conservative(self):
        cfg = Config(roots=["https://e.com/"])
        assert cfg.honor_robots is True
        assert cfg.concurrency <= 8 and cfg.per_host_concurrency <= 4
        assert cfg.min_delay > 0
        assert cfg.verify_tls is True
        assert cfg.mode is Mode.FULL

    def test_mode_accepts_string(self):
        assert Config(roots=[], mode="discovery_only").mode is Mode.DISCOVERY_ONLY

    def test_roundtrip_json(self, tmp_path):
        cfg = Config(roots=["https://e.com/"], max_depth=3, min_delay=1.25,
                     deny_patterns=[r"/admin"])
        path = tmp_path / "c.json"
        cfg.save(path)
        loaded = Config.load(path)
        assert loaded.roots == cfg.roots
        assert loaded.max_depth == 3
        assert loaded.min_delay == 1.25
        assert loaded.deny_patterns == [r"/admin"]

    def test_overrides_win_over_file(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"roots": ["https://e.com/"], "max_depth": 3}))
        assert Config.load(path, max_depth=9).max_depth == 9

    def test_unknown_key_rejected(self):
        with pytest.raises(ValueError, match="unknown config keys"):
            Config.from_dict({"roots": [], "nonsense": 1})

    def test_relative_paths_resolve_under_project_root(self, tmp_path):
        cfg = Config(roots=[], project_root=str(tmp_path), output_dir="arch")
        assert cfg.archive_path == tmp_path / "arch"

    def test_absolute_paths_are_respected(self, tmp_path):
        cfg = Config(roots=[], project_root="/nowhere", output_dir=str(tmp_path / "a"))
        assert cfg.archive_path == tmp_path / "a"

    def test_ensure_dirs_creates_tree(self, tmp_path):
        cfg = Config(roots=[], project_root=str(tmp_path))
        cfg.ensure_dirs()
        assert cfg.archive_path.is_dir()
        assert cfg.logs_path.is_dir()
        assert cfg.exports_path.is_dir()

    def test_blank_roots_dropped(self):
        assert Config(roots=["https://e.com/", "", "  "]).roots == ["https://e.com/"]

    def test_normalization_kwargs_follow_flags(self):
        on = Config(roots=[], drop_tracking_params=True).normalization_kwargs()
        off = Config(roots=[], drop_tracking_params=False).normalization_kwargs()
        assert on["drop_params"] and not off["drop_params"]


class TestFailureClassification:
    @pytest.mark.parametrize("status,permanent,label", [
        (404, True, "HTTP_404"),
        (410, True, "HTTP_410"),
        (400, True, "HTTP_400"),
        (401, True, "ACCESS_RESTRICTED"),
        (403, True, "ACCESS_RESTRICTED"),
        (407, True, "ACCESS_RESTRICTED"),
        (429, False, "HTTP_429"),
        (500, False, "HTTP_500"),
        (502, False, "HTTP_502"),
        (503, False, "HTTP_503"),
        (504, False, "HTTP_504"),
        (408, False, "HTTP_408"),
    ])
    def test_classification(self, status, permanent, label):
        assert Fetcher.classify_failure(status) == (permanent, label)

    def test_retry_after_header(self):
        assert Fetcher._retry_after({"retry-after": "5"}) == 5.0
        assert Fetcher._retry_after({"retry-after": "not-a-number"}) is None
        assert Fetcher._retry_after({}) is None
        # An absurd Retry-After is clamped rather than obeyed literally.
        assert Fetcher._retry_after({"retry-after": "100000"}) == 120.0

    def test_backoff_grows_and_is_capped(self):
        cfg = Config(roots=[], backoff_base=2.0, backoff_cap=10.0)
        fetcher = Fetcher(cfg)
        first = [fetcher._backoff_delay(1) for _ in range(20)]
        later = [fetcher._backoff_delay(4) for _ in range(20)]
        assert sum(later) / len(later) > sum(first) / len(first)
        assert all(d <= 10.0 * 1.3 for d in [fetcher._backoff_delay(20) for _ in range(20)])


@pytest.mark.asyncio
class TestHostLimiter:
    async def test_enforces_minimum_delay(self):
        import time

        limiter = HostLimiter(concurrency=1, min_delay=0.05)
        start = time.monotonic()
        for _ in range(4):
            await limiter.acquire()
            limiter.release()
        # 4 acquisitions means at least 3 inter-request gaps.
        assert time.monotonic() - start >= 0.05 * 3 * 0.85

    async def test_set_delay_only_increases(self):
        limiter = HostLimiter(concurrency=1, min_delay=1.0)
        limiter.set_delay(0.1)
        assert limiter.min_delay == 1.0
        limiter.set_delay(3.0)
        assert limiter.min_delay == 3.0


class TestCli:
    def test_parses_defaults(self):
        from crawler.cli import build_parser, config_from_args

        args = build_parser().parse_args(["crawl", "https://e.com/"])
        cfg = config_from_args(args)
        assert args.command == "crawl"
        assert cfg.roots == ["https://e.com/"]
        assert cfg.honor_robots is True

    def test_command_defaults_to_crawl(self):
        from crawler.cli import build_parser

        assert build_parser().parse_args([]).command == "crawl"

    def test_flags_map_to_config(self):
        from crawler.cli import build_parser, config_from_args

        args = build_parser().parse_args([
            "crawl", "https://e.com/", "--max-depth", "2", "--delay", "1.5",
            "--concurrency", "6", "--no-render", "--ignore-robots",
            "--deny", "/admin", "--deny", "/tmp", "--force",
        ])
        cfg = config_from_args(args)
        assert cfg.max_depth == 2
        assert cfg.min_delay == 1.5
        assert cfg.concurrency == 6
        assert cfg.render is False
        assert cfg.honor_robots is False
        assert cfg.deny_patterns == ["/admin", "/tmp"]
        assert cfg.force is True

    def test_render_command_forces_rendering(self, tmp_path, monkeypatch):
        from crawler import cli

        captured = {}

        def fake_crawl(config, args, mode):
            captured["render_always"] = config.render_always
            captured["mode"] = mode
            return 0

        monkeypatch.setattr(cli, "cmd_crawl", fake_crawl)
        cli.main(["render", "https://e.com/", "--project-root", str(tmp_path)])
        assert captured["render_always"] is True

    def test_status_without_database_is_an_error_not_a_crash(self, tmp_path, capsys):
        from crawler import cli

        code = cli.main(["status", "--project-root", str(tmp_path)])
        assert code == 1
        assert "nothing crawled yet" in capsys.readouterr().out
