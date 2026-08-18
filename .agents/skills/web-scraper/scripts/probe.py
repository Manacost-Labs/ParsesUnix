#!/usr/bin/env python3
"""Safe static reconnaissance for a public web target (thin CLI wrapper).

All logic lives in ``web_scraper.probe``; this file only re-exports it.
"""

from _bootstrap import ensure_web_scraper_on_path

ensure_web_scraper_on_path()

from web_scraper.probe import (  # noqa: E402,F401
    PROBE_REPORT_SCHEMA,
    FetchResult,
    ProbeReport,
    UnsafeTarget,
    probe,
    validate_public_url,
)
from web_scraper.probe.cli import main  # noqa: E402,F401

if __name__ == "__main__":
    raise SystemExit(main())
