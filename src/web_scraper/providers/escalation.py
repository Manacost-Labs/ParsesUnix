"""The paid attempt: the one place money actually leaves.

Everything expensive funnels through :meth:`PaidEscalator.attempt`, so the
guarantees live in one readable place rather than scattered across the gateway:

* paying is only ever considered for a verdict triage says may be paid for;
* the worst-case cost is held *before* the request is built;
* the request is marked submitted *before* we wait for an answer, so a crash
  cannot make it look like it never happened;
* the reported cost — not the estimate — is what gets settled, and a missing
  cost becomes unknown spend rather than zero;
* the provider's answer is judged by canonical triage exactly like any other
  response, because a provider returning 200 with a challenge page is still a
  block.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
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
from web_scraper.providers.base import Provider, ProviderError, ProviderRequest, ProviderResponse
from web_scraper.providers.breaker import ProviderBreakers
from web_scraper.providers.router import PaidDecision, PaidProviderRouter
from web_scraper.triage import classify_response

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaidAttempt:
    """What one paid attempt did, whether or not it happened."""

    attempted: bool
    reason: str
    decision: PaidDecision | None = None
    triage: TriageResult | None = None
    response: ProviderResponse | None = None
    reserved: Decimal = Decimal("0")
    actual_cost: Decimal | None = None
    unknown_spend: bool = False

    @property
    def cost(self) -> Cost:
        """The same fact as ``actual_cost``, in the canonical type.

        Exists so this escalator and the multi-provider one present one shape to
        the gateway; a gateway that had to know which escalator it held would
        grow a branch per vendor strategy.
        """

        if not self.attempted:
            return Cost.free()
        return Cost.unknown() if self.actual_cost is None else Cost.of(self.actual_cost)

    @property
    def succeeded(self) -> bool:
        """Validated success — a 200 from the provider is not enough."""

        return self.triage is not None and self.triage.verdict is Verdict.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "reason": self.reason,
            "succeeded": self.succeeded,
            "reserved": str(self.reserved),
            "actual_cost": str(self.actual_cost) if self.actual_cost is not None else None,
            "unknown_spend": self.unknown_spend,
            "verdict": self.triage.verdict.value if self.triage else None,
            "decision": self.decision.to_dict() if self.decision else None,
            "provider_response": self.response.to_dict() if self.response else None,
        }


def _target_hash(url: str) -> str:
    """Identify the target without storing it: profiles and ledgers get shared."""

    return "sha256:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


class PaidEscalator:
    """Runs at most one paid attempt per URL, under every guarantee at once."""

    def __init__(
        self,
        provider: Provider,
        *,
        budget: BudgetLedger,
        router: PaidProviderRouter,
        breakers: ProviderBreakers | None = None,
    ) -> None:
        self.provider = provider
        self.budget = budget
        self.router = router
        self.breakers = breakers or ProviderBreakers()

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

        # 3. Health. A tripped provider or strategy is skipped, not retried.
        if self.breakers.is_open(self.provider.name):
            return PaidAttempt(False, f"circuit breaker open for {self.provider.name}")

        available = [
            strategy
            for strategy in self.provider.strategies()
            if not self.breakers.is_open(self.provider.name, strategy.id)
        ]
        if not available:
            return PaidAttempt(False, "every strategy for this provider is tripped")

        # 4. Choice. Cheapest strategy clearing the confidence bar.
        decision = self.router.choose(
            available,
            provider=self.provider.name,
            domain=domain,
            url_class=url_class,
            verdict=verdict,
        )
        if not decision.chosen:
            return PaidAttempt(False, "no strategy meets the confidence bound", decision=decision)

        strategy = next(s for s in available if s.id == decision.strategy_id)

        # 4b. Claim permission for exactly this one call. A half-open breaker
        #     admits a single trial; without claiming it here a whole batch
        #     would pour through the moment a cooldown expired.
        admission = self.breakers.admit(self.provider.name, strategy.id)
        if not admission.allowed:
            return PaidAttempt(False, admission.reason, decision=decision)

        # 5. Hold the WORST case, not the typical cost.
        try:
            reservation = self.budget.reserve(
                provider=self.provider.name,
                credits=strategy.worst_case_cost,
                strategy_id=strategy.id,
                target_hash=_target_hash(url),
            )
        except BudgetExceeded as exc:
            # We never called the provider, so this was not a trial of it.
            self.breakers.release_probe(self.provider.name, strategy.id)
            return PaidAttempt(False, f"budget refused the hold: {exc}", decision=decision)

        request = ProviderRequest(url=url, strategy_id=strategy.id, wait_selector=wait_selector)

        # 6. Durable "it has left" BEFORE waiting. A crash after this point must
        #    look like possible spend, never like nothing happened.
        reservation = self.budget.mark_submitted(reservation)

        try:
            response = self.provider.fetch(request)
        except ProviderError as exc:
            # The request may or may not have been billed; we were never told.
            # Releasing here would silently under-count the budget.
            self.budget.mark_unknown(reservation, detail=f"{exc.kind.value}: {exc.message}")
            self.breakers.record_error(self.provider.name, strategy.id, exc.kind)
            return PaidAttempt(
                True,
                f"provider error: {exc.kind.value}",
                decision=decision,
                reserved=reservation.credits,
                unknown_spend=True,
            )

        # 7. Settle with what the provider reported. None means unknown, not free.
        reported = response.cost.credits if response.cost.attributed else None
        self.budget.settle(reservation, actual_credits=reported)
        unknown = reported is None

        # 8. Canonical validation. The provider's 200 proves nothing about content.
        triage = classify_response(
            status=response.target_status,
            body=response.body,
            headers=response.headers,
            rules=rules or ContentRules(min_body_bytes=1),
        )
        self.breakers.record_verdict(self.provider.name, strategy.id, triage.verdict)

        return PaidAttempt(
            attempted=True,
            reason=f"paid attempt via {self.provider.name}:{strategy.id}",
            decision=decision,
            triage=triage,
            response=response,
            reserved=reservation.credits,
            actual_cost=reported,
            unknown_spend=unknown,
        )
