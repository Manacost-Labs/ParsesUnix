"""Locate the web_scraper package for the thin CLI wrappers in this directory."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_web_scraper_on_path() -> None:
    try:
        import web_scraper  # noqa: F401

        return
    except ModuleNotFoundError:
        pass
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "src" / "web_scraper" / "__init__.py"
        if candidate.is_file():
            sys.path.insert(0, str(candidate.parents[1]))
            return
    raise ModuleNotFoundError(
        "web_scraper package not found: install web-scraper-core or keep this skill inside its repository"
    )
