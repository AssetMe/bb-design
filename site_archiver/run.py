#!/usr/bin/env python3
"""Entry point for the site archiver.  See `python run.py --help`."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from crawler.cli import main

if __name__ == "__main__":
    sys.exit(main())
