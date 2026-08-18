"""Freshness: avoid downloading unchanged pages while proving data is current.

Order of cheap-to-strong change signals (killer-feature doc, part 2):
1. conditional request (``ETag`` / ``Last-Modified`` -> 304);
2. normalized content hash (change even when the server does not send validators);
3. an adaptive per-URL interval so stable pages are polled less often.
"""

from web_scraper.freshness.store import FreshnessStore, FreshnessRecord, content_hash

__all__ = ["FreshnessStore", "FreshnessRecord", "content_hash"]
