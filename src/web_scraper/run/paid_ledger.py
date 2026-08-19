"""One durable record per URL of whether money was ever spent on it.

The budget ledger answers "what did this run spend?". It cannot answer "have we
already paid for *this URL*?", because a reservation identifies a call, not a
target. After a crash that distinction is the whole game: the queue may still
show a URL as unresolved, the router will happily choose a strategy for it, and
the budget will happily grant a hold — and we pay a second time for a page we may
already have bought.

So the decision is written down before the money moves and updated after it
settles. A URL with a recorded attempt is never offered to the paid layer again
within a run, whatever the queue thinks.

The ordering matters as much as the content. The record is written **before**
the reservation, so a process that dies between the two leaves evidence that an
attempt was starting. Writing it afterwards would leave the crash window exactly
where the double payment happens.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from web_scraper.contracts import Cost, CostCertainty


class PaidAttemptState(StrEnum):
    """Where one URL's paid attempt got to."""

    #: Written before the reservation. A row stuck here means a process died
    #: between deciding to pay and holding the money.
    STARTED = "STARTED"
    #: The provider answered and the cost was settled.
    SETTLED = "SETTLED"
    #: The provider call failed, or its cost was never reported.
    UNKNOWN = "UNKNOWN"
    #: The attempt was refused before any call — budget, breaker, or policy.
    REFUSED = "REFUSED"

    @property
    def may_retry(self) -> bool:
        """Only a refusal leaves the URL safe to offer again.

        ``STARTED`` deliberately does not: we do not know whether that call
        reached the provider, and assuming it did not is how a crash turns into
        a double charge.
        """

        return self is PaidAttemptState.REFUSED


@dataclass(frozen=True)
class PaidAttemptRecord:
    url: str
    state: PaidAttemptState
    provider: str | None = None
    strategy_id: str | None = None
    reservation_id: str | None = None
    cost_credits: str | None = None
    cost_certainty: str | None = None
    verdict: str | None = None
    reason: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0

    @property
    def cost(self) -> Cost:
        if self.cost_certainty is None:
            return Cost.free()
        certainty = CostCertainty(self.cost_certainty)
        if certainty is CostCertainty.UNKNOWN:
            return Cost.unknown()
        if certainty is CostCertainty.PROVISIONAL:
            return Cost.provisional(self.cost_credits or "0")
        return Cost.of(self.cost_credits or "0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "state": self.state.value,
            "provider": self.provider,
            "strategy": self.strategy_id,
            "reservation_id": self.reservation_id,
            "cost": self.cost.to_dict(),
            "verdict": self.verdict,
            "reason": self.reason,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


class PaidAttemptLedger:
    """Durable per-URL record of paid attempts."""

    def __init__(self, path: str | Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paid_attempts (
                    url TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    provider TEXT,
                    strategy_id TEXT,
                    reservation_id TEXT,
                    cost_credits TEXT,
                    cost_certainty TEXT,
                    verdict TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS paid_attempts_state ON paid_attempts(state)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # -- the gate ----------------------------------------------------------

    def may_attempt(self, url: str) -> bool:
        """Is this URL still safe to offer to the paid layer?"""

        record = self.get(url)
        return record is None or record.state.may_retry

    def blocked_reason(self, url: str) -> str | None:
        record = self.get(url)
        if record is None or record.state.may_retry:
            return None
        if record.state is PaidAttemptState.STARTED:
            return (
                "a paid attempt was started for this URL and never completed; "
                "it may already have been billed"
            )
        return f"already attempted via {record.provider}:{record.strategy_id}"

    # -- writes ------------------------------------------------------------

    def start(self, url: str, *, provider: str, strategy_id: str) -> None:
        """Record the intent to pay, BEFORE any money is held."""

        now = self._now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO paid_attempts
                    (url, state, provider, strategy_id, started_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    state=excluded.state,
                    provider=excluded.provider,
                    strategy_id=excluded.strategy_id,
                    updated_at=excluded.updated_at
                """,
                (url, PaidAttemptState.STARTED.value, provider, strategy_id, now, now),
            )

    def finish(
        self,
        url: str,
        *,
        state: PaidAttemptState,
        cost: Cost | None = None,
        verdict: str | None = None,
        reservation_id: str | None = None,
        provider_hint: str | None = None,
        reason: str = "",
    ) -> None:
        credits = None if cost is None or cost.credits is None else str(cost.credits)
        certainty = None if cost is None else cost.certainty.value
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO paid_attempts
                    (url, state, provider, cost_credits, cost_certainty, verdict,
                     reservation_id, reason, started_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    state=excluded.state,
                    provider=COALESCE(excluded.provider, paid_attempts.provider),
                    cost_credits=excluded.cost_credits,
                    cost_certainty=excluded.cost_certainty,
                    verdict=excluded.verdict,
                    reservation_id=excluded.reservation_id,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (
                    url,
                    state.value,
                    provider_hint,
                    credits,
                    certainty,
                    verdict,
                    reservation_id,
                    reason,
                    self._now(),
                    self._now(),
                ),
            )

    # -- reads -------------------------------------------------------------

    def get(self, url: str) -> PaidAttemptRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM paid_attempts WHERE url = ?", (url,)).fetchone()
        return None if row is None else _from_row(row)

    def all_records(self) -> list[PaidAttemptRecord]:
        with closing(self._connect()) as conn:
            return [
                _from_row(row)
                for row in conn.execute("SELECT * FROM paid_attempts ORDER BY started_at")
            ]

    def stranded(self) -> list[PaidAttemptRecord]:
        """Attempts that started and never finished. Each needs a human.

        These are the rows that keep a URL out of the paid layer forever until
        someone reconciles them, which is the correct default: an unreconciled
        maybe-charge must not become a second charge.
        """

        with closing(self._connect()) as conn:
            return [
                _from_row(row)
                for row in conn.execute(
                    "SELECT * FROM paid_attempts WHERE state = ?",
                    (PaidAttemptState.STARTED.value,),
                )
            ]

    def summary(self) -> dict[str, Any]:
        records = self.all_records()
        by_state: dict[str, int] = {}
        known = Decimal("0")
        unknown = 0
        for record in records:
            by_state[record.state.value] = by_state.get(record.state.value, 0) + 1
            cost = record.cost
            if cost.is_known:
                known += cost.known_credits
            elif record.state is not PaidAttemptState.REFUSED:
                unknown += 1
        return {
            "urls_with_paid_attempt": len(records),
            "by_state": by_state,
            "known_credits": str(known),
            "unknown_cost_calls": unknown,
            "stranded": len(self.stranded()),
        }


def _from_row(row: sqlite3.Row) -> PaidAttemptRecord:
    return PaidAttemptRecord(
        url=row["url"],
        state=PaidAttemptState(row["state"]),
        provider=row["provider"],
        strategy_id=row["strategy_id"],
        reservation_id=row["reservation_id"],
        cost_credits=row["cost_credits"],
        cost_certainty=row["cost_certainty"],
        verdict=row["verdict"],
        reason=row["reason"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
    )
