"""Per-strategy provider memory, and the metric that actually matters.

Cost per request is the number vendors advertise. It is the wrong one. A
provider charging 1 credit that validates half the time costs 2 credits per
usable result; a provider charging 1.5 that validates almost always costs about
1.52. Ranked by list price the first looks 33% cheaper and is in fact 30% more
expensive. :attr:`ProviderStrategyStats.cost_per_valid_result` is the number the
router ranks on once there is enough history to compute it.

Two rules carry over unchanged from the free route statistics, for the same
reasons:

* **Neutral outcomes never damage reputation.** A dead URL, an origin outage or
  an auth wall says nothing about whether the strategy can fetch. Counting them
  as failures would retire working strategies during someone else's incident.
* **Identity is never merged.** Statistics are keyed by provider *and* strategy
  *and* domain *and* url_class. ``scrape_do:normal`` failing on one domain is not
  evidence about ``brightdata:unlocker``, and averaging them produces a number
  that describes nothing real.

Unknown costs are counted, not summed. A strategy whose spend is partly
unattributed reports an incomplete cost, and the router is told so rather than
being handed a total that looks small because half of it went missing.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from web_scraper.contracts import Cost, Verdict
from web_scraper.routing.stats import wilson_lower_bound

#: Smoothing for the reactive success signal. Matches the free route stats.
EWMA_ALPHA = 0.3

#: Verdicts about the TARGET, not about the strategy. See the module docstring.
NEUTRAL_VERDICTS = frozenset(
    {
        Verdict.DEAD_URL,
        Verdict.ORIGIN_DOWN,
        Verdict.AUTH_REQUIRED,
        Verdict.ACCESS_DENIED,
        Verdict.RATE_LIMITED,
        Verdict.NOT_MODIFIED,
    }
)

#: The only verdict that counts as this strategy having done its job.
SUCCESS_VERDICTS = frozenset({Verdict.OK})


@dataclass(frozen=True)
class ProviderStrategyKey:
    """Identity of one strategy on one kind of page of one site."""

    provider: str
    strategy_id: str
    domain: str
    url_class: str

    @property
    def strategy_ref(self) -> str:
        """Stable cross-provider identity, e.g. ``scrape_do:normal``."""

        return f"{self.provider}:{self.strategy_id}"

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "strategy": self.strategy_id,
            "strategy_ref": self.strategy_ref,
            "domain": self.domain,
            "url_class": self.url_class,
        }


@dataclass(frozen=True)
class ProviderStrategyStats:
    """What one strategy has actually done here."""

    key: ProviderStrategyKey
    attempts: int = 0
    #: Attempts that say something about this strategy — neutral ones excluded.
    scored_attempts: int = 0
    validated_successes: int = 0
    blocks: int = 0
    provider_errors: int = 0
    neutral_outcomes: int = 0
    ewma_success: float = 0.0
    latency_ms: float = 0.0
    #: Sum of costs we know. A floor when ``unknown_cost_calls`` is non-zero.
    known_cost: Decimal = Decimal("0")
    unknown_cost_calls: int = 0
    last_success: float | None = None
    last_failure: float | None = None

    @property
    def confidence_bound(self) -> float:
        """Wilson lower bound of validated success. The safety gate."""

        return wilson_lower_bound(self.validated_successes, self.scored_attempts)

    @property
    def success_rate(self) -> float | None:
        """Point estimate. The expectation, as distinct from the safety gate.

        ``None`` with no scored attempts — a rate of zero would read as "known to
        always fail", which is the opposite of "never tried".
        """

        if self.scored_attempts == 0:
            return None
        return self.validated_successes / self.scored_attempts

    @property
    def cost_is_complete(self) -> bool:
        return self.unknown_cost_calls == 0

    @property
    def cost_per_valid_result(self) -> Decimal | None:
        """Credits actually spent per usable result. ``None`` when unknowable.

        Unknowable in two distinct ways, both of which must not be reported as a
        number: no validated success yet (division by zero), or spend that was
        never attributed (the numerator is a floor, so the ratio would understate
        the true cost).
        """

        if self.validated_successes == 0 or not self.cost_is_complete:
            return None
        return self.known_cost / Decimal(self.validated_successes)

    def to_dict(self) -> dict[str, Any]:
        cpvr = self.cost_per_valid_result
        return {
            **self.key.to_dict(),
            "attempts": self.attempts,
            "scored_attempts": self.scored_attempts,
            "validated_successes": self.validated_successes,
            "blocks": self.blocks,
            "provider_errors": self.provider_errors,
            "neutral_outcomes": self.neutral_outcomes,
            "ewma_success": round(self.ewma_success, 4),
            "confidence_bound": round(self.confidence_bound, 4),
            "success_rate": None if self.success_rate is None else round(self.success_rate, 4),
            "latency_ms": round(self.latency_ms, 1),
            "known_cost": str(self.known_cost),
            "cost_is_complete": self.cost_is_complete,
            "unknown_cost_calls": self.unknown_cost_calls,
            "cost_per_valid_result": None if cpvr is None else str(cpvr),
            "last_success": self.last_success,
            "last_failure": self.last_failure,
        }


class ProviderStatsStore:
    """SQLite memory of what each paid strategy achieved, shared across runs."""

    def __init__(self, path: str | Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_stats (
                    provider TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    url_class TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    scored_attempts INTEGER NOT NULL DEFAULT 0,
                    validated_successes INTEGER NOT NULL DEFAULT 0,
                    blocks INTEGER NOT NULL DEFAULT 0,
                    provider_errors INTEGER NOT NULL DEFAULT 0,
                    neutral_outcomes INTEGER NOT NULL DEFAULT 0,
                    ewma_success REAL NOT NULL DEFAULT 0,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    known_cost TEXT NOT NULL DEFAULT '0',
                    unknown_cost_calls INTEGER NOT NULL DEFAULT 0,
                    last_success REAL,
                    last_failure REAL,
                    PRIMARY KEY (provider, strategy_id, domain, url_class)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get(self, key: ProviderStrategyKey) -> ProviderStrategyStats | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM provider_stats
                WHERE provider=? AND strategy_id=? AND domain=? AND url_class=?
                """,
                (key.provider, key.strategy_id, key.domain, key.url_class),
            ).fetchone()
        return None if row is None else _from_row(key, row)

    def record(
        self,
        key: ProviderStrategyKey,
        *,
        verdict: Verdict | None = None,
        provider_error: bool = False,
        cost: Cost | None = None,
        latency_ms: float | None = None,
    ) -> ProviderStrategyStats:
        """Fold one paid attempt into this strategy's memory.

        ``provider_error`` is separate from a verdict on purpose: the provider
        failing to answer is not the site refusing us, and mixing them would let
        a vendor outage look like a site that got harder.
        """

        current = self.get(key) or ProviderStrategyStats(key=key)
        now = self._now()

        is_neutral = verdict in NEUTRAL_VERDICTS if verdict is not None else False
        is_success = verdict in SUCCESS_VERDICTS if verdict is not None else False
        # A provider error is scored: it is exactly the kind of failure this
        # strategy could have avoided, and it is why a strategy gets retired.
        scored = provider_error or (verdict is not None and not is_neutral)

        ewma = current.ewma_success
        if scored:
            ewma = EWMA_ALPHA * (1.0 if is_success else 0.0) + (1 - EWMA_ALPHA) * ewma

        latency = current.latency_ms
        if latency_ms is not None:
            latency = (
                latency_ms
                if current.attempts == 0
                else EWMA_ALPHA * latency_ms + (1 - EWMA_ALPHA) * latency
            )

        known_cost = current.known_cost
        unknown_calls = current.unknown_cost_calls
        if cost is not None:
            if cost.is_known:
                known_cost += cost.known_credits
            else:
                # Never add zero for an unknown: it would make this strategy
                # look cheaper than it is, which is how the router picks it.
                unknown_calls += 1

        updated = ProviderStrategyStats(
            key=key,
            attempts=current.attempts + 1,
            scored_attempts=current.scored_attempts + (1 if scored else 0),
            validated_successes=current.validated_successes + (1 if is_success else 0),
            blocks=current.blocks + (1 if verdict in {Verdict.BLOCKED, Verdict.SOFT_BLOCK} else 0),
            provider_errors=current.provider_errors + (1 if provider_error else 0),
            neutral_outcomes=current.neutral_outcomes + (1 if is_neutral else 0),
            ewma_success=ewma,
            latency_ms=latency,
            known_cost=known_cost,
            unknown_cost_calls=unknown_calls,
            last_success=now if is_success else current.last_success,
            last_failure=now if (scored and not is_success) else current.last_failure,
        )
        self._write(updated)
        return updated

    def _write(self, stats: ProviderStrategyStats) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO provider_stats
                    (provider, strategy_id, domain, url_class, attempts, scored_attempts,
                     validated_successes, blocks, provider_errors, neutral_outcomes,
                     ewma_success, latency_ms, known_cost, unknown_cost_calls,
                     last_success, last_failure)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, strategy_id, domain, url_class) DO UPDATE SET
                    attempts=excluded.attempts,
                    scored_attempts=excluded.scored_attempts,
                    validated_successes=excluded.validated_successes,
                    blocks=excluded.blocks,
                    provider_errors=excluded.provider_errors,
                    neutral_outcomes=excluded.neutral_outcomes,
                    ewma_success=excluded.ewma_success,
                    latency_ms=excluded.latency_ms,
                    known_cost=excluded.known_cost,
                    unknown_cost_calls=excluded.unknown_cost_calls,
                    last_success=excluded.last_success,
                    last_failure=excluded.last_failure
                """,
                (
                    stats.key.provider,
                    stats.key.strategy_id,
                    stats.key.domain,
                    stats.key.url_class,
                    stats.attempts,
                    stats.scored_attempts,
                    stats.validated_successes,
                    stats.blocks,
                    stats.provider_errors,
                    stats.neutral_outcomes,
                    stats.ewma_success,
                    stats.latency_ms,
                    str(stats.known_cost),
                    stats.unknown_cost_calls,
                    stats.last_success,
                    stats.last_failure,
                ),
            )

    def all_stats(self) -> list[ProviderStrategyStats]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM provider_stats ORDER BY provider, strategy_id")
            return [
                _from_row(
                    ProviderStrategyKey(
                        provider=row["provider"],
                        strategy_id=row["strategy_id"],
                        domain=row["domain"],
                        url_class=row["url_class"],
                    ),
                    row,
                )
                for row in rows
            ]


def _from_row(key: ProviderStrategyKey, row: sqlite3.Row) -> ProviderStrategyStats:
    return ProviderStrategyStats(
        key=key,
        attempts=row["attempts"],
        scored_attempts=row["scored_attempts"],
        validated_successes=row["validated_successes"],
        blocks=row["blocks"],
        provider_errors=row["provider_errors"],
        neutral_outcomes=row["neutral_outcomes"],
        ewma_success=row["ewma_success"],
        latency_ms=row["latency_ms"],
        known_cost=Decimal(str(row["known_cost"])),
        unknown_cost_calls=row["unknown_cost_calls"],
        last_success=row["last_success"],
        last_failure=row["last_failure"],
    )
