"""Per-domain session reuse with warmup and TTL."""

from __future__ import annotations

import time
from typing import Callable

from web_scraper.fetchers.base import Transport


class SessionPool:
    """Creates one transport per domain, re-created after the TTL expires.

    ``ttl_minutes == 0`` disables reuse (a fresh session per request).
    Warmup is a best-effort GET of the given URL right after creation.
    """

    def __init__(
        self,
        factory: Callable[[str], Transport],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._sessions: dict[str, tuple[Transport, float]] = {}
        self.warmup_urls: list[str] = []

    def get(self, domain: str, *, ttl_minutes: int, warmup_url: str | None = None) -> Transport:
        now = self._clock()
        entry = self._sessions.get(domain)
        if entry is not None and ttl_minutes > 0 and now - entry[1] < ttl_minutes * 60:
            return entry[0]

        transport = self._factory(domain)
        if warmup_url:
            self.warmup_urls.append(warmup_url)
            try:
                transport.fetch(warmup_url)
            except Exception:
                pass  # warmup must never fail the real attempt
        self._sessions[domain] = (transport, now)
        return transport

    def discard(self, domain: str) -> None:
        self._sessions.pop(domain, None)
