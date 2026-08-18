"""Per-domain politeness: minimum intervals, jitter, and Retry-After."""

from __future__ import annotations

import random
import time
from typing import Callable, Mapping


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Read Retry-After seconds from response headers (dates are ignored)."""

    value = next(
        (item for key, item in headers.items() if str(key).lower() == "retry-after"), None
    )
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    return max(0.0, seconds)


class Pacer:
    """Spaces requests per domain and sleeps bounded backoffs.

    ``sleep``, ``clock``, and ``rng`` are injectable so tests never wait.
    """

    def __init__(
        self,
        *,
        min_interval_s: float = 1.0,
        jitter_s: float = 0.35,
        max_delay_s: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if min_interval_s < 0 or jitter_s < 0 or max_delay_s <= 0:
            raise ValueError("pacer intervals must be non-negative and max_delay_s positive")
        self.min_interval_s = min_interval_s
        self.jitter_s = jitter_s
        self.max_delay_s = max_delay_s
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._last_request: dict[str, float] = {}

    def pause(self, domain: str) -> float:
        """Wait until the polite interval (plus jitter) for the domain has passed."""

        now = self._clock()
        last = self._last_request.get(domain)
        slept = 0.0
        if last is not None:
            due = last + self.min_interval_s + self._rng() * self.jitter_s
            delay = due - now
            if delay > 0:
                self._sleep(delay)
                slept = delay
        self._last_request[domain] = self._clock()
        return slept

    def backoff(self, seconds: float) -> float:
        """Sleep a bounded backoff (used for Retry-After and origin failures)."""

        delay = min(max(0.0, seconds), self.max_delay_s)
        if delay > 0:
            self._sleep(delay)
        return delay
