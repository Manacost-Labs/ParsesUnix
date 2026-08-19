"""Circuit breakers for paid providers, per strategy rather than per provider.

Opening a breaker on the whole vendor because one mode failed is a mistake that
costs coverage: ``normal`` being blocked on a domain says nothing about whether
``super`` would get through, and switching the vendor off entirely removes the
route that would have worked.

The distinction the free-route statistics already make is kept here too: a
verdict that describes the *target* — a dead URL, an origin outage, an auth wall
— never damages a strategy's reputation. Only failures the strategy could have
prevented count against it.

**A breaker that never closes is an outage of our own making.** Each one is a
state machine:

.. code-block:: text

    CLOSED --failures reach threshold--> OPEN
    OPEN --cooldown elapses--> HALF_OPEN
    HALF_OPEN --one probe succeeds--> CLOSED
    HALF_OPEN --that probe fails--> OPEN (longer cooldown)

HALF_OPEN admits exactly **one** call. Letting a whole batch through the moment
a cooldown expires would turn recovery into a second incident, paid for at
provider rates.

Two failures are deliberately not on a timer. ``AUTH`` means our credentials are
wrong and no amount of waiting fixes that, so it stays open until a human clears
it. ``QUOTA`` does resolve on its own when the billing window rolls over, so it
gets a long cooldown rather than a manual gate.

State is persisted when a store is supplied. Without persistence a restart
forgets that a provider is refusing us, and the first thing the new process does
is spend money finding out again.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
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

#: Of those, the one no timer can fix.
NEEDS_HUMAN = frozenset({ProviderErrorKind.AUTH})

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

#: First cooldown after a strategy trips.
DEFAULT_COOLDOWN_SECONDS = 60.0

#: Each repeat trip doubles the wait, up to this. A provider having a bad hour
#: should not be probed every minute for that hour.
DEFAULT_MAX_COOLDOWN_SECONDS = 900.0

#: A quota resets with the billing window, not in a minute.
DEFAULT_QUOTA_COOLDOWN_SECONDS = 3600.0


class BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class BreakerEntry:
    """One breaker's memory. ``trips`` drives the backoff, so it is not reset by
    a cooldown — only by a success that proves the path works again."""

    key: str = ""
    state: BreakerState = BreakerState.CLOSED
    consecutive: int = 0
    reason: str = ""
    opened_at: float | None = None
    cooldown_seconds: float = 0.0
    trips: int = 0
    #: True while a HALF_OPEN probe is in flight, so only one call gets through.
    probe_in_flight: bool = False
    #: AUTH and similar: a timer must not reopen the gate.
    manual_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "state": self.state.value,
            "consecutive": self.consecutive,
            "reason": self.reason,
            "opened_at": self.opened_at,
            "cooldown_seconds": self.cooldown_seconds,
            "trips": self.trips,
            "manual_only": self.manual_only,
        }


@dataclass(frozen=True)
class Admission:
    """Whether one call may go out, and whether it is the single trial call."""

    allowed: bool
    reason: str
    is_probe: bool = False


class BreakerStore:
    """SQLite persistence so a restart does not forget a refusing provider."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_breakers (
                    key TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    consecutive INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    opened_at REAL,
                    cooldown_seconds REAL NOT NULL DEFAULT 0,
                    trips INTEGER NOT NULL DEFAULT 0,
                    manual_only INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def load(self) -> dict[str, BreakerEntry]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM provider_breakers").fetchall()
        # probe_in_flight is deliberately not persisted: a probe that was in
        # flight when the process died is over, and restoring it as "in flight"
        # would wedge the breaker shut forever.
        return {
            row["key"]: BreakerEntry(
                key=row["key"],
                state=BreakerState(row["state"]),
                consecutive=row["consecutive"],
                reason=row["reason"],
                opened_at=row["opened_at"],
                cooldown_seconds=row["cooldown_seconds"],
                trips=row["trips"],
                manual_only=bool(row["manual_only"]),
            )
            for row in rows
        }

    def save(self, entry: BreakerEntry) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO provider_breakers
                    (key, state, consecutive, reason, opened_at, cooldown_seconds,
                     trips, manual_only)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    state=excluded.state,
                    consecutive=excluded.consecutive,
                    reason=excluded.reason,
                    opened_at=excluded.opened_at,
                    cooldown_seconds=excluded.cooldown_seconds,
                    trips=excluded.trips,
                    manual_only=excluded.manual_only
                """,
                (
                    entry.key,
                    entry.state.value,
                    entry.consecutive,
                    entry.reason,
                    entry.opened_at,
                    entry.cooldown_seconds,
                    entry.trips,
                    int(entry.manual_only),
                ),
            )


@dataclass
class ProviderBreakers:
    """Recovering breakers keyed by provider and by provider:strategy."""

    threshold: int = 5
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    max_cooldown_seconds: float = DEFAULT_MAX_COOLDOWN_SECONDS
    quota_cooldown_seconds: float = DEFAULT_QUOTA_COOLDOWN_SECONDS
    store: BreakerStore | None = None
    clock: Callable[[], float] = time.time
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _entries: dict[str, BreakerEntry] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("threshold must be >= 1")
        if self.store is not None:
            self._entries = self.store.load()

    # -- keys --------------------------------------------------------------

    @staticmethod
    def _strategy_key(provider: str, strategy_id: str) -> str:
        return f"{provider}:{strategy_id}"

    def _entry(self, key: str) -> BreakerEntry:
        entry = self._entries.get(key)
        if entry is None:
            entry = BreakerEntry(key=key)
            self._entries[key] = entry
        return entry

    def _persist(self, entry: BreakerEntry) -> None:
        if self.store is not None:
            self.store.save(entry)

    # -- transitions -------------------------------------------------------

    def _refresh_locked(self, entry: BreakerEntry) -> BreakerEntry:
        """Move OPEN to HALF_OPEN once the cooldown has elapsed."""

        if entry.state is not BreakerState.OPEN or entry.manual_only:
            return entry
        if entry.opened_at is None:
            return entry
        if self.clock() - entry.opened_at >= entry.cooldown_seconds:
            entry.state = BreakerState.HALF_OPEN
            entry.probe_in_flight = False
            self._persist(entry)
        return entry

    def _trip_locked(self, entry: BreakerEntry, *, reason: str, manual_only: bool = False) -> None:
        entry.trips += 1
        entry.state = BreakerState.OPEN
        entry.reason = reason
        entry.opened_at = self.clock()
        entry.manual_only = manual_only
        entry.probe_in_flight = False
        # Exponential backoff, capped: repeated trips mean the provider needs
        # longer than we first guessed, not more frequent poking.
        base = self.cooldown_seconds
        entry.cooldown_seconds = min(
            base * (2 ** max(0, entry.trips - 1)), self.max_cooldown_seconds
        )
        self._persist(entry)

    def _close_locked(self, entry: BreakerEntry) -> None:
        entry.state = BreakerState.CLOSED
        entry.consecutive = 0
        entry.trips = 0
        entry.reason = ""
        entry.opened_at = None
        entry.cooldown_seconds = 0.0
        entry.probe_in_flight = False
        entry.manual_only = False
        self._persist(entry)

    # -- queries -----------------------------------------------------------

    def is_open(self, provider: str, strategy_id: str | None = None) -> bool:
        """True when calls must not go out right now.

        A breaker whose cooldown has expired is *not* open: it is HALF_OPEN and
        will admit one trial call.
        """

        with self._lock:
            if self._refresh_locked(self._entry(provider)).state is BreakerState.OPEN:
                return True
            if strategy_id is None:
                return False
            key = self._strategy_key(provider, strategy_id)
            return self._refresh_locked(self._entry(key)).state is BreakerState.OPEN

    def state_of(self, provider: str, strategy_id: str | None = None) -> BreakerState:
        with self._lock:
            entry = self._refresh_locked(self._entry(provider))
            if entry.state is BreakerState.OPEN:
                return BreakerState.OPEN
            if strategy_id is None:
                return entry.state
            key = self._strategy_key(provider, strategy_id)
            return self._refresh_locked(self._entry(key)).state

    # -- admission ---------------------------------------------------------

    def admit(self, provider: str, strategy_id: str) -> Admission:
        """Claim permission for exactly one call.

        Must be paired with :meth:`record_success` / :meth:`record_error` /
        :meth:`record_verdict`, which release a claimed probe slot.
        """

        with self._lock:
            provider_entry = self._refresh_locked(self._entry(provider))
            if provider_entry.state is BreakerState.OPEN:
                detail = " (needs a human)" if provider_entry.manual_only else ""
                return Admission(False, f"{provider} breaker open: {provider_entry.reason}{detail}")

            strategy_entry = self._refresh_locked(
                self._entry(self._strategy_key(provider, strategy_id))
            )
            if strategy_entry.state is BreakerState.OPEN:
                return Admission(
                    False, f"{provider}:{strategy_id} breaker open: {strategy_entry.reason}"
                )

            probing = [
                e for e in (provider_entry, strategy_entry) if e.state is BreakerState.HALF_OPEN
            ]
            if not probing:
                return Admission(True, "breaker closed")

            for entry in probing:
                if entry.probe_in_flight:
                    return Admission(False, f"{entry.key} is already being probed")
            for entry in probing:
                entry.probe_in_flight = True
            return Admission(True, "half-open trial call", is_probe=True)

    # -- outcomes ----------------------------------------------------------

    def record_success(self, provider: str, strategy_id: str) -> None:
        """A validated success closes both breakers: the path demonstrably works."""

        with self._lock:
            for key in (provider, self._strategy_key(provider, strategy_id)):
                self._close_locked(self._entry(key))

    def record_error(self, provider: str, strategy_id: str, kind: ProviderErrorKind) -> None:
        """Count a provider-side failure against the strategy, or the vendor."""

        with self._lock:
            if kind in PROVIDER_WIDE:
                # Bad credentials or an exhausted quota are not this strategy's
                # fault and no other strategy will fare better.
                entry = self._entry(provider)
                entry.consecutive += 1
                needs_human = kind in NEEDS_HUMAN
                self._trip_locked(entry, reason=kind.value, manual_only=needs_human)
                if not needs_human:
                    # A quota returns on the billing clock, not on our backoff.
                    entry.cooldown_seconds = self.quota_cooldown_seconds
                    self._persist(entry)
                return
            if kind not in COUNTS_AGAINST_STRATEGY:
                self._release_probe_locked(provider, strategy_id)
                return
            self._fail_locked(provider, strategy_id, kind.value)

    def record_verdict(self, provider: str, strategy_id: str, verdict: Verdict) -> None:
        """Fold a target verdict in, ignoring the ones that are not about us."""

        if verdict in NEUTRAL_VERDICTS:
            # Neutral outcomes must not consume the single probe slot either:
            # an origin outage is not a trial of this strategy.
            with self._lock:
                self._release_probe_locked(provider, strategy_id)
            return
        if verdict is Verdict.OK:
            self.record_success(provider, strategy_id)
            return
        with self._lock:
            self._fail_locked(provider, strategy_id, verdict.value)

    def _fail_locked(self, provider: str, strategy_id: str, reason: str) -> None:
        entry = self._entry(self._strategy_key(provider, strategy_id))
        if entry.state is BreakerState.HALF_OPEN:
            # The trial call failed: straight back to OPEN, with a longer wait.
            self._trip_locked(entry, reason=reason)
            return
        entry.consecutive += 1
        entry.reason = reason
        if entry.consecutive >= self.threshold:
            self._trip_locked(entry, reason=reason)
        else:
            self._persist(entry)

    def _release_probe_locked(self, provider: str, strategy_id: str) -> None:
        for key in (provider, self._strategy_key(provider, strategy_id)):
            entry = self._entries.get(key)
            if entry is not None:
                entry.probe_in_flight = False

    def release_probe(self, provider: str, strategy_id: str) -> None:
        """Give back a claimed trial slot when the call never happened."""

        with self._lock:
            self._release_probe_locked(provider, strategy_id)

    # -- operations --------------------------------------------------------

    def clear(self, provider: str, strategy_id: str | None = None) -> None:
        """Human intervention: credentials rotated, quota topped up."""

        with self._lock:
            key = provider if strategy_id is None else self._strategy_key(provider, strategy_id)
            self._close_locked(self._entry(key))

    def state(self) -> dict[str, Any]:
        with self._lock:
            for entry in list(self._entries.values()):
                self._refresh_locked(entry)
            return {
                key: entry.to_dict()
                for key, entry in self._entries.items()
                if entry.state is not BreakerState.CLOSED or entry.consecutive
            }
