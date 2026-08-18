#!/usr/bin/env python3
"""Fail if the provider reference `last_reviewed` date is older than a threshold.

The web-scraper skill requires provider docs (scrape.do, Firecrawl, Bright Data)
to be re-verified against the live sites at least every 60 days. Nothing else in
the repo enforces that, so CI does: this is the staleness watchdog.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

REFERENCE = Path(".agents/skills/web-scraper/references/providers-and-stacks.md")
PATTERN = re.compile(r"last_reviewed:\s*(\d{4}-\d{2}-\d{2})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-days", type=int, default=60)
    parser.add_argument("--reference", type=Path, default=REFERENCE)
    parser.add_argument("--today", help="ISO date override for testing (default: real today)")
    args = parser.parse_args(argv)

    if not args.reference.is_file():
        print(f"ERROR: {args.reference} not found", file=sys.stderr)
        return 2
    match = PATTERN.search(args.reference.read_text(encoding="utf-8"))
    if not match:
        print(f"ERROR: no `last_reviewed:` marker in {args.reference}", file=sys.stderr)
        return 2

    reviewed = dt.date.fromisoformat(match.group(1))
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    age = (today - reviewed).days
    if age > args.max_age_days:
        print(
            f"STALE: provider docs last reviewed {reviewed} ({age} days ago, "
            f"limit {args.max_age_days}). Re-verify scrape.do / Firecrawl / "
            f"Bright Data docs and bump last_reviewed.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: provider docs reviewed {reviewed} ({age} days ago, limit {args.max_age_days}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
