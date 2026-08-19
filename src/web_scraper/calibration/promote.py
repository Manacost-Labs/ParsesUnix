"""Moving calibration evidence into production memory — only when told to.

The default is that this never happens. A calibration session deliberately
calls strategies with no track record, on targets picked to be difficult, and
sometimes stops a vendor half way through when its cap runs out. Evidence
gathered under those conditions is *useful* — it is why the session exists — but
it is not the same thing as evidence from ordinary traffic, and letting it flow
into the router automatically would mean production routing quietly changed
every time somebody ran a benchmark.

So promotion is a separate, explicit act with a preview in front of it. The
preview names every key that would change and what it would become; nothing
about it is a summary that could hide a surprise.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from web_scraper.providers.stats import ProviderStatsStore, ProviderStrategyStats


@dataclass(frozen=True)
class PromotionItem:
    """One statistics key, before and after."""

    stats: ProviderStrategyStats
    existing: ProviderStrategyStats | None

    @property
    def is_new(self) -> bool:
        return self.existing is None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.stats.key.to_dict(),
            "new_key": self.is_new,
            "importing": {
                "attempts": self.stats.attempts,
                "scored_attempts": self.stats.scored_attempts,
                "validated_successes": self.stats.validated_successes,
                "provider_errors": self.stats.provider_errors,
                "known_cost": str(self.stats.known_cost),
                "unknown_cost_calls": self.stats.unknown_cost_calls,
                "confidence_bound": round(self.stats.confidence_bound, 4),
            },
            "existing": (
                None
                if self.existing is None
                else {
                    "attempts": self.existing.attempts,
                    "scored_attempts": self.existing.scored_attempts,
                    "validated_successes": self.existing.validated_successes,
                    "confidence_bound": round(self.existing.confidence_bound, 4),
                }
            ),
        }


@dataclass(frozen=True)
class PromotionPlan:
    """Everything that would change, as a thing a human reads before saying yes."""

    items: tuple[PromotionItem, ...]
    source: str
    destination: str

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted({i.stats.key.provider for i in self.items}))

    @property
    def domains(self) -> tuple[str, ...]:
        return tuple(sorted({i.stats.key.domain for i in self.items}))

    @property
    def url_classes(self) -> tuple[str, ...]:
        return tuple(sorted({i.stats.key.url_class for i in self.items}))

    @property
    def strategies(self) -> tuple[str, ...]:
        return tuple(sorted({i.stats.key.strategy_ref for i in self.items}))

    @property
    def total_attempts(self) -> int:
        return sum(i.stats.attempts for i in self.items)

    @property
    def unpriced_calls(self) -> int:
        return sum(i.stats.unknown_cost_calls for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "keys": len(self.items),
            "new_keys": sum(1 for i in self.items if i.is_new),
            "providers": list(self.providers),
            "domains": list(self.domains),
            "url_classes": list(self.url_classes),
            "strategies": list(self.strategies),
            "total_attempts": self.total_attempts,
            "unpriced_calls": self.unpriced_calls,
            "items": [i.to_dict() for i in self.items],
        }

    def describe(self) -> str:
        """The preview. Deliberately per-key: an aggregate can hide a surprise."""

        if not self.items:
            return "nothing to promote: the calibration store holds no evidence"
        lines = [
            f"from: {self.source}",
            f"to:   {self.destination}",
            "",
            f"{len(self.items)} statistics key(s), {self.total_attempts} attempt(s)",
            f"providers:   {', '.join(self.providers)}",
            f"domains:     {', '.join(self.domains)}",
            f"url classes: {', '.join(self.url_classes)}",
            "",
        ]
        for item in self.items:
            key = item.stats.key
            prior = item.existing
            existing = (
                "new"
                if prior is None
                else f"merging into {prior.validated_successes}/{prior.scored_attempts}"
            )
            lines.append(
                f"  {key.strategy_ref:<26} {key.domain}/{key.url_class:<12} "
                f"{item.stats.validated_successes}/{item.stats.scored_attempts} validated "
                f"({existing})"
            )
        if self.unpriced_calls:
            lines.append("")
            lines.append(
                f"note: {self.unpriced_calls} of these calls had no attributable cost; "
                "the router will treat that strategy's spend as incomplete"
            )
        lines.append("")
        lines.append("nothing has been written. Re-run with --yes to apply.")
        return "\n".join(lines)


def plan_promotion(
    calibration: ProviderStatsStore,
    production: ProviderStatsStore,
    *,
    providers: tuple[str, ...] = (),
    domains: tuple[str, ...] = (),
    min_scored_attempts: int = 1,
) -> PromotionPlan:
    """What promoting this session would do, without doing any of it."""

    items: list[PromotionItem] = []
    for stats in calibration.all_stats():
        key = stats.key
        if providers and key.provider not in providers:
            continue
        if domains and key.domain not in domains:
            continue
        if stats.scored_attempts < min_scored_attempts:
            # A key with nothing scored carries no signal the router can use,
            # and importing it would only add rows that look like history.
            continue
        items.append(PromotionItem(stats=stats, existing=production.get(key)))
    items.sort(key=lambda i: (i.stats.key.provider, i.stats.key.strategy_id, i.stats.key.domain))
    return PromotionPlan(
        items=tuple(items),
        source=str(calibration.path),
        destination=str(production.path),
    )


def apply_promotion(plan: PromotionPlan, production: ProviderStatsStore) -> dict[str, Any]:
    """Merge the reviewed evidence in. Called only after an explicit yes."""

    merged = 0
    added_attempts = 0
    for item in plan.items:
        production.merge(item.stats)
        merged += 1
        added_attempts += item.stats.attempts
    return {
        "promoted_keys": merged,
        "promoted_attempts": added_attempts,
        "destination": str(production.path),
        "unpriced_calls": plan.unpriced_calls,
        "cost_recorded": str(sum((i.stats.known_cost for i in plan.items), Decimal("0"))),
    }
