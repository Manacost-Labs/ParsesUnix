"""SQLite-backed URL queue: dedup, checkpoint/resume, quarantine, dead zones."""

from web_scraper.queue.normalize import normalize_url
from web_scraper.queue.store import QueuedUrl, QueueStore, UrlStatus

__all__ = ["QueueStore", "QueuedUrl", "UrlStatus", "normalize_url"]
