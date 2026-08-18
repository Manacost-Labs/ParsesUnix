"""Adaptive route selection.

The default ladder is "cheapest level first, declared order within a level". That
is a good prior and a bad memory: it re-learns nothing from yesterday's run. This
router keeps the prior and adds evidence — it reorders routes the profile already
declares, using what actually worked.

Four rules keep it honest:

1. **Evidence, not noise.** Below ``min_observations`` scored attempts a route
   keeps its declared position. A single lucky success never promotes a route.
2. **Pessimism.** Ranking uses the Wilson lower bound, so a route with 1/1 does
   not outrank one with 200/205.
3. **Hysteresis.** A challenger must beat the incumbent by a margin to displace
   it, which is what stops two routes trading places every run.
4. **Cost is a tiebreaker, never an override.** The router may reorder routes
   *within* the free ladder and may promote a proven cheap route; it never
   selects a paid level. Paid escalation remains a triage-verdict decision
   enforced by the gateway and the budget.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from web_scraper.contracts import Route
from web_scraper.routing.stats import RouteKey, RouteStats, RouteStatsStore

#: Scored attempts required before a route's own history outranks the profile.
DEFAULT_MIN_OBSERVATIONS = 8

#: A challenger must beat the incumbent's score by this much to displace it.
DEFAULT_HYSTERESIS = 0.10

#: How often to spend one attempt re-testing a cheaper route that history says is
#: failing. Without this the system never notices that a site relaxed its
#: defenses and stays on the expensive route forever.
DEFAULT_SHADOW_PROBE_RATE = 0.05

#: Latency beyond which a route starts losing points, and the maximum penalty.
LATENCY_REFERENCE_MS = 2_000.0
MAX_LATENCY_PENALTY = 0.15


@dataclass(frozen=True)
class RankedRoute:
    route: Route
    score: float
    reason: str
    stats: RouteStats | None = None
    shadow_probe: bool = False
    #: Evidence component only (0.0 when the route has no usable history).
    confidence: float = 0.0

    @property
    def has_evidence(self) -> bool:
        return self.stats is not None and self.confidence > 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route.to_dict(),
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "shadow_probe": self.shadow_probe,
            "stats": self.stats.to_dict() if self.stats else None,
        }


def _declared_prior(route: Route, position: int) -> float:
    """The ordering the gateway would use on its own, as a score.

    With no evidence the router must reproduce the gateway's plan exactly —
    declared primary first, then alternatives cheapest-first — otherwise the
    system has two disagreeing route policies.
    """

    if position == 0:
        return 0.9  # the profile author's declared primary
    return 0.7 - route.level.rank * 0.05  # alternatives: cheaper ranks higher


def _cost_preference(route: Route) -> float:
    """How much a level is preferred purely for being cheap."""

    return (4 - route.level.rank) / 5.0  # L0 -> 0.8, L1 -> 0.6, L2 -> 0.4


def _latency_penalty(stats: RouteStats | None) -> float:
    if stats is None or stats.latency_ms <= LATENCY_REFERENCE_MS:
        return 0.0
    overshoot = (stats.latency_ms - LATENCY_REFERENCE_MS) / LATENCY_REFERENCE_MS
    return min(MAX_LATENCY_PENALTY, overshoot * 0.05)


class AdaptiveRouter:
    def __init__(
        self,
        stats: RouteStatsStore | None = None,
        *,
        min_observations: int = DEFAULT_MIN_OBSERVATIONS,
        hysteresis: float = DEFAULT_HYSTERESIS,
        shadow_probe_rate: float = DEFAULT_SHADOW_PROBE_RATE,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.stats = stats
        self.min_observations = min_observations
        self.hysteresis = hysteresis
        self.shadow_probe_rate = shadow_probe_rate
        self._rng = rng

    def rank(
        self,
        routes: Sequence[Route],
        *,
        domain: str,
        url_class: str,
    ) -> list[RankedRoute]:
        """Order the profile's free routes by evidence, then by the declared prior."""

        ranked: list[RankedRoute] = []
        for position, route in enumerate(routes):
            if route.level.is_paid:
                continue  # paid levels are never the router's decision
            stats = self._stats_for(route, domain, url_class)

            if stats is None or stats.scored_attempts < self.min_observations:
                observed = stats.scored_attempts if stats else 0
                ranked.append(
                    RankedRoute(
                        route=route,
                        score=_declared_prior(route, position),
                        reason=(
                            f"declared order kept: {observed} scored attempt(s), "
                            f"need {self.min_observations} before history is trusted"
                        ),
                        stats=stats,
                    )
                )
                continue

            # With evidence, reliability leads and cheapness breaks ties: two
            # equally reliable routes should settle on the cheaper level.
            confidence = stats.confidence_bound
            score = confidence + 0.2 * _cost_preference(route) - _latency_penalty(stats)
            ranked.append(
                RankedRoute(
                    route=route,
                    score=score,
                    confidence=confidence,
                    reason=(
                        f"confidence {confidence:.2f} over {stats.scored_attempts} attempts "
                        f"(recent {stats.ewma_success:.2f})"
                    ),
                    stats=stats,
                )
            )

        ranked.sort(key=lambda item: item.score, reverse=True)
        ranked = self._apply_hysteresis(routes, ranked)
        return self._add_shadow_probe(ranked)

    def _stats_for(self, route: Route, domain: str, url_class: str) -> RouteStats | None:
        if self.stats is None:
            return None
        return self.stats.get(RouteKey.for_route(route, domain=domain, url_class=url_class))

    def _apply_hysteresis(
        self, declared: Sequence[Route], ranked: list[RankedRoute]
    ) -> list[RankedRoute]:
        """Keep the declared primary in front unless a challenger clearly wins.

        Without this, two routes within noise of each other swap every run, which
        churns sessions and makes route statistics meaningless.
        """

        if len(ranked) < 2 or not declared:
            return ranked
        incumbent_route = declared[0]
        leader = ranked[0]
        if leader.route == incumbent_route:
            return ranked

        incumbent = next((item for item in ranked if item.route == incumbent_route), None)
        if incumbent is None:
            return ranked

        # A cheaper challenger that is at least as reliable is a deliberate,
        # stable improvement, not noise — hysteresis exists to damp flapping, and
        # letting cost win here is the whole point of the ladder.
        cheaper = leader.route.level.rank < incumbent.route.level.rank
        at_least_as_reliable = leader.confidence >= incumbent.confidence
        if cheaper and at_least_as_reliable and leader.has_evidence:
            return ranked

        if leader.score - incumbent.score < self.hysteresis:
            held = RankedRoute(
                route=incumbent.route,
                score=incumbent.score,
                reason=(
                    f"{incumbent.reason}; held in front: challenger leads by "
                    f"{leader.score - incumbent.score:.3f} < hysteresis {self.hysteresis:.2f}"
                ),
                stats=incumbent.stats,
            )
            rest = [item for item in ranked if item.route != incumbent_route]
            return [held, *rest]
        return ranked

    def _add_shadow_probe(self, ranked: list[RankedRoute]) -> list[RankedRoute]:
        """Occasionally re-test a cheaper route that evidence says is failing.

        This is how a domain gets downgraded again after a site relaxes its
        defenses: without a probe, a route that once failed is never retried and
        the system pays the higher level forever.
        """

        if len(ranked) < 2 or self._rng() >= self.shadow_probe_rate:
            return ranked
        leader = ranked[0]
        cheaper = [
            item
            for item in ranked[1:]
            if item.route.level.rank < leader.route.level.rank and item.stats is not None
        ]
        if not cheaper:
            return ranked
        probe = min(cheaper, key=lambda item: item.route.level.rank)
        promoted = RankedRoute(
            route=probe.route,
            score=probe.score,
            reason=f"shadow probe: re-testing a cheaper level ({probe.reason})",
            stats=probe.stats,
            shadow_probe=True,
        )
        rest = [item for item in ranked if item.route != probe.route]
        return [promoted, *rest]

    def order(self, routes: Sequence[Route], *, domain: str, url_class: str) -> list[Route]:
        """Convenience: just the routes, highest-ranked first."""

        return [item.route for item in self.rank(routes, domain=domain, url_class=url_class)]
