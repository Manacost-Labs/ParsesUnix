#!/usr/bin/env python3
"""Canonical response triage for the web-scraper skill (thin CLI wrapper).

All logic lives in ``web_scraper.triage``; this file only re-exports it.
"""

from _bootstrap import ensure_web_scraper_on_path

ensure_web_scraper_on_path()

from web_scraper.contracts import (  # noqa: E402,F401
    PAID_ESCALATION_VERDICTS,
    ContentRules,
    TriageResult,
    Verdict,
)
from web_scraper.triage import (  # noqa: E402,F401
    ACCESS_DENIED_SIGNATURES,
    BLOCK_SIGNATURES,
    classify_response,
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
