"""Paid escalation across several vendors, under one budget.

:class:`~web_scraper.providers.escalation.PaidEscalator` runs one vendor. This
one runs a fleet, and the difference that matters is not "more providers" but
**one attempt per URL across all of them**. Trying vendor after vendor until one
works is how a single blocked URL costs sixty credits; the router's whole job is
to make the first choice the right one, and letting the escalator retry through
the fleet would throw that away.

The guarantee order is identical to the single-provider escalator, because every
step of it is load-bearing:

1. **Policy** — only a triage verdict may put money on the table.
2. **Budget state** — an unresolved incident stops all spending.
3. **Choice** — the router ranks every strategy of every vendor by expected cost
   per valid result, subject to a confidence bound and capability match.
4. **Admission** — the chosen strategy's breaker grants exactly one call.
5. **Reservation** — the *worst case* is held, not the typical price.
6. **mark_submitted before waiting** — a crash must look like possible spend.
7. **Settlement** — at the reported cost; ``None`` is unknown, never zero.
8. **Triage** — a provider's 200 proves nothing about the content.

Every outcome is recorded against that exact ``provider:strategy`` on that exact
``domain/url_class``, so the next decision is better informed than this one.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from web_scraper.budget import BudgetExceeded, BudgetLedger
from web_scraper.contracts import (
    PAID_ESCALATION_VERDICTS,
    ContentRules,
    Cost,
    TriageResult,
    Verdict,
)
from web_scraper.providers.base import ProviderError, ProviderRequest, ProviderResponse
from web_scraper.providers.breaker import ProviderBreakers
from web_scraper.providers.multi_router import MultiProviderDecision, MultiProviderRouter
from web_scraper.providers.pricing import PricingBook
from web_scraper.providers.stats import ProviderStatsStore, ProviderStrategyKey
from web_scraper.triage import classify_response

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaidAttempt:
    """What one paid attempt did, whether or not it happened."""

    attempted: bool
    reason: str
    decision: MultiProviderDecision | None = None
    triage: TriageResult | None = None
    response: ProviderResponse | None = None
    reserved: Decimal = Decimal("0")
    cost: Cost = field(default_factory=Cost.free)
    provider: str | None = None
    strategy_id: str | None = None

    @property
    def succeeded(self) -> bool:
        """Validated success — a 200 from a provider is not enough."""

        return self.triage is not None and self.triage.verdict is Verdict.OK

    @property
    def unknown_spend(self) -> bool:
        return self.attempted and not self.cost.is_known

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "reason": self.reason,
            "succeeded": self.succeeded,
            "provider": self.provider,
            "strategy": self.strategy_id,
            "reserved": str(self.reserved),
            "cost": self.cost.to_dict(),
            "unknown_spend": self.unknown_spend,
            "verdict": self.triage.verdict.value if self.triage else None,
            "decision": self.decision.to_dict() if self.decision else None,
            "provider_response": self.response.to_dict() if self.response else None,
        }


def _target_hash(url: str) -> str:
    """Identify the target without storing it: ledgers get shared and read."""

    return "sha256:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


class MultiProviderEscalator:
    """One paid attempt per URL, chosen across every configured vendor."""

    def __init__(
        self,
        router: MultiProviderRouter,
        *,
        budget: BudgetLedger,
        stats: ProviderStatsStore | None = None,
        breakers: ProviderBreakers | None = None,
        pricing: PricingBook | None = None,
    ) -> None:
        self.router = router
        self.budget = budget
        # The tariff book is the ONLY place a provisional bound may come from.
        self.pricing = pricing if pricing is not None else PricingBook()
        self.stats = stats if stats is not None else router.stats
        self.breakers = breakers or router.breakers or ProviderBreakers()
        # The router consults the same breakers when ranking, so a tripped
        # strategy is never even offered.
        if self.router.breakers is None:
            self.router.breakers = self.breakers

    def attempt(
        self,
        url: str,
        *,
        verdict: Verdict,
        domain: str,
        url_class: str,
        rules: ContentRules | None = None,
        wait_selector: str | None = None,
    ) -> PaidAttempt:
        # 1. Policy. Only triage decides that paying is even on the table.
        if verdict not in PAID_ESCALATION_VERDICTS:
            return PaidAttempt(False, f"{verdict.value} never justifies a paid provider")

        # 2. Budget state. An unresolved incident stops spending entirely.
        state = self.budget.state()
        if not state.allows_paid_work:
            return PaidAttempt(False, f"budget state is {state.value}")

        # 3. Choice across the whole fleet, ranked by expected cost per result.
        decision = self.router.choose(domain=domain, url_class=url_class, verdict=verdict)
        if not decision.chosen:
            return PaidAttempt(False, "no strategy clears the confidence bound", decision=decision)

        provider_name = decision.provider or ""
        strategy_id = decision.strategy_id or ""
        provider = self.router.provider_for(provider_name)
        if provider is None:  # pragma: no cover - guarded by the router's own list
            return PaidAttempt(
                False, f"provider {provider_name} is not configured", decision=decision
            )
        strategy = next(s for s in provider.strategies() if s.id == strategy_id)

        # 4. Admission. A half-open breaker grants exactly one trial call.
        admission = self.breakers.admit(provider_name, strategy_id)
        if not admission.allowed:
            return PaidAttempt(False, admission.reason, decision=decision)

        # 5. Hold the WORST case, not the typical cost.
        try:
            reservation = self.budget.reserve(
                provider=provider_name,
                credits=strategy.worst_case_cost,
                strategy_id=strategy_id,
                target_hash=_target_hash(url),
            )
        except BudgetExceeded as exc:
            self.breakers.release_probe(provider_name, strategy_id)
            return PaidAttempt(False, f"budget refused the hold: {exc}", decision=decision)

        key = ProviderStrategyKey(
            provider=provider_name,
            strategy_id=strategy_id,
            domain=domain,
            url_class=url_class,
        )
        request = ProviderRequest(url=url, strategy_id=strategy_id, wait_selector=wait_selector)

        # 6. Durable "it has left" BEFORE waiting on the network.
        reservation = self.budget.mark_submitted(reservation)

        try:
            response = provider.fetch(request)
        except ProviderError as exc:
            # We were never told whether this was billed. Releasing the hold
            # would under-count the budget, so it stays held and unknown.
            self.budget.mark_unknown(reservation, detail=f"{exc.kind.value}: {exc.message}")
            self.breakers.record_error(provider_name, strategy_id, exc.kind)
            if self.stats is not None:
                self.stats.record(key, provider_error=True, cost=Cost.unknown())
            return PaidAttempt(
                True,
                f"provider error: {exc.kind.value}",
                decision=decision,
                reserved=reservation.credits,
                cost=Cost.unknown(),
                provider=provider_name,
                strategy_id=strategy_id,
            )

        # 7. Settle. A provider that reported nothing gets a PROVISIONAL ceiling
        #    only when its tariff is documented and deterministic; otherwise the
        #    answer is UNKNOWN and spending stops. That rule lives in the pricing
        #    book so it is stated once, not re-derived per adapter.
        reported = response.cost.credits if response.cost.attributed else None
        cost = self.pricing.settle(
            provider_name,
            strategy_id,
            reported,
            # A vendor that states its own dollars settles exactly, whether or
            # not an operator ever configured a rate for it.
            reported_usd=response.cost.usd if response.cost.attributed else None,
        )
        # A provisional cost is settled at its CEILING, so the ledger never
        # under-counts: the true spend is at most what we recorded.
        self.budget.settle(reservation, actual_credits=cost.credits)
        drift = (
            self.pricing.detect_drift(provider_name, strategy_id, reported)
            if reported is not None
            else None
        )
        if drift:
            logger.warning("pricing drift: %s", drift)

        # 8. Canonical validation. The provider's 200 proves nothing.
        triage = (
            TriageResult(
                Verdict.PARSE_FAIL,
                "provider response hit the body-size ceiling and is incomplete",
                response.target_status,
                len(response.body),
            )
            if response.truncated
            else classify_response(
                status=response.target_status,
                body=response.body,
                headers=response.headers,
                rules=rules or ContentRules(min_body_bytes=1),
            )
        )
        self.breakers.record_verdict(provider_name, strategy_id, triage.verdict)
        if self.stats is not None:
            self.stats.record(
                key,
                verdict=triage.verdict,
                cost=cost,
                latency_ms=response.latency_ms,
            )

        return PaidAttempt(
            attempted=True,
            reason=f"paid attempt via {provider_name}:{strategy_id}",
            decision=decision,
            triage=triage,
            response=response,
            reserved=reservation.credits,
            cost=cost,
            provider=provider_name,
            strategy_id=strategy_id,
        )
