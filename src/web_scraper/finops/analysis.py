"""Was the money well spent, and did anything go wrong?

Three questions, each with a way of being answered dishonestly that this module
refuses:

**What did we get per credit?** Cost per *request* flatters an unreliable
provider. Cost per *valid result* is the honest figure, and it is ``None`` — not
zero, not infinity — when there were no valid results or when part of the spend
was never attributed.

**Was the adaptive routing worth it?** Only measurable against a stated
alternative. :func:`counterfactual_savings` compares what was actually spent
against what one fixed policy would have cost on the *same* URLs. It reports the
comparison, never a general claim about how much the system saves.

**Did anything go wrong?** :func:`detect_anomalies` reports what an operator
should look at, with the numbers that triggered it, so the alert can be judged
rather than merely obeyed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from web_scraper.providers.stats import ProviderStrategyStats

#: A paid share above this is worth a look: the free layer may have regressed.
DEFAULT_PAID_SHARE_ALERT = 0.30

#: Cost per valid result rising by more than this against the baseline.
DEFAULT_COST_SPIKE_RATIO = 1.5

#: Share of paid calls going to the expensive fallback before it is worth asking
#: why the cheaper doors stopped working.
DEFAULT_FALLBACK_SHARE_ALERT = 0.25


@dataclass(frozen=True)
class SpendReport:
    """What a run spent, and on what."""

    total_urls: int
    paid_calls: int
    validated_paid_results: int
    known_spend: Decimal = Decimal("0")
    unknown_cost_calls: int = 0
    by_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def spend_is_complete(self) -> bool:
        return self.unknown_cost_calls == 0

    @property
    def paid_share(self) -> float:
        return 0.0 if self.total_urls == 0 else self.paid_calls / self.total_urls

    @property
    def cost_per_valid_result(self) -> Decimal | None:
        """``None`` when unknowable — see the module docstring."""

        if self.validated_paid_results == 0 or not self.spend_is_complete:
            return None
        return (self.known_spend / Decimal(self.validated_paid_results)).quantize(Decimal("0.001"))

    def to_dict(self) -> dict[str, Any]:
        cpvr = self.cost_per_valid_result
        return {
            "total_urls": self.total_urls,
            "paid_calls": self.paid_calls,
            "paid_share": round(self.paid_share, 4),
            "validated_paid_results": self.validated_paid_results,
            "known_spend": str(self.known_spend),
            "spend_is_complete": self.spend_is_complete,
            "unknown_cost_calls": self.unknown_cost_calls,
            "cost_per_valid_result": None if cpvr is None else str(cpvr),
            "by_strategy": self.by_strategy,
        }


def summarise_spend(stats: Sequence[ProviderStrategyStats], *, total_urls: int) -> SpendReport:
    """Fold per-strategy memory into one run-level view."""

    known = sum((s.known_cost for s in stats), Decimal("0"))
    return SpendReport(
        total_urls=total_urls,
        paid_calls=sum(s.attempts for s in stats),
        validated_paid_results=sum(s.validated_successes for s in stats),
        known_spend=known,
        unknown_cost_calls=sum(s.unknown_cost_calls for s in stats),
        by_strategy={s.key.strategy_ref: s.to_dict() for s in stats},
    )


@dataclass(frozen=True)
class CounterfactualSavings:
    """Adaptive spend against one named fixed policy, on the same URLs."""

    policy: str
    actual_spend: Decimal
    policy_spend: Decimal
    urls_compared: int
    complete: bool = True

    @property
    def saved(self) -> Decimal:
        return self.policy_spend - self.actual_spend

    @property
    def saved_share(self) -> float | None:
        if self.policy_spend == 0:
            return None
        return float(self.saved / self.policy_spend)

    def explain(self) -> str:
        if not self.complete:
            return (
                f"vs {self.policy}: not comparable — part of the actual spend was "
                "never attributed, so any saving figure would be invented"
            )
        share = self.saved_share
        pct = "n/a" if share is None else f"{share:.0%}"
        return (
            f"vs {self.policy}: spent {self.actual_spend} instead of {self.policy_spend} "
            f"on {self.urls_compared} URLs — {self.saved} saved ({pct})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "actual_spend": str(self.actual_spend),
            "policy_spend": str(self.policy_spend),
            "saved": str(self.saved) if self.complete else None,
            "saved_share": self.saved_share if self.complete else None,
            "urls_compared": self.urls_compared,
            "complete": self.complete,
            "explanation": self.explain(),
        }


def counterfactual_savings(
    report: SpendReport,
    *,
    policy_name: str,
    policy_cost_per_call: Decimal,
) -> CounterfactualSavings:
    """What a fixed 'always use vendor X' policy would have cost instead.

    Deliberately narrow. It compares against *one* stated policy on the URLs that
    were actually attempted, and refuses to produce a number at all when part of
    the real spend is unattributed — a saving computed from an incomplete
    numerator would overstate itself in exactly the flattering direction.
    """

    return CounterfactualSavings(
        policy=policy_name,
        actual_spend=report.known_spend,
        policy_spend=policy_cost_per_call * Decimal(report.paid_calls),
        urls_compared=report.paid_calls,
        complete=report.spend_is_complete,
    )


@dataclass(frozen=True)
class CostAnomaly:
    """Something worth an operator's attention, with the evidence."""

    kind: str
    severity: str
    detail: str
    observed: str
    threshold: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "detail": self.detail,
            "observed": self.observed,
            "threshold": self.threshold,
        }


def detect_anomalies(
    report: SpendReport,
    *,
    baseline_cost_per_valid_result: Decimal | None = None,
    fallback_providers: Sequence[str] = ("brightdata",),
    paid_share_alert: float = DEFAULT_PAID_SHARE_ALERT,
    cost_spike_ratio: float = DEFAULT_COST_SPIKE_RATIO,
    fallback_share_alert: float = DEFAULT_FALLBACK_SHARE_ALERT,
) -> list[CostAnomaly]:
    """What changed for the worse. Empty list means nothing stood out."""

    found: list[CostAnomaly] = []

    if not report.spend_is_complete:
        found.append(
            CostAnomaly(
                kind="unknown_spend",
                severity="critical",
                detail=(
                    "money left the account without a reported cost; totals below "
                    "are a floor, and further paid work is blocked until reconciled"
                ),
                observed=f"{report.unknown_cost_calls} calls",
            )
        )

    if report.paid_share > paid_share_alert:
        found.append(
            CostAnomaly(
                kind="paid_share_spike",
                severity="warning",
                detail=(
                    "an unusual share of URLs needed paying for; the usual cause is "
                    "the free layer regressing, not the sites getting harder"
                ),
                observed=f"{report.paid_share:.0%}",
                threshold=f"{paid_share_alert:.0%}",
            )
        )

    current = report.cost_per_valid_result
    if (
        current is not None
        and baseline_cost_per_valid_result is not None
        and baseline_cost_per_valid_result > 0
        and current > baseline_cost_per_valid_result * Decimal(str(cost_spike_ratio))
    ):
        found.append(
            CostAnomaly(
                kind="cost_per_valid_result_spike",
                severity="warning",
                detail="each usable result is costing materially more than the baseline",
                observed=str(current),
                threshold=str(baseline_cost_per_valid_result),
            )
        )

    fallback_calls = sum(
        int(data.get("attempts", 0))
        for ref, data in report.by_strategy.items()
        if any(ref.startswith(f"{name}:") for name in fallback_providers)
    )
    if report.paid_calls and fallback_calls / report.paid_calls > fallback_share_alert:
        found.append(
            CostAnomaly(
                kind="fallback_provider_spike",
                severity="warning",
                detail=(
                    "the expensive fallback is carrying an unusual share of traffic; "
                    "check whether the cheaper strategies have quietly stopped working"
                ),
                observed=f"{fallback_calls}/{report.paid_calls}",
                threshold=f"{fallback_share_alert:.0%}",
            )
        )

    return found
