"""Choosing across providers: cheapest *per usable result*, subject to confidence.

The single-provider router in :mod:`web_scraper.providers.router` answers "which
mode of this vendor?". This one answers the question that actually costs money:
**which vendor and which mode**, given everything we have observed.

The optimisation:

.. code-block:: text

    minimise expected cost per valid result
    subject to  confidence bound >= minimum
                capability matches the observed failure

Ranking on list price is the mistake this module exists to avoid. A one-credit
strategy that validates half the time costs two credits per usable page; a
1.5-credit strategy that validates almost always costs about 1.52. The first is
33% cheaper on the invoice and 30% more expensive in reality.

Three refusals are structural:

* the router never decides *whether* to pay — that is a triage verdict gated by
  the budget. It only answers "given that paying is permitted, through whom?";
* an expensive strategy is never chosen for being expensive. Price is not
  evidence of reliability, and treating it as such is how a fallback provider
  becomes the default;
* cold start is not optimism. A strategy with no history is not assumed to work;
  it is *explored*, cheapest-first, under a hard cap on how much exploration may
  cost before the router falls back to what it already trusts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from web_scraper.contracts import Verdict
from web_scraper.providers.base import Provider, ProviderStrategy
from web_scraper.providers.breaker import ProviderBreakers
from web_scraper.providers.router import (
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_MINIMUM_CONFIDENCE_BOUND,
    DEFAULT_SHADOW_PROBE_RATE,
    _inappropriate_reason,
    _strategy_is_appropriate,
)
from web_scraper.providers.stats import (
    ProviderStatsStore,
    ProviderStrategyKey,
    ProviderStrategyStats,
)

#: How many calls a never-tried strategy may consume before the router stops
#: exploring it and sticks with what it trusts. Without a cap, a strategy that
#: always fails is retried forever because it never accumulates enough evidence
#: to be rejected — every attempt looks like "still learning".
DEFAULT_MAX_EXPLORATION_CALLS = 10

#: And the same limit expressed in money, because ten calls on an expensive
#: strategy is a very different bill from ten calls on a cheap one.
DEFAULT_MAX_EXPLORATION_CREDITS = Decimal("50")

#: Floor for the success-rate divisor, so a strategy that has never succeeded
#: gets a large-but-finite expected cost rather than infinity. Finite numbers
#: sort; infinities all compare equal and destroy the ordering.
_MIN_RATE = 0.02


@dataclass(frozen=True)
class Candidate:
    """One provider strategy, judged on the evidence."""

    provider: str
    strategy: ProviderStrategy
    stats: ProviderStrategyStats | None
    eligible: bool
    reason: str
    expected_cost: Decimal
    confidence: float
    exploring: bool = False

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.strategy.id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "provider": self.provider,
            "strategy": self.strategy.id,
            "nominal_cost": str(self.strategy.nominal_cost),
            "reservation_cost": str(self.strategy.worst_case_cost),
            "expected_cost_per_valid_result": str(self.expected_cost),
            "confidence_bound": round(self.confidence, 4),
            "eligible": self.eligible,
            "exploring": self.exploring,
            "reason": self.reason,
            "observed": self.stats.to_dict() if self.stats else None,
        }


@dataclass(frozen=True)
class MultiProviderDecision:
    """What was chosen across all providers, and why — auditable after the fact."""

    provider: str | None
    strategy_id: str | None
    estimated_cost: Decimal
    reservation_cost: Decimal
    minimum_confidence_bound: float
    escalation_verdict: str | None
    candidates: tuple[Candidate, ...] = ()
    shadow_probe: bool = False

    @property
    def chosen(self) -> bool:
        return self.provider is not None and self.strategy_id is not None

    @property
    def ref(self) -> str | None:
        return None if not self.chosen else f"{self.provider}:{self.strategy_id}"

    def explain(self) -> str:
        """The answer to 'why did this URL go through this vendor at this price?'."""

        lines = [
            f"escalation verdict: {self.escalation_verdict or '-'}",
            f"minimum confidence bound: {self.minimum_confidence_bound:.3f}",
            "candidates, ranked by expected cost per valid result:",
        ]
        for candidate in self.candidates:
            mark = "->" if candidate.ref == self.ref else "  "
            lines.append(
                f"{mark} {candidate.ref:<28} list {candidate.strategy.nominal_cost:>4}  "
                f"expected {candidate.expected_cost:>8}  "
                f"confidence {candidate.confidence:.3f}  {candidate.reason}"
            )
        if self.shadow_probe:
            lines.append("shadow probe: deliberately re-testing a cheaper option")
        if not self.chosen:
            lines.append("selected: none — nothing clears the bar; this URL stays unresolved")
        else:
            lines.append(f"selected: {self.ref}, holding {self.reservation_cost}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "strategy": self.strategy_id,
            "ref": self.ref,
            "estimated_cost": str(self.estimated_cost),
            "reservation_cost": str(self.reservation_cost),
            "minimum_confidence_bound": self.minimum_confidence_bound,
            "escalation_verdict": self.escalation_verdict,
            "shadow_probe": self.shadow_probe,
            "candidates": [c.to_dict() for c in self.candidates],
            "explanation": self.explain(),
        }


@dataclass
class MultiProviderRouter:
    """Ranks every strategy of every configured provider on one scale."""

    providers: Sequence[Provider] = ()
    stats: ProviderStatsStore | None = None
    breakers: ProviderBreakers | None = None
    minimum_confidence_bound: float = DEFAULT_MINIMUM_CONFIDENCE_BOUND
    min_observations: int = DEFAULT_MIN_OBSERVATIONS
    shadow_probe_rate: float = DEFAULT_SHADOW_PROBE_RATE
    max_exploration_calls: int = DEFAULT_MAX_EXPLORATION_CALLS
    max_exploration_credits: Decimal = DEFAULT_MAX_EXPLORATION_CREDITS
    _rng: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not 0.0 < self.minimum_confidence_bound <= 1.0:
            raise ValueError("minimum_confidence_bound must be in (0, 1]")
        if self.min_observations < 1:
            raise ValueError("min_observations must be >= 1")
        if self._rng is None:
            import random

            self._rng = random.random

    # -- assessment --------------------------------------------------------

    def assess(
        self,
        *,
        domain: str,
        url_class: str,
        verdict: Verdict | None = None,
    ) -> list[Candidate]:
        """Judge every strategy of every provider, cheapest expected cost first."""

        candidates: list[Candidate] = []
        for provider in self.providers:
            for strategy in provider.strategies():
                candidates.append(self._judge(provider.name, strategy, domain, url_class, verdict))
        # Ineligible options are kept and reported: an operator asking why a URL
        # was not resolved needs to see what was rejected and on what grounds.
        candidates.sort(key=lambda c: (not c.eligible, c.expected_cost, c.strategy.nominal_cost))
        return candidates

    def _judge(
        self,
        provider: str,
        strategy: ProviderStrategy,
        domain: str,
        url_class: str,
        verdict: Verdict | None,
    ) -> Candidate:
        # 1. Capability. Paying for a power that cannot address the observed
        #    failure is spending with no mechanism of working.
        if not _strategy_is_appropriate(strategy, verdict):
            return Candidate(
                provider=provider,
                strategy=strategy,
                stats=None,
                eligible=False,
                reason=_inappropriate_reason(strategy, verdict),
                expected_cost=strategy.nominal_cost,
                confidence=0.0,
            )

        # 2. Health. A tripped breaker is not a candidate at any price.
        if self.breakers is not None and self.breakers.is_open(provider, strategy.id):
            return Candidate(
                provider=provider,
                strategy=strategy,
                stats=None,
                eligible=False,
                reason="circuit breaker open",
                expected_cost=strategy.nominal_cost,
                confidence=0.0,
            )

        stats = (
            self.stats.get(
                ProviderStrategyKey(
                    provider=provider,
                    strategy_id=strategy.id,
                    domain=domain,
                    url_class=url_class,
                )
            )
            if self.stats is not None
            else None
        )
        observations = stats.scored_attempts if stats else 0

        # 3. Cold start. Not "assumed reliable" — explored, under a cap.
        if observations < self.min_observations:
            spent = stats.known_cost if stats else Decimal("0")
            attempts = stats.attempts if stats else 0
            if attempts >= self.max_exploration_calls or spent >= self.max_exploration_credits:
                return Candidate(
                    provider=provider,
                    strategy=strategy,
                    stats=stats,
                    eligible=False,
                    reason=(
                        f"exploration budget spent ({attempts} calls, {spent} credits) "
                        f"without reaching {self.min_observations} scored observations"
                    ),
                    expected_cost=strategy.nominal_cost,
                    confidence=0.0,
                )
            return Candidate(
                provider=provider,
                strategy=strategy,
                stats=stats,
                eligible=True,
                reason=f"exploring ({observations}/{self.min_observations} observations)",
                # Ranked at list price during exploration: we have no rate to
                # divide by, and pretending otherwise would invent evidence.
                expected_cost=strategy.nominal_cost,
                confidence=0.0,
                exploring=True,
            )

        # 4. Evidence. The bound gates; the rate prices.
        confidence = stats.confidence_bound if stats else 0.0
        expected = _expected_cost(strategy, stats)
        meets = confidence >= self.minimum_confidence_bound
        detail = f"{stats.validated_successes}/{stats.scored_attempts} validated" if stats else ""
        if stats is not None and not stats.cost_is_complete:
            detail += f", {stats.unknown_cost_calls} calls with unattributed cost"
        return Candidate(
            provider=provider,
            strategy=strategy,
            stats=stats,
            eligible=meets,
            reason=(
                detail
                if meets
                else f"{detail}; confidence {confidence:.3f} below "
                f"{self.minimum_confidence_bound:.3f}"
            ),
            expected_cost=expected,
            confidence=confidence,
        )

    # -- choice ------------------------------------------------------------

    def choose(
        self,
        *,
        domain: str,
        url_class: str,
        verdict: Verdict | None = None,
    ) -> MultiProviderDecision:
        candidates = self.assess(domain=domain, url_class=url_class, verdict=verdict)
        eligible = [c for c in candidates if c.eligible]
        chosen = eligible[0] if eligible else None
        shadow = False

        # Occasionally re-test a cheaper option the evidence rejected, so a
        # domain can come back down in price after a site relaxes. Without this
        # the first expensive choice is paid forever.
        if chosen is not None and self._rng() < self.shadow_probe_rate:
            cheaper = [
                c
                for c in candidates
                if c.strategy.nominal_cost < chosen.strategy.nominal_cost
                and not c.eligible
                and c.stats is not None
                and c.stats.scored_attempts >= self.min_observations
            ]
            if cheaper:
                cheaper.sort(key=lambda c: c.strategy.nominal_cost)
                chosen, shadow = cheaper[0], True

        return MultiProviderDecision(
            provider=chosen.provider if chosen else None,
            strategy_id=chosen.strategy.id if chosen else None,
            estimated_cost=chosen.expected_cost if chosen else Decimal("0"),
            reservation_cost=chosen.strategy.worst_case_cost if chosen else Decimal("0"),
            minimum_confidence_bound=self.minimum_confidence_bound,
            escalation_verdict=verdict.value if verdict else None,
            candidates=tuple(candidates),
            shadow_probe=shadow,
        )

    def provider_for(self, name: str) -> Provider | None:
        return next((p for p in self.providers if p.name == name), None)


def _expected_cost(strategy: ProviderStrategy, stats: ProviderStrategyStats | None) -> Decimal:
    """List price divided by how often it actually works.

    This is the whole point of the module. The divisor is the point estimate,
    not the Wilson bound: the bound is the safety gate that decides whether a
    strategy may be used at all, while the expected cost should reflect what we
    actually expect to spend.
    """

    if stats is None or stats.success_rate is None:
        return strategy.nominal_cost
    rate = max(stats.success_rate, _MIN_RATE)
    return (strategy.nominal_cost / Decimal(str(rate))).quantize(Decimal("0.01"))
