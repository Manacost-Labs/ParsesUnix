"""SQLite-backed URL queue: dedup, checkpoint/resume, quarantine, dead zones."""

from web_scraper.queue.store import (
    QueueStore,
    QueuedUrl,
    UrlStatus,
    normalize_url,
)

__all__ = ["QueueStore", "QueuedUrl", "UrlStatus", "normalize_url"]
