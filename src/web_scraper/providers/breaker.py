"""Circuit breakers for paid providers, per strategy rather than per provider.

Opening a breaker on the whole vendor because one mode failed is a mistake that
costs coverage: `normal` being blocked on a domain says nothing about whether
`super` would get through, and switching the vendor off entirely removes the
route that would have worked.

The distinction the free-route statistics already make is kept here too: a
verdict that describes the *target* — a dead URL, an origin outage, an auth wall
— never damages a strategy's reputation. Only failures the strategy could have
prevented count against it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from web_scraper.contracts import Verdict
from web_scraper.providers.base import ProviderErrorKind

#: Provider-side faults that count against a strategy.
COUNTS_AGAINST_STRATEGY = frozenset(
    {
        ProviderErrorKind.PROVIDER_FAULT,
        ProviderErrorKind.TIMEOUT,
        ProviderErrorKind.TRANSPORT,
        ProviderErrorKind.MALFORMED_RESPONSE,
    }
)

#: Provider errors that are about *us* or the account, not this strategy's
#: ability to fetch. They open the whole provider instead.
PROVIDER_WIDE = frozenset({ProviderErrorKind.AUTH, ProviderErrorKind.QUOTA})

#: Target verdicts that say nothing about the strategy — the same philosophy the
#: free route statistics use.
NEUTRAL_VERDICTS = frozenset(
    {
        Verdict.DEAD_URL,
        Verdict.ORIGIN_DOWN,
        Verdict.AUTH_REQUIRED,
        Verdict.ACCESS_DENIED,
        Verdict.RATE_LIMITED,
    }
)


@dataclass
class BreakerEntry:
    consecutive: int = 0
    open: bool = False
    reason: str = ""


@dataclass
class ProviderBreakers:
    """Consecutive-failure breakers keyed by provider and by provider:strategy."""

    threshold: int = 5
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _entries: dict[str, BreakerEntry] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("threshold must be >= 1")

    @staticmethod
    def _strategy_key(provider: str, strategy_id: str) -> str:
        return f"{provider}:{strategy_id}"

    def _entry(self, key: str) -> BreakerEntry:
        return self._entries.setdefault(key, BreakerEntry())

    def is_open(self, provider: str, strategy_id: str | None = None) -> bool:
        """Open when either the whole provider or this one strategy is tripped."""

        with self._lock:
            if self._entry(provider).open:
                return True
            if strategy_id is None:
                return False
            return self._entry(self._strategy_key(provider, strategy_id)).open

    def record_success(self, provider: str, strategy_id: str) -> None:
        """A validated success clears both breakers: the path demonstrably works."""

        with self._lock:
            for key in (provider, self._strategy_key(provider, strategy_id)):
                entry = self._entry(key)
                entry.consecutive = 0
                entry.open = False
                entry.reason = ""

    def record_error(self, provider: str, strategy_id: str, kind: ProviderErrorKind) -> None:
        """Count a provider-side failure against the strategy, or the vendor."""

        if kind in PROVIDER_WIDE:
            # Bad credentials or an exhausted quota are not this strategy's fault
            # and no other strategy will fare better.
            with self._lock:
                entry = self._entry(provider)
                entry.consecutive += 1
                entry.reason = kind.value
                entry.open = True  # immediate: retrying cannot help
            return
        if kind not in COUNTS_AGAINST_STRATEGY:
            return
        with self._lock:
            entry = self._entry(self._strategy_key(provider, strategy_id))
            entry.consecutive += 1
            entry.reason = kind.value
            if entry.consecutive >= self.threshold:
                entry.open = True

    def record_verdict(self, provider: str, strategy_id: str, verdict: Verdict) -> None:
        """Fold a target verdict in, ignoring the ones that are not about us."""

        if verdict in NEUTRAL_VERDICTS:
            return
        if verdict is Verdict.OK:
            self.record_success(provider, strategy_id)
            return
        with self._lock:
            entry = self._entry(self._strategy_key(provider, strategy_id))
            entry.consecutive += 1
            entry.reason = verdict.value
            if entry.consecutive >= self.threshold:
                entry.open = True

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                key: {"open": e.open, "consecutive": e.consecutive, "reason": e.reason}
                for key, e in self._entries.items()
                if e.consecutive or e.open
            }
