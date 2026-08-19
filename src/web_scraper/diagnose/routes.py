"""Is this URL class worth moving from the browser onto a discovered API?

The question an operator actually has to answer, with the numbers needed to
answer it and no numbers that were not measured.

The refusal that shapes this module: **a saving is only reported where it was
observed**. Latency comparison needs both routes to have been timed. Cost
comparison needs cost history. Where either is missing the answer is UNKNOWN,
printed as UNKNOWN, rather than a plausible figure derived from a list price and
an assumption. A migration decision made on an invented number is worse than one
made on an admitted gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from web_scraper.discovery.store import Evidence, EvidenceState


@dataclass(frozen=True)
class RouteMeasurement:
    """What is known about one route's behaviour. Any field may be unknown."""

    label: str
    samples: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None
    cost_per_call: Decimal | None = None
    cost_unit: str = ""

    @property
    def measured(self) -> bool:
        return self.samples > 0 and self.p95_ms is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "samples": self.samples,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "cost_per_call": None if self.cost_per_call is None else str(self.cost_per_call),
            "cost_unit": self.cost_unit,
            "measured": self.measured,
        }


@dataclass(frozen=True)
class RouteComparison:
    """The current route against a validated candidate, honestly."""

    url_class: str
    current: RouteMeasurement
    candidate: RouteMeasurement
    evidence: Evidence
    critical_fields: tuple[str, ...] = ()
    browser_renders_observed: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def field_coverage(self) -> str:
        if not self.critical_fields:
            return "no critical fields declared"
        covered = sum(1 for f in self.critical_fields if f in self.evidence.matched_fields)
        return f"{covered}/{len(self.critical_fields)}"

    @property
    def fully_covers_fields(self) -> bool:
        return bool(self.critical_fields) and all(
            f in self.evidence.matched_fields for f in self.critical_fields
        )

    @property
    def latency_saving_ms(self) -> float | None:
        """``None`` unless BOTH routes were actually timed."""

        if not (self.current.measured and self.candidate.measured):
            return None
        assert self.current.p95_ms is not None and self.candidate.p95_ms is not None
        return self.current.p95_ms - self.candidate.p95_ms

    @property
    def cost_saving(self) -> str:
        """A figure only when both sides have a cost. Otherwise UNKNOWN."""

        if self.current.cost_per_call is None or self.candidate.cost_per_call is None:
            return "UNKNOWN"
        saved = self.current.cost_per_call - self.candidate.cost_per_call
        return f"{saved} {self.current.cost_unit or 'per call'}"

    @property
    def recommendation(self) -> str:
        """What to do, stated in terms of what is actually known."""

        if self.evidence.state is not EvidenceState.VALIDATED:
            return f"NOT READY — evidence is {self.evidence.state.value}"
        if not self.fully_covers_fields:
            return (
                f"NOT READY — the endpoint supplies {self.field_coverage} critical fields; "
                "a cheaper route that returns different data is not an optimisation"
            )
        saving = self.latency_saving_ms
        if saving is None:
            return (
                "READY TO REVIEW — fields covered and evidence validated, but the two "
                "routes were not both timed, so no speed claim is made"
            )
        if saving <= 0:
            return "READY TO REVIEW — the API route is not faster; the case is stability, not speed"
        return f"READY TO REVIEW — p95 {saving:.0f} ms lower and every critical field covered"

    def explain(self) -> str:
        lines = [
            f"url_class: {self.url_class}",
            "",
            f"Current route:      {self.current.label}",
            f"Validated candidate: {self.evidence.method} {self.evidence.endpoint}",
            "",
            f"Observed on:        {self.evidence.distinct_pages} distinct pages",
            f"Observations:       {self.evidence.observation_count}",
            f"Critical fields:    {self.field_coverage}",
            f"Schema changes:     {self.evidence.schema_changes}",
            f"Evidence weight:    {self.evidence.decay_factor(now=self.evidence.last_seen):.2f}",
        ]
        if self.evidence.pagination.get("strategy", "NONE") != "NONE":
            lines.append(f"Pagination:         {self.evidence.pagination['strategy'].lower()}")
        lines.append("")
        lines.append(_measure_line("Current route p95", self.current))
        lines.append(_measure_line("API route p95", self.candidate))
        saving = self.latency_saving_ms
        lines.append(
            f"Latency saving:     {saving:.0f} ms"
            if saving is not None
            else "Latency saving:     UNKNOWN (both routes were not timed)"
        )
        lines.append(f"Cost saving:        {self.cost_saving}")
        if self.browser_renders_observed:
            lines.append(f"Browser renders:    {self.browser_renders_observed} observed")
        lines.extend(["", self.recommendation])
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url_class": self.url_class,
            "current": self.current.to_dict(),
            "candidate": self.candidate.to_dict(),
            "evidence": self.evidence.to_dict(),
            "critical_fields": list(self.critical_fields),
            "field_coverage": self.field_coverage,
            "fully_covers_fields": self.fully_covers_fields,
            "latency_saving_ms": self.latency_saving_ms,
            "cost_saving": self.cost_saving,
            "browser_renders_observed": self.browser_renders_observed,
            "recommendation": self.recommendation,
            "explanation": self.explain(),
            "notes": list(self.notes),
        }


def _measure_line(label: str, measurement: RouteMeasurement) -> str:
    if not measurement.measured:
        return f"{label + ':':<20}UNKNOWN (not measured)"
    assert measurement.p95_ms is not None
    return f"{label + ':':<20}{measurement.p95_ms:.0f} ms  (n={measurement.samples})"


def compare_routes(
    evidence_items: list[Evidence],
    *,
    critical_fields: tuple[str, ...] = (),
    current: RouteMeasurement | None = None,
    candidate_measurements: dict[str, RouteMeasurement] | None = None,
    browser_renders: int = 0,
) -> list[RouteComparison]:
    """One comparison per validated endpoint, best evidence first."""

    measurements = candidate_measurements or {}
    fallback = current or RouteMeasurement(label="L2 browser")
    out = [
        RouteComparison(
            url_class=item.url_class or "unknown",
            current=fallback,
            candidate=measurements.get(
                item.identity, RouteMeasurement(label=f"{item.method} {item.endpoint}")
            ),
            evidence=item,
            critical_fields=critical_fields,
            browser_renders_observed=browser_renders,
        )
        for item in evidence_items
        if item.state is EvidenceState.VALIDATED
    ]
    out.sort(key=lambda c: (-c.evidence.distinct_pages, c.evidence.endpoint))
    return out


def describe_comparisons(comparisons: list[RouteComparison]) -> str:
    if not comparisons:
        return (
            "no validated route candidates yet\n\n"
            "Evidence accumulates across runs: an endpoint needs to appear on several "
            "distinct pages before it is worth proposing."
        )
    header = f"{len(comparisons)} validated candidate(s) worth reviewing\n"
    return header + "\n\n" + "\n\n---\n\n".join(c.explain() for c in comparisons)
