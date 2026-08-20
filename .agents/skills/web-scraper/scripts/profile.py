#!/usr/bin/env python3
"""Validate or draft Site Profiles without network access (thin CLI wrapper).

All logic lives in ``web_scraper.profiles``; this file only re-exports it.
"""

from _bootstrap import ensure_web_scraper_on_path

ensure_web_scraper_on_path()

from web_scraper.profiles import (  # noqa: E402,F401
    ProfileError,
    SiteProfile,
    draft_profile_from_probe,
    load_profile,
    merge_api_candidate,
    parse_profile,
)
from web_scraper.profiles.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
