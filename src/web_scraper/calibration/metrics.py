"""Turning attempts into the one number that decides where money goes.

The headline is **USD per validated result**, and the reason it is the headline
rather than USD per request is arithmetic that vendors have no incentive to do
for you:

.. code-block:: text

    A: $0.0010/request, validates 50%  ->  $0.0020 per usable page
    B: $0.0015/request, validates 99%  ->  $0.0015 per usable page

A is 33% cheaper on the invoice and 33% more expensive in reality. Ranking on
list price picks A every time.

Three rules keep the number honest:

* **A single success is not a rate.** The Wilson lower bound is the gate: with
  one attempt it stays near zero however that attempt went, so a lucky first
  call cannot crown a strategy.
* **Unknown spend disqualifies the ratio.** If any call's cost went unreported,
  the numerator is a floor and the quotient would understate the true price.
  The metric reports ``None`` and says why, rather than printing a number that
  is wrong in the cheap direction.
* **Neutral outcomes are not failures.** A dead URL faithfully reported is the
  provider doing its job. Counting it against them would retire the honest
  vendors first — which is precisely backwards, since misreporting a dead URL
  is the defect that cost this project the most.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from web_scraper.calibration.harness import AttemptOutcome
from web_scraper.routing.stats import wilson_lower_bound


def _percentile(samples: Sequence[float], fraction: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    index = min(int(len(ordered) * fraction), len(ordered) - 1)
    return round(ordered[index], 1)


@dataclass
class StrategyMetrics:
    """Everything measured about one strategy in one place on one kind of page."""

    provider: str
    strategy: str
    domain: str
    url_class: str
    attempts: int = 0
    scored_attempts: int = 0
    validated_successes: int = 0
    blocks: int = 0
    provider_errors: int = 0
    neutral_outcomes: int = 0
    ineligible: int = 0
    early_stopped: int = 0
    #: How often the provider reported the status the site actually gives.
    status_checked: int = 0
    status_correct: int = 0
    exact_usd: Decimal = Decimal("0")
    provisional_usd: Decimal = Decimal("0")
    unknown_cost_calls: int = 0
    latencies: list[float] = field(default_factory=list)
    fields_expected: int = 0
    fields_extracted: int = 0
    discovery_candidates: int = 0
    discovery_validated: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    fingerprints: dict[str, int] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.strategy}"

    @property
    def success_rate(self) -> float | None:
        if self.scored_attempts == 0:
            return None
        return self.validated_successes / self.scored_attempts

    @property
    def confidence_bound(self) -> float:
        return wilson_lower_bound(self.validated_successes, self.scored_attempts)

    @property
    def known_usd(self) -> Decimal:
        return self.exact_usd + self.provisional_usd

    @property
    def cost_is_complete(self) -> bool:
        return self.unknown_cost_calls == 0

    @property
    def usd_per_request(self) -> Decimal | None:
        """What the invoice says. Kept only to show how misleading it is."""

        if self.attempts == 0 or not self.cost_is_complete:
            return None
        return (self.known_usd / Decimal(self.attempts)).quantize(Decimal("0.000001"))

    @property
    def usd_per_validated_result(self) -> Decimal | None:
        """The headline. ``None`` when it cannot be computed honestly."""

        if self.validated_successes == 0 or not self.cost_is_complete:
            return None
        return (self.known_usd / Decimal(self.validated_successes)).quantize(Decimal("0.000001"))

    @property
    def cost_unavailable_reason(self) -> str | None:
        if self.usd_per_validated_result is not None:
            return None
        if not self.cost_is_complete:
            return f"{self.unknown_cost_calls} call(s) with unattributed cost"
        if self.validated_successes == 0:
            return "no validated result to divide by"
        return None  # pragma: no cover - the two cases above are exhaustive

    @property
    def status_fidelity(self) -> float | None:
        if self.status_checked == 0:
            return None
        return self.status_correct / self.status_checked

    def observe(self, outcome: AttemptOutcome) -> None:
        if not outcome.attempted:
            if not outcome.eligible:
                self.ineligible += 1
            elif outcome.skip_reason:
                self.early_stopped += 1
            return

        self.attempts += 1
        if outcome.error_kind is not None:
            self.provider_errors += 1
            self.scored_attempts += 1
            self.unknown_cost_calls += 1
            return

        if outcome.scored:
            self.scored_attempts += 1
        else:
            self.neutral_outcomes += 1
        if outcome.validated:
            self.validated_successes += 1
        if outcome.verdict in {"BLOCKED", "SOFT_BLOCK"}:
            self.blocks += 1
        if outcome.verdict:
            self.verdicts[outcome.verdict] = self.verdicts.get(outcome.verdict, 0) + 1
        if outcome.block_signature:
            key = outcome.block_signature
            self.fingerprints[key] = self.fingerprints.get(key, 0) + 1

        if outcome.status_fidelity is not None:
            self.status_checked += 1
            self.status_correct += 1 if outcome.status_fidelity else 0
        if outcome.latency_ms is not None:
            self.latencies.append(float(outcome.latency_ms))

        usd = Decimal(outcome.cost_usd) if outcome.cost_usd is not None else None
        if outcome.cost_certainty == "EXACT" and usd is not None:
            self.exact_usd += usd
        elif outcome.cost_certainty == "PROVISIONAL" and usd is not None:
            self.provisional_usd += usd
        elif outcome.cost_certainty in {"EXACT", "PROVISIONAL"}:
            # Native units known, money not: no tariff covers this strategy. The
            # spend is real and unpriceable, which is an unknown for this metric.
            self.unknown_cost_calls += 1
        else:
            self.unknown_cost_calls += 1

        self.fields_expected += outcome.fields_expected
        self.fields_extracted += outcome.fields_extracted
        self.discovery_candidates += outcome.discovery_candidates
        self.discovery_validated += outcome.discovery_validated

    def to_dict(self) -> dict[str, Any]:
        cpvr = self.usd_per_validated_result
        return {
            "ref": self.ref,
            "provider": self.provider,
            "strategy": self.strategy,
            "domain": self.domain,
            "url_class": self.url_class,
            "attempts": self.attempts,
            "scored_attempts": self.scored_attempts,
            "validated_successes": self.validated_successes,
            "success_rate": None if self.success_rate is None else round(self.success_rate, 4),
            "confidence_bound": round(self.confidence_bound, 4),
            "blocks": self.blocks,
            "provider_errors": self.provider_errors,
            "neutral_outcomes": self.neutral_outcomes,
            "ineligible": self.ineligible,
            "early_stopped": self.early_stopped,
            "status_fidelity": (
                None if self.status_fidelity is None else round(self.status_fidelity, 4)
            ),
            "status_checked": self.status_checked,
            "exact_usd": str(self.exact_usd),
            "provisional_usd": str(self.provisional_usd),
            "unknown_cost_calls": self.unknown_cost_calls,
            "cost_is_complete": self.cost_is_complete,
            "usd_per_request": None if self.usd_per_request is None else str(self.usd_per_request),
            "usd_per_validated_result": None if cpvr is None else str(cpvr),
            "cost_unavailable_reason": self.cost_unavailable_reason,
            "p50_ms": _percentile(self.latencies, 0.50),
            "p95_ms": _percentile(self.latencies, 0.95),
            "mean_ms": round(statistics.fmean(self.latencies), 1) if self.latencies else None,
            "fields_expected": self.fields_expected,
            "fields_extracted": self.fields_extracted,
            "discovery_candidates": self.discovery_candidates,
            "discovery_validated": self.discovery_validated,
            "verdicts": dict(sorted(self.verdicts.items())),
            "failure_fingerprints": dict(sorted(self.fingerprints.items())),
        }


def aggregate(
    outcomes: Iterable[AttemptOutcome], *, by_kind: bool = False
) -> dict[tuple[str, ...], StrategyMetrics]:
    """Fold attempts into per-strategy records.

    ``by_kind`` swaps the url_class dimension for the target kind, which is how
    segment winners are computed without a second pass over the data.
    """

    out: dict[tuple[str, ...], StrategyMetrics] = {}
    for outcome in outcomes:
        third = outcome.target_kind if by_kind else outcome.url_class
        key = (outcome.provider, outcome.strategy, outcome.domain, third)
        metrics = out.get(key)
        if metrics is None:
            metrics = StrategyMetrics(
                provider=outcome.provider,
                strategy=outcome.strategy,
                domain=outcome.domain,
                url_class=third,
            )
            out[key] = metrics
        metrics.observe(outcome)
    return out


@dataclass(frozen=True)
class Ranked:
    """One strategy's place in a segment, with the reason it is there."""

    metrics: StrategyMetrics
    passes_confidence: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.metrics.to_dict(),
            "passes_confidence": self.passes_confidence,
            "reason": self.reason,
        }


def rank(
    metrics: Iterable[StrategyMetrics],
    *,
    minimum_confidence: float,
    min_observations: int = 3,
) -> list[Ranked]:
    """Order a segment: reliable first, then cheapest per validated result.

    Anything that cannot price itself sorts below everything that can. "We do
    not know what this costs" must never win a cost comparison by default —
    that is how an unpriced strategy becomes the cheapest thing in the table.
    """

    ranked: list[Ranked] = []
    for record in metrics:
        if record.attempts == 0:
            continue
        if record.scored_attempts < min_observations:
            ranked.append(
                Ranked(
                    record,
                    False,
                    f"only {record.scored_attempts} scored attempt(s); "
                    f"{min_observations} needed before a rate means anything",
                )
            )
            continue
        passes = record.confidence_bound >= minimum_confidence
        reason = (
            f"{record.validated_successes}/{record.scored_attempts} validated, "
            f"Wilson LB {record.confidence_bound:.3f}"
        )
        if not passes:
            reason += f" below the {minimum_confidence:.3f} gate"
        if not record.cost_is_complete:
            reason += f"; {record.unknown_cost_calls} call(s) unpriced"
        ranked.append(Ranked(record, passes, reason))

    def sort_key(item: Ranked) -> tuple[Any, ...]:
        cost = item.metrics.usd_per_validated_result
        return (
            not item.passes_confidence,
            cost if cost is not None else Decimal("999999"),
            -item.metrics.confidence_bound,
        )

    ranked.sort(key=sort_key)
    return ranked


def concentration(outcomes: Iterable[AttemptOutcome]) -> dict[str, Any]:
    """How much of the paid traffic one vendor would carry.

    Not balanced automatically — that would be trading money for a risk the
    operator may be happy to hold. It is reported because a fleet that has
    quietly become a single point of failure looks identical to a well-tuned one
    right up to the morning the vendor has an outage.
    """

    calls: dict[str, int] = {}
    spend: dict[str, Decimal] = {}
    for outcome in outcomes:
        if not outcome.attempted:
            continue
        calls[outcome.provider] = calls.get(outcome.provider, 0) + 1
        spend[outcome.provider] = spend.get(outcome.provider, Decimal("0")) + Decimal(
            outcome.charged_usd or "0"
        )
    total_calls = sum(calls.values())
    total_spend = sum(spend.values(), Decimal("0"))
    top = max(calls, key=lambda k: calls[k]) if calls else None
    return {
        "calls_by_provider": dict(sorted(calls.items())),
        "spend_by_provider": {k: str(v) for k, v in sorted(spend.items())},
        "share_of_calls": {
            k: round(v / total_calls, 4) for k, v in sorted(calls.items()) if total_calls
        },
        "top_provider": top,
        "top_provider_share": (round(calls[top] / total_calls, 4) if top and total_calls else None),
        "total_calls": total_calls,
        "total_spend_usd": str(total_spend),
    }


def by_fingerprint(outcomes: Iterable[AttemptOutcome]) -> dict[str, dict[str, Any]]:
    """Secondary analysis: which vendor wins against which defence.

    Deliberately secondary. The statistics key stays
    ``provider + strategy + domain + url_class``; a fingerprint cut that
    replaced it would merge sites that share an anti-bot vendor and nothing else.
    """

    groups: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        if not outcome.attempted or not outcome.block_signature:
            continue
        group = groups.setdefault(
            outcome.block_signature, {"attempts": 0, "by_strategy": {}, "beaten_by": []}
        )
        group["attempts"] += 1
        stats = group["by_strategy"].setdefault(
            f"{outcome.provider}:{outcome.strategy}", {"attempts": 0, "validated": 0}
        )
        stats["attempts"] += 1
        if outcome.validated:
            stats["validated"] += 1
    for group in groups.values():
        group["beaten_by"] = sorted(
            (ref for ref, s in group["by_strategy"].items() if s["validated"]),
        )
    return groups


def segment_winners(
    outcomes: Iterable[AttemptOutcome],
    *,
    minimum_confidence: float,
    min_observations: int = 3,
) -> dict[str, Any]:
    """Best strategy per target kind, or an honest 'not enough evidence'."""

    materialised = list(outcomes)
    per_kind = aggregate(materialised, by_kind=True)
    kinds: dict[str, list[StrategyMetrics]] = {}
    for (_, _, _, kind), metrics in per_kind.items():
        kinds.setdefault(kind, []).append(metrics)

    winners: dict[str, Any] = {}
    for kind, records in sorted(kinds.items()):
        ordered = rank(
            records, minimum_confidence=minimum_confidence, min_observations=min_observations
        )
        qualified = [r for r in ordered if r.passes_confidence]
        winners[kind] = {
            "winner": qualified[0].metrics.ref if qualified else None,
            "reason": (
                qualified[0].reason
                if qualified
                else "no strategy cleared the confidence gate on this segment"
            ),
            "ranked": [r.to_dict() for r in ordered],
        }
    return winners


def totals(outcomes: Iterable[AttemptOutcome]) -> dict[str, Any]:
    """Session-level arithmetic, with the three certainties kept apart."""

    materialised = list(outcomes)
    exact = sum(
        (Decimal(o.cost_usd) for o in materialised if o.cost_certainty == "EXACT" and o.cost_usd),
        Decimal("0"),
    )
    provisional = sum(
        (
            Decimal(o.cost_usd)
            for o in materialised
            if o.cost_certainty == "PROVISIONAL" and o.cost_usd
        ),
        Decimal("0"),
    )
    unknown_calls = sum(1 for o in materialised if o.attempted and not o.cost_known)
    charged = sum((Decimal(o.charged_usd or "0") for o in materialised), Decimal("0"))
    attempted = [o for o in materialised if o.attempted]
    validated = [o for o in attempted if o.validated]
    checked = [o for o in attempted if o.status_fidelity is not None]
    return {
        "planned_calls": len(materialised),
        "attempted": len(attempted),
        "ineligible": sum(1 for o in materialised if not o.attempted and not o.eligible),
        "skipped_early": sum(
            1 for o in materialised if not o.attempted and o.eligible and o.skip_reason
        ),
        "validated": len(validated),
        "provider_errors": sum(1 for o in attempted if o.error_kind),
        "exact_usd": str(exact),
        "provisional_usd": str(provisional),
        "unknown_cost_calls": unknown_calls,
        "charged_usd": str(charged),
        "cost_is_complete": unknown_calls == 0,
        "usd_per_validated_result": (
            str((exact + provisional) / Decimal(len(validated)))
            if validated and not unknown_calls
            else None
        ),
        "status_fidelity": (
            round(sum(1 for o in checked if o.status_fidelity) / len(checked), 4)
            if checked
            else None
        ),
    }


def recommendation(
    stats_path: Any,
    *,
    providers: Sequence[Any],
    domain: str,
    url_class: str,
    verdict: Any = None,
    pricing: Any = None,
    minimum_confidence: float | None = None,
) -> Mapping[str, Any]:
    """What the REAL router would now do, given this evidence.

    Deliberately the production class rather than a ranking written for the
    report. A recommendation computed by a second implementation would be a
    claim about that implementation; this one is a rehearsal of the decision the
    run will actually take.
    """

    from web_scraper.providers.multi_router import MultiProviderRouter
    from web_scraper.providers.pricing import PricingBook
    from web_scraper.providers.stats import ProviderStatsStore

    store = (
        stats_path if isinstance(stats_path, ProviderStatsStore) else ProviderStatsStore(stats_path)
    )
    router = MultiProviderRouter(
        providers=list(providers),
        stats=store,
        pricing=pricing or PricingBook(),
        # A recommendation must be reproducible: a shadow probe is a real thing
        # the run may do, but a report that quoted a different winner on each
        # invocation would be unusable for approval.
        _rng=lambda: 1.0,
    )
    if minimum_confidence is not None:
        router.minimum_confidence_bound = minimum_confidence
    decision = router.choose(domain=domain, url_class=url_class, verdict=verdict)
    return decision.to_dict()
