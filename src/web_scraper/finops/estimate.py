"""What a paid run would cost, computed without spending anything.

An operator asked to approve a paid batch needs three numbers that are routinely
confused:

* **expected** — what it will probably cost, using each strategy's observed
  success rate. This is the planning figure.
* **reserved** — what will be *held* while the run executes. Always larger, and
  the number that decides whether the run can start at all: a run whose holds
  exceed the daily limit stalls halfway through no matter how cheap its expected
  cost looked.
* **worst case** — every URL costing its strategy's maximum. Not a prediction;
  the answer to "how bad can this get".

Quoting only the expected cost is how a run is approved and then blocked at 60%
completion with its budget consumed by holds.

Nothing here performs a paid call, and nothing here reserves anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from web_scraper.contracts import Verdict
from web_scraper.providers.multi_router import MultiProviderRouter

#: Phases of a large run, cheapest first. Escalating a single URL straight to
#: the most expensive vendor the moment it fails is what makes a big crawl
#: expensive; draining each phase first means most URLs never reach the next one.
PHASE_A_FREE = "A:free"
PHASE_B_FREE_RETRY = "B:free-retry"
PHASE_C_CHEAP_PAID = "C:cheap-paid"
PHASE_D_EXPENSIVE_PAID = "D:expensive-paid"

#: Strategies at or below this planning cost belong to the cheap paid phase.
DEFAULT_CHEAP_PAID_CEILING = Decimal("10")


@dataclass(frozen=True)
class PhasePlan:
    """One phase of a run: what it covers and what it may cost."""

    name: str
    url_count: int
    expected_cost: Decimal = Decimal("0")
    reserved_cost: Decimal = Decimal("0")
    worst_case_cost: Decimal = Decimal("0")
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.name,
            "urls": self.url_count,
            "expected_cost": str(self.expected_cost),
            "reserved_cost": str(self.reserved_cost),
            "worst_case_cost": str(self.worst_case_cost),
            "note": self.note,
        }


@dataclass(frozen=True)
class CostEstimate:
    """The answer to 'what will this run cost?', with its own uncertainty."""

    unresolved_urls: int
    phases: tuple[PhasePlan, ...] = ()
    #: How many URLs the router would send to each provider:strategy.
    strategy_distribution: dict[str, int] = field(default_factory=dict)
    budget_remaining: Decimal | None = None
    #: URLs for which no strategy clears the confidence bound. These will not be
    #: attempted, so they cost nothing and stay unresolved — which is a coverage
    #: fact an operator needs before approving anything.
    unroutable_urls: int = 0

    @property
    def expected_cost(self) -> Decimal:
        return sum((p.expected_cost for p in self.phases), Decimal("0"))

    @property
    def reserved_cost(self) -> Decimal:
        return sum((p.reserved_cost for p in self.phases), Decimal("0"))

    @property
    def worst_case_cost(self) -> Decimal:
        return sum((p.worst_case_cost for p in self.phases), Decimal("0"))

    @property
    def fits_budget(self) -> bool | None:
        """Can the run finish without stalling on holds? ``None`` if unknown.

        Compared against the RESERVED total, not the expected one: holds are what
        the ledger actually refuses on.
        """

        if self.budget_remaining is None:
            return None
        return self.reserved_cost <= self.budget_remaining

    def explain(self) -> str:
        lines = [
            f"unresolved URLs: {self.unresolved_urls}",
            f"expected cost:   {self.expected_cost}",
            f"reserved (held): {self.reserved_cost}",
            f"worst case:      {self.worst_case_cost}",
        ]
        if self.budget_remaining is not None:
            verdict = "fits" if self.fits_budget else "DOES NOT FIT — the run would stall"
            lines.append(f"budget remaining: {self.budget_remaining} ({verdict})")
        if self.unroutable_urls:
            lines.append(
                f"unroutable: {self.unroutable_urls} URLs have no strategy clearing the "
                "confidence bound; they stay unresolved and cost nothing"
            )
        lines.append("")
        lines.append("phases:")
        for phase in self.phases:
            lines.append(
                f"  {phase.name:<18} {phase.url_count:>6} urls  "
                f"expected {phase.expected_cost:>8}  held {phase.reserved_cost:>8}"
                + (f"  ({phase.note})" if phase.note else "")
            )
        if self.strategy_distribution:
            lines.append("")
            lines.append("predicted strategy distribution:")
            for ref, count in sorted(self.strategy_distribution.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {ref:<28} {count:>6}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unresolved_urls": self.unresolved_urls,
            "unroutable_urls": self.unroutable_urls,
            "expected_cost": str(self.expected_cost),
            "reserved_cost": str(self.reserved_cost),
            "worst_case_cost": str(self.worst_case_cost),
            "budget_remaining": (
                None if self.budget_remaining is None else str(self.budget_remaining)
            ),
            "fits_budget": self.fits_budget,
            "phases": [p.to_dict() for p in self.phases],
            "strategy_distribution": dict(self.strategy_distribution),
            "explanation": self.explain(),
        }


@dataclass
class _Bucket:
    """Running totals for one paid phase."""

    urls: int = 0
    expected: Decimal = Decimal("0")
    reserved: Decimal = Decimal("0")

    def add(self, *, expected: Decimal, reserved: Decimal) -> None:
        self.urls += 1
        self.expected += expected
        self.reserved += reserved


@dataclass(frozen=True)
class UnresolvedUrl:
    """A URL the free layer could not resolve, and why."""

    url: str
    domain: str
    url_class: str
    verdict: Verdict


def estimate_run_cost(
    unresolved: Sequence[UnresolvedUrl],
    *,
    router: MultiProviderRouter,
    free_url_count: int = 0,
    free_retry_count: int = 0,
    budget_remaining: Decimal | None = None,
    cheap_paid_ceiling: Decimal = DEFAULT_CHEAP_PAID_CEILING,
) -> CostEstimate:
    """Price a paid run by asking the router what it *would* choose.

    The router is consulted exactly as it would be during the run, so the
    estimate reflects the same confidence bounds, capability rules and breaker
    state that will govern the real thing. It is a dry run of the decision, not
    a separate model that can drift away from it.
    """

    distribution: dict[str, int] = {}
    cheap, dear = _Bucket(), _Bucket()
    unroutable = 0

    for item in unresolved:
        decision = router.choose(domain=item.domain, url_class=item.url_class, verdict=item.verdict)
        if not decision.chosen:
            unroutable += 1
            continue
        ref = decision.ref or "?"
        distribution[ref] = distribution.get(ref, 0) + 1

        bucket = cheap if decision.reservation_cost <= cheap_paid_ceiling else dear
        bucket.add(expected=decision.estimated_cost, reserved=decision.reservation_cost)

    phases = (
        PhasePlan(
            name=PHASE_A_FREE,
            url_count=free_url_count,
            note="L0-L2; no paid call is possible in this phase",
        ),
        PhasePlan(
            name=PHASE_B_FREE_RETRY,
            url_count=free_retry_count,
            note="delayed free retry; transient failures resolve here for nothing",
        ),
        PhasePlan(
            name=PHASE_C_CHEAP_PAID,
            url_count=cheap.urls,
            expected_cost=cheap.expected,
            reserved_cost=cheap.reserved,
            # Worst case IS the sum of holds: a hold is defined as the most the
            # provider can bill for that call.
            worst_case_cost=cheap.reserved,
            note=f"strategies holding <= {cheap_paid_ceiling}",
        ),
        PhasePlan(
            name=PHASE_D_EXPENSIVE_PAID,
            url_count=dear.urls,
            expected_cost=dear.expected,
            reserved_cost=dear.reserved,
            worst_case_cost=dear.reserved,
            note="only URLs no cheaper strategy can serve",
        ),
    )

    return CostEstimate(
        unresolved_urls=len(unresolved),
        phases=phases,
        strategy_distribution=distribution,
        budget_remaining=budget_remaining,
        unroutable_urls=unroutable,
    )
