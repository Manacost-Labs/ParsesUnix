"""Run metrics aggregation and a per-URL run report.

The report is the Stage 4 acceptance artifact: every URL has a record or a
verdict — coverage, verdicts, fallback, extractor sources, freshness, and
unresolved/dead-zone URLs, with paid cost reported separately.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from web_scraper.contracts import Result, Verdict
from web_scraper.observability.accounting import UrlAccounting

logger = logging.getLogger(__name__)


@dataclass
class RunMetrics:
    """Mutable accumulator updated as each URL is processed."""

    verdicts: Counter[str] = field(default_factory=Counter)
    by_level: Counter[str] = field(default_factory=Counter)  # winning level for OK results
    fallbacks: int = 0  # OK results that did NOT resolve on the first route
    extractor_sources: Counter[str] = field(default_factory=Counter)
    quorum_conflicts: int = 0
    fresh_unchanged: int = 0  # skipped or 304 — the download was avoided
    #: Downloaded but byte-identical to the last run: a fetch that saved nothing.
    fetched_unchanged: int = 0
    cost_credits: Decimal = Decimal("0")
    paid_calls: int = 0
    #: Paid attempts whose reported cost could not be parsed. Money that cannot be
    #: attributed is under-counted spend, so it is surfaced rather than dropped.
    unattributed_costs: int = 0
    per_domain: Counter[str] = field(default_factory=Counter)
    #: Per-route health, straight from the route memory (see web_scraper.routing).
    route_stats: list[dict[str, Any]] = field(default_factory=list)

    def observe(
        self,
        result: Result,
        *,
        extractor_sources: dict[str, str] | None = None,
        conflicts: int = 0,
        domain: str | None = None,
    ) -> None:
        self.verdicts[result.verdict.value] += 1
        if domain:
            self.per_domain[domain] += 1
        if result.verdict is Verdict.NOT_MODIFIED:
            self.fresh_unchanged += 1
        if result.verdict is Verdict.OK and result.attempts:
            self.by_level[result.attempts[-1].level.value] += 1
            if len(result.attempts) > 1:
                self.fallbacks += 1
        for attempt in result.attempts:
            if attempt.provider:
                self.paid_calls += 1
                try:
                    self.cost_credits += Decimal(str(attempt.cost_credits))
                except (InvalidOperation, ValueError, TypeError):
                    # Never silently drop spend: an unparseable cost is reported
                    # so the total is known to be incomplete.
                    self.unattributed_costs += 1
                    logger.warning(
                        "unparseable cost %r on a paid attempt for %s",
                        attempt.cost_credits,
                        attempt.url,
                    )
        for source in (extractor_sources or {}).values():
            self.extractor_sources[source] += 1
        self.quorum_conflicts += conflicts

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdicts": dict(self.verdicts),
            "by_level": dict(self.by_level),
            "fallbacks": self.fallbacks,
            "extractor_sources": dict(self.extractor_sources),
            "quorum_conflicts": self.quorum_conflicts,
            "fresh_unchanged": self.fresh_unchanged,
            "fetched_unchanged": self.fetched_unchanged,
            "cost_credits": str(self.cost_credits),
            "paid_calls": self.paid_calls,
            "unattributed_costs": self.unattributed_costs,
            "per_domain": dict(self.per_domain),
            "route_stats": self.route_stats,
        }


@dataclass(frozen=True)
class RunReport:
    total: int
    resolved: int
    metrics: dict[str, Any]
    unresolved_urls: list[str]
    quarantined_urls: list[str]
    dead_zone_urls: list[str]
    promote: dict[str, Any] | None = None
    #: The run-level ledger. A report without complete accounting is defective.
    accounting: UrlAccounting | None = None

    @property
    def coverage(self) -> float:
        return (self.resolved / self.total) if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "resolved": self.resolved,
            "coverage": round(self.coverage, 4),
            "metrics": self.metrics,
            "unresolved_urls": self.unresolved_urls,
            "quarantined_urls": self.quarantined_urls,
            "dead_zone_urls": self.dead_zone_urls,
            "promote": self.promote,
            "accounting": self.accounting.to_dict() if self.accounting else None,
        }


def build_report(
    results: Iterable[Result],
    *,
    metrics: RunMetrics,
    quarantined_urls: list[str],
    dead_zone_urls: list[str],
    promote: dict[str, Any] | None = None,
    accounting: UrlAccounting | None = None,
) -> RunReport:
    results = list(results)
    resolved = sum(1 for r in results if r.verdict in (Verdict.OK, Verdict.NOT_MODIFIED))
    unresolved = [
        r.url
        for r in results
        if r.verdict not in (Verdict.OK, Verdict.NOT_MODIFIED, Verdict.DEAD_URL)
    ]
    return RunReport(
        total=len(results),
        resolved=resolved,
        metrics=metrics.to_dict(),
        unresolved_urls=unresolved,
        quarantined_urls=quarantined_urls,
        dead_zone_urls=dead_zone_urls,
        promote=promote,
        accounting=accounting,
    )
