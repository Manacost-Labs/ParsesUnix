"""Durable per-route statistics.

Success here means **validated** success: a triage verdict of ``OK``, not an HTTP
200. That distinction is the whole point — a route that reliably returns a
challenge page with status 200 must score zero, not one.

Verdicts that say nothing about a route's ability to get past a site's defenses
(an origin outage, a dead URL, a rate limit) are recorded as *neutral*: they
count as attempts for observability but move neither the success nor the failure
estimator, so an origin outage cannot make a healthy route look broken and push
the router upward.
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from web_scraper.contracts import Verdict

#: Verdicts that prove the route reached real content.
SUCCESS_VERDICTS = frozenset({Verdict.OK, Verdict.NOT_MODIFIED})

#: Verdicts that prove the route was defeated by the site's defenses.
BLOCK_VERDICTS = frozenset({Verdict.BLOCKED, Verdict.SOFT_BLOCK})

#: Verdicts that say nothing about the route: they describe the resource or the
#: server, not our ability to fetch it. Never let these depress a route's score.
NEUTRAL_VERDICTS = frozenset(
    {
        Verdict.DEAD_URL,
        Verdict.ORIGIN_DOWN,
        Verdict.RATE_LIMITED,
        Verdict.AUTH_REQUIRED,
        Verdict.ACCESS_DENIED,
        Verdict.PROVIDER_ERROR,
    }
)

#: Weight of the newest observation in the EWMA. 0.25 keeps roughly the last ten
#: attempts meaningful without letting one bad response flip a route.
EWMA_ALPHA = 0.25


def wilson_lower_bound(successes: int, attempts: int, *, z: float = 1.96) -> float:
    """Lower bound of the 95% confidence interval for a success rate.

    Answers "how good is this route, pessimistically?" so that one lucky success
    out of one attempt does not outrank a route with 200 successes out of 205.
    """

    if attempts <= 0:
        return 0.0
    phat = successes / attempts
    denominator = 1 + z**2 / attempts
    centre = phat + z**2 / (2 * attempts)
    margin = z * math.sqrt((phat * (1 - phat) + z**2 / (4 * attempts)) / attempts)
    return max(0.0, (centre - margin) / denominator)


@dataclass(frozen=True)
class RouteKey:
    domain: str
    url_class: str
    route_type: str
    level: str

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.domain, self.url_class, self.route_type, self.level)


@dataclass(frozen=True)
class RouteStats:
    key: RouteKey
    attempts: int = 0
    scored_attempts: int = 0  # attempts that said something about this route
    validated_successes: int = 0
    blocks: int = 0
    soft_blocks: int = 0
    ewma_success: float = 0.0
    latency_ms: float = 0.0  # EWMA of observed latency
    cost_credits: Decimal = Decimal("0")
    last_success: float | None = None
    last_failure: float | None = None

    @property
    def success_rate(self) -> float:
        return self.validated_successes / self.scored_attempts if self.scored_attempts else 0.0

    @property
    def confidence_bound(self) -> float:
        """Pessimistic success estimate; low when the sample is small."""

        return wilson_lower_bound(self.validated_successes, self.scored_attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.key.domain,
            "url_class": self.key.url_class,
            "route": self.key.route_type,
            "level": self.key.level,
            "attempts": self.attempts,
            "scored_attempts": self.scored_attempts,
            "validated_successes": self.validated_successes,
            "blocks": self.blocks,
            "soft_blocks": self.soft_blocks,
            "success_rate": round(self.success_rate, 4),
            "recent_success_rate": round(self.ewma_success, 4),
            "confidence_bound": round(self.confidence_bound, 4),
            "latency_ms": round(self.latency_ms, 1),
            "cost_credits": str(self.cost_credits),
            "last_success": self.last_success,
            "last_failure": self.last_failure,
        }


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


class RouteStatsStore:
    """SQLite-backed route memory, shared across runs."""

    def __init__(self, path: str | Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS route_stats (
                    domain TEXT NOT NULL,
                    url_class TEXT NOT NULL,
                    route_type TEXT NOT NULL,
                    level TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    scored_attempts INTEGER NOT NULL DEFAULT 0,
                    validated_successes INTEGER NOT NULL DEFAULT 0,
                    blocks INTEGER NOT NULL DEFAULT 0,
                    soft_blocks INTEGER NOT NULL DEFAULT 0,
                    ewma_success REAL NOT NULL DEFAULT 0,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    cost_credits TEXT NOT NULL DEFAULT '0',
                    last_success REAL,
                    last_failure REAL,
                    PRIMARY KEY (domain, url_class, route_type, level)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def record(
        self,
        key: RouteKey,
        *,
        verdict: Verdict,
        latency_ms: float | None = None,
        cost_credits: Any = 0,
    ) -> RouteStats:
        """Fold one attempt into the route's memory and return the new state."""

        current = self.get(key) or RouteStats(key=key)
        now = self._now()

        is_success = verdict in SUCCESS_VERDICTS
        is_neutral = verdict in NEUTRAL_VERDICTS
        # PARSE_FAIL/THIN_CONTENT are route-relevant: the door opened but did not
        # deliver usable content, which is exactly what a route score should show.
        scored = not is_neutral

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

        updated = RouteStats(
            key=key,
            attempts=current.attempts + 1,
            scored_attempts=current.scored_attempts + (1 if scored else 0),
            validated_successes=current.validated_successes + (1 if is_success else 0),
            blocks=current.blocks + (1 if verdict is Verdict.BLOCKED else 0),
            soft_blocks=current.soft_blocks + (1 if verdict is Verdict.SOFT_BLOCK else 0),
            ewma_success=ewma,
            latency_ms=latency,
            cost_credits=current.cost_credits + _decimal(cost_credits),
            last_success=now if is_success else current.last_success,
            last_failure=now if (scored and not is_success) else current.last_failure,
        )
        self._write(updated)
        return updated

    def _write(self, stats: RouteStats) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO route_stats(
                    domain, url_class, route_type, level, attempts, scored_attempts,
                    validated_successes, blocks, soft_blocks, ewma_success, latency_ms,
                    cost_credits, last_success, last_failure
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain, url_class, route_type, level) DO UPDATE SET
                    attempts=excluded.attempts,
                    scored_attempts=excluded.scored_attempts,
                    validated_successes=excluded.validated_successes,
                    blocks=excluded.blocks,
                    soft_blocks=excluded.soft_blocks,
                    ewma_success=excluded.ewma_success,
                    latency_ms=excluded.latency_ms,
                    cost_credits=excluded.cost_credits,
                    last_success=excluded.last_success,
                    last_failure=excluded.last_failure
                """,
                (
                    *stats.key.as_tuple(),
                    stats.attempts,
                    stats.scored_attempts,
                    stats.validated_successes,
                    stats.blocks,
                    stats.soft_blocks,
                    stats.ewma_success,
                    stats.latency_ms,
                    str(stats.cost_credits),
                    stats.last_success,
                    stats.last_failure,
                ),
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> RouteStats:
        return RouteStats(
            key=RouteKey(row["domain"], row["url_class"], row["route_type"], row["level"]),
            attempts=row["attempts"],
            scored_attempts=row["scored_attempts"],
            validated_successes=row["validated_successes"],
            blocks=row["blocks"],
            soft_blocks=row["soft_blocks"],
            ewma_success=row["ewma_success"],
            latency_ms=row["latency_ms"],
            cost_credits=_decimal(row["cost_credits"]),
            last_success=row["last_success"],
            last_failure=row["last_failure"],
        )

    def get(self, key: RouteKey) -> RouteStats | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM route_stats
                WHERE domain = ? AND url_class = ? AND route_type = ? AND level = ?
                """,
                key.as_tuple(),
            ).fetchone()
        return self._from_row(row) if row else None

    def for_class(self, domain: str, url_class: str) -> list[RouteStats]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM route_stats WHERE domain = ? AND url_class = ? ORDER BY level",
                (domain, url_class),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def all_stats(self) -> list[RouteStats]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM route_stats ORDER BY domain, url_class").fetchall()
        return [self._from_row(row) for row in rows]
