"""Per-domain circuit breaker: stop hammering a domain that keeps hard-failing.

A run of identical hard verdicts (``BLOCKED``, ``ORIGIN_DOWN``) for one domain
means the domain is down or blocking wholesale; continuing wastes the window and,
once paid providers exist, would burn budget. The breaker opens after a threshold
and short-circuits further attempts for that domain until an ``OK`` resets it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from web_scraper.contracts import Verdict

HARD_FAILURE_VERDICTS = frozenset({Verdict.BLOCKED, Verdict.ORIGIN_DOWN})


@dataclass(frozen=True)
class BreakerState:
    open: bool
    verdict: Verdict | None
    consecutive: int


class CircuitBreaker:
    """Thread-safe per-domain consecutive-hard-failure breaker."""

    def __init__(self, *, threshold: int = 5) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self.threshold = threshold
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._last_verdict: dict[str, Verdict] = {}
        self._open: set[str] = set()

    def is_open(self, domain: str) -> bool:
        with self._lock:
            return domain in self._open

    def state(self, domain: str) -> BreakerState:
        with self._lock:
            return BreakerState(
                open=domain in self._open,
                verdict=self._last_verdict.get(domain),
                consecutive=self._counts.get(domain, 0),
            )

    def record(self, domain: str, verdict: Verdict) -> BreakerState:
        """Update the breaker with one outcome; returns the resulting state."""

        with self._lock:
            if verdict is Verdict.OK:
                self._counts.pop(domain, None)
                self._last_verdict.pop(domain, None)
                self._open.discard(domain)
                return BreakerState(open=False, verdict=Verdict.OK, consecutive=0)

            if verdict in HARD_FAILURE_VERDICTS and self._last_verdict.get(domain) == verdict:
                self._counts[domain] = self._counts.get(domain, 1) + 1
            elif verdict in HARD_FAILURE_VERDICTS:
                self._counts[domain] = 1
            else:
                # A different (non-hard) verdict breaks the streak without opening.
                self._counts.pop(domain, None)
                self._last_verdict.pop(domain, None)
                return BreakerState(open=domain in self._open, verdict=verdict, consecutive=0)

            self._last_verdict[domain] = verdict
            if self._counts[domain] >= self.threshold:
                self._open.add(domain)
            return BreakerState(
                open=domain in self._open,
                verdict=verdict,
                consecutive=self._counts[domain],
            )
