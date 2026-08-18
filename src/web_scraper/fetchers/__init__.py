"""Fetch Gateway L0-L2 adapters (implementation plan, stage 2).

This package intentionally holds no code yet: the gateway (Scrapling
sessions, warmup/TTL, concurrency, jitter, Retry-After, snapshots, and
the mandatory triage call after each attempt) lands here so that levels
and routes keep flowing through :mod:`web_scraper.contracts`.
"""

__all__: list[str] = []
