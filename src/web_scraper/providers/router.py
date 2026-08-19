"""Choosing a paid strategy: the cheapest one that clears the reliability bar.

The optimisation is stated plainly:

    minimise expected cost, subject to reliability >= target

Reliability is the Wilson lower bound of *validated* successes for that exact
provider strategy — never a merged provider reputation, because `normal` failing
on a domain says nothing about whether `super` would work there.

Three refusals are structural, not configurable:

* the router never decides *whether* to pay. That is a triage verdict
  (``BLOCKED``/``SOFT_BLOCK``) gated by the budget; the router only answers
  "given that paying is permitted, which strategy?";
* a strategy the evidence has not cleared is not chosen because it is
  expensive — expensive is not a synonym for reliable;
* every choice carries its reasoning, so an operator can ask "why did this URL
  cost 10 credits?" and get an answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from web_scraper.contracts import Verdict
from web_scraper.providers.base import ProviderStrategy
from web_scraper.routing.stats import RouteKey, RouteStatsStore, wilson_lower_bound

#: Default bar a strategy must clear before it is trusted.
#:
#: This is compared against a Wilson **lower** bound, which is a much stronger
#: claim than a success rate, and the arithmetic is worth stating because it is
#: easy to set this number unreachably high. With a perfect record:
#:
#:      10/10 -> 0.722      40/40 -> 0.912      100/100 -> 0.963
#:      20/20 -> 0.839      70/70 -> 0.948      200/200 -> 0.981
#:
#: So a target of 0.95 demands roughly seventy consecutive validated successes
#: before any strategy qualifies — which in practice means the router refuses
#: everything and a frustrated operator lowers the bar to something arbitrary.
#: 0.80 says "we are confident this works" and is reachable from ~15 clean
#: observations. Raise it per url_class for data that genuinely warrants it.
DEFAULT_MINIMUM_CONFIDENCE_BOUND = 0.80

#: Deprecated alias kept so existing configs keep working.
DEFAULT_RELIABILITY_TARGET = DEFAULT_MINIMUM_CONFIDENCE_BOUND

#: Attempts needed before a strategy's own record outranks its declared order.
DEFAULT_MIN_OBSERVATIONS = 5

#: How often to re-test a cheaper strategy that history says is failing, so a
#: domain can come back down after a site relaxes. Without this the system pays
#: the expensive strategy forever.
DEFAULT_SHADOW_PROBE_RATE = 0.05


@dataclass(frozen=True)
class StrategyAssessment:
    """One strategy, judged."""

    strategy: ProviderStrategy
    observations: int
    validated_successes: int
    reliability: float
    meets_target: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.id,
            "nominal_cost": str(self.strategy.nominal_cost),
            "observations": self.observations,
            "validated_successes": self.validated_successes,
            "reliability": round(self.reliability, 4),
            "meets_target": self.meets_target,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PaidDecision:
    """What was chosen, and the reasoning an operator can audit."""

    provider: str
    strategy_id: str | None
    estimated_cost: Decimal
    target: float
    considered: tuple[StrategyAssessment, ...] = ()
    shadow_probe: bool = False
    escalation_verdict: str | None = None

    @property
    def chosen(self) -> bool:
        return self.strategy_id is not None

    def explain(self) -> str:
        """Human-readable answer to 'why did this URL cost what it cost?'."""

        lines = [
            f"provider: {self.provider}",
            f"escalation verdict: {self.escalation_verdict or '-'}",
            f"reliability target: {self.target:.3f}",
        ]
        for item in self.considered:
            mark = "->" if item.strategy.id == self.strategy_id else "  "
            lines.append(
                f"{mark} {item.strategy.id:<13} cost {item.strategy.nominal_cost:>3}  "
                f"reliability {item.reliability:.3f}  {item.reason}"
            )
        if self.shadow_probe:
            lines.append("shadow probe: re-testing a cheaper strategy on purpose")
        if not self.chosen:
            lines.append("selected: none — no strategy clears the target")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "strategy": self.strategy_id,
            "estimated_cost": str(self.estimated_cost),
            "target": self.target,
            "shadow_probe": self.shadow_probe,
            "escalation_verdict": self.escalation_verdict,
            "considered": [item.to_dict() for item in self.considered],
            "explanation": self.explain(),
        }


@dataclass
class PaidProviderRouter:
    """Picks the cheapest strategy that clears the bar, and says why."""

    stats: RouteStatsStore | None = None
    #: The bar a strategy must clear. Named for what it IS — a lower confidence
    #: bound — because "reliability target" reads like a success rate and invites
    #: operators to set 0.95 expecting "95% of calls work".
    minimum_confidence_bound: float = DEFAULT_MINIMUM_CONFIDENCE_BOUND
    #: Deprecated: use minimum_confidence_bound. Kept so configs do not break.
    target: float | None = None  # deprecated alias, see __post_init__
    min_observations: int = DEFAULT_MIN_OBSERVATIONS
    shadow_probe_rate: float = DEFAULT_SHADOW_PROBE_RATE
    _rng: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.target is not None:
            import warnings

            warnings.warn(
                "PaidProviderRouter(target=...) is deprecated; use "
                "minimum_confidence_bound=... — the value is a Wilson lower bound, "
                "not a success rate (see docs/operations/cost-control.md)",
                DeprecationWarning,
                stacklevel=2,
            )
            self.minimum_confidence_bound = self.target
        if not 0.0 < self.minimum_confidence_bound <= 1.0:
            raise ValueError("minimum_confidence_bound must be in (0, 1]")
        if self.min_observations < 1:
            raise ValueError("min_observations must be >= 1")
        if self._rng is None:
            import random

            self._rng = random.random

    def _key(
        self, strategy: ProviderStrategy, *, provider: str, domain: str, url_class: str
    ) -> RouteKey:
        # Statistics are per STRATEGY: `normal` failing here says nothing about
        # whether `super` would work, and merging them would hide that.
        return RouteKey(
            domain=domain,
            url_class=url_class,
            route_id=f"{provider}:{strategy.id}",
            level="L3" if not strategy.premium_network else "L4",
        )

    def assess(
        self,
        strategies: Sequence[ProviderStrategy],
        *,
        provider: str,
        domain: str,
        url_class: str,
        verdict: Verdict | None = None,
    ) -> list[StrategyAssessment]:
        """Judge every strategy against the target, cheapest first."""

        out: list[StrategyAssessment] = []
        for strategy in sorted(strategies, key=lambda s: s.nominal_cost):
            if not _strategy_is_appropriate(strategy, verdict):
                out.append(
                    StrategyAssessment(
                        strategy=strategy,
                        observations=0,
                        validated_successes=0,
                        reliability=0.0,
                        meets_target=False,
                        reason=_inappropriate_reason(strategy, verdict),
                    )
                )
                continue

            record = (
                self.stats.get(
                    self._key(strategy, provider=provider, domain=domain, url_class=url_class)
                )
                if self.stats is not None
                else None
            )
            observations = record.scored_attempts if record else 0
            successes = record.validated_successes if record else 0

            if observations < self.min_observations:
                # No evidence yet. The cheapest untried strategy is the right
                # first bet: it is what makes cold start converge downward.
                out.append(
                    StrategyAssessment(
                        strategy=strategy,
                        observations=observations,
                        validated_successes=successes,
                        reliability=0.0,
                        meets_target=True,
                        reason=f"untried ({observations}/{self.min_observations} observations)",
                    )
                )
                continue

            reliability = wilson_lower_bound(successes, observations)
            meets = reliability >= self.minimum_confidence_bound
            out.append(
                StrategyAssessment(
                    strategy=strategy,
                    observations=observations,
                    validated_successes=successes,
                    reliability=reliability,
                    meets_target=meets,
                    reason=(
                        f"{successes}/{observations} validated"
                        + ("" if meets else f", below target {self.minimum_confidence_bound:.3f}")
                    ),
                )
            )
        return out

    def choose(
        self,
        strategies: Sequence[ProviderStrategy],
        *,
        provider: str,
        domain: str,
        url_class: str,
        verdict: Verdict | None = None,
        target: float | None = None,
    ) -> PaidDecision:
        """The cheapest strategy clearing the target, with its reasoning."""

        effective_target = target if target is not None else self.minimum_confidence_bound
        considered = self.assess(
            strategies, provider=provider, domain=domain, url_class=url_class, verdict=verdict
        )
        eligible = [item for item in considered if item.meets_target]

        chosen = eligible[0] if eligible else None  # already cheapest-first
        shadow = False

        # Occasionally re-test a cheaper strategy the evidence rejected, so a
        # domain can return to a lower price after the site relaxes.
        if chosen is not None and self._rng() < self.shadow_probe_rate:
            cheaper = [
                item
                for item in considered
                if item.strategy.nominal_cost < chosen.strategy.nominal_cost
                and item.observations >= self.min_observations
            ]
            if cheaper:
                chosen, shadow = cheaper[0], True

        return PaidDecision(
            provider=provider,
            strategy_id=chosen.strategy.id if chosen else None,
            estimated_cost=chosen.strategy.nominal_cost if chosen else Decimal("0"),
            target=effective_target,
            considered=tuple(considered),
            shadow_probe=shadow,
            escalation_verdict=verdict.value if verdict else None,
        )


#: A premium network changes the address we arrive from. That only helps when we
#: were refused entry.
_PREMIUM_NETWORK_ANSWERS = frozenset({Verdict.BLOCKED, Verdict.SOFT_BLOCK})

#: Rendering runs the page's JavaScript. That answers a page that never rendered,
#: and a challenge that is itself JavaScript (a 200 carrying an interstitial).
#: It does NOT answer a hard 403: a WAF refusing the connection refuses the
#: rendered one too, and paying five credits instead of one to be refused again
#: is exactly the waste this rule exists to stop.
_RENDERING_ANSWERS = frozenset({Verdict.CSR_REQUIRED, Verdict.SOFT_BLOCK})


def _strategy_is_appropriate(strategy: ProviderStrategy, verdict: Verdict | None) -> bool:
    """Is this strategy even the right tool for the failure we saw?

    Paying for a capability that cannot address the observed failure is how a
    budget disappears without anything improving.
    """

    if verdict is None:
        return True
    if strategy.premium_network and verdict not in _PREMIUM_NETWORK_ANSWERS:
        return False
    return not (strategy.renders_javascript and verdict not in _RENDERING_ANSWERS)


def _inappropriate_reason(strategy: ProviderStrategy, verdict: Verdict | None) -> str:
    name = verdict.value if verdict else "?"
    if strategy.premium_network and verdict not in _PREMIUM_NETWORK_ANSWERS:
        return f"a premium network does not address {name}"
    return f"rendering does not address {name}"
