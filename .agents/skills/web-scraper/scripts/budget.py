#!/usr/bin/env python3
"""Idempotent SQLite ledger for daily paid scraping budgets (thin CLI wrapper).

All logic lives in ``web_scraper.budget``; this file only re-exports it.
"""

from _bootstrap import ensure_web_scraper_on_path

ensure_web_scraper_on_path()

from web_scraper.budget import (  # noqa: E402,F401
    BudgetExceeded,
    BudgetLedger,
    Usage,
    main,
    scrape_do_request_cost,
)

if __name__ == "__main__":
    raise SystemExit(main())
