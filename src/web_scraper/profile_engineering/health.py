"""Whether a certified profile is still telling the truth, judged from real runs.

Certification is a photograph. It says the profile was correct against a corpus
on one day. What decides whether it is correct *now* is production: how often
critical fields actually arrive, how often two sources disagree, how often the
crawl has to reach for a browser or a paid provider to get what plain HTTP used
to give it.

The distinction this module exists to hold is between a bad night and a broken
profile. A site with a two-hour outage produces exactly the same first hour of
signal as a site that has been redesigned, and reacting to the first one costs a
day of pointless work while reacting late to the second costs a month of quietly
wrong data. So degradation needs *sustained* evidence over a window, and one
transient failure moves nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    #: Something moved. Not acted on, but worth an operator's eyes.
    WATCH = "WATCH"
    DEGRADED = "DEGRADED"
    #: The profile is producing data that should not be published.
    CRITICAL = "CRITICAL"

    @property
    def should_degrade_profile(self) -> bool:
        return self in {HealthState.DEGRADED, HealthState.CRITICAL}


@dataclass(frozen=True)
class HealthThresholds:
    """The lines that separate a bad night from a broken profile.

    Every figure is configuration rather than a constant in the middle of a
    function, because the right value depends on the site: a rankings page that
    changes hourly and an archive that changes yearly do not deserve the same
    patience.
    """

    #: How many runs must agree before anything changes state. One run is
    #: weather.
    min_runs: int = 3
    #: Critical-field availability. Below the first, watch; below the second,
    #: the profile is no longer delivering what it promised.
    critical_watch: float = 0.98
    critical_degraded: float = 0.90
    critical_critical: float = 0.60
    #: Share of quorum comparisons that disagree.
    conflict_watch: float = 0.01
    conflict_degraded: float = 0.05
    #: How much of the traffic had to escalate beyond the profile's own route.
    escalation_watch: float = 0.20
    escalation_degraded: float = 0.50
    #: A single schema-drift event is worth watching; a run of them is not a
    #: coincidence.
    drift_watch: int = 1
    drift_degraded: int = 3
    #: A crawl that cannot finish its pagination is not producing a dataset.
    incomplete_degraded: float = 0.10

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_runs": self.min_runs,
            "critical_watch": self.critical_watch,
            "critical_degraded": self.critical_degraded,
            "critical_critical": self.critical_critical,
            "conflict_watch": self.conflict_watch,
            "conflict_degraded": self.conflict_degraded,
            "escalation_watch": self.escalation_watch,
            "escalation_degraded": self.escalation_degraded,
            "drift_watch": self.drift_watch,
            "drift_degraded": self.drift_degraded,
            "incomplete_degraded": self.incomplete_degraded,
        }


@dataclass(frozen=True)
class RunSample:
    """What one production run said about one profile."""

    run_id: str
    urls: int
    validated: int
    critical_fields_expected: int
    critical_fields_found: int
    quorum_comparisons: int = 0
    quorum_conflicts: int = 0
    browser_escalations: int = 0
    paid_escalations: int = 0
    schema_drift_events: int = 0
    pagination_incomplete: int = 0

    @property
    def critical_rate(self) -> float | None:
        if self.critical_fields_expected == 0:
            return None
        return self.critical_fields_found / self.critical_fields_expected

    @property
    def conflict_rate(self) -> float | None:
        if self.quorum_comparisons == 0:
            return None
        return self.quorum_conflicts / self.quorum_comparisons

    @property
    def escalation_rate(self) -> float | None:
        if self.urls == 0:
            return None
        return (self.browser_escalations + self.paid_escalations) / self.urls

    @property
    def incomplete_rate(self) -> float | None:
        if self.urls == 0:
            return None
        return self.pagination_incomplete / self.urls

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "urls": self.urls,
            "validated": self.validated,
            "critical_rate": self.critical_rate,
            "conflict_rate": self.conflict_rate,
            "escalation_rate": self.escalation_rate,
            "schema_drift_events": self.schema_drift_events,
            "incomplete_rate": self.incomplete_rate,
        }


@dataclass
class HealthReport:
    """The state, and every signal that argued for it."""

    domain: str
    state: HealthState
    runs_considered: int
    signals: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def should_degrade_profile(self) -> bool:
        return self.state.should_degrade_profile

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "state": self.state.value,
            "runs_considered": self.runs_considered,
            "signals": list(self.signals),
            "metrics": dict(self.metrics),
            "should_degrade_profile": self.should_degrade_profile,
        }

    def describe(self) -> str:
        lines = [f"{self.domain}: {self.state.value} (over {self.runs_considered} run(s))"]
        lines.extend(f"  - {signal}" for signal in self.signals)
        if not self.signals:
            lines.append("  - nothing has moved")
        return "\n".join(lines)


def assess_health(
    domain: str,
    samples: Sequence[RunSample],
    *,
    thresholds: HealthThresholds | None = None,
    baseline: Mapping[str, float] | None = None,
) -> HealthReport:
    """Judge a profile from its recent runs.

    ``baseline`` is what the profile used to do — usually the figures recorded
    at certification. A fall from 99.8% to 91% is a much louder signal than 91%
    on its own, and without the baseline the second reading looks acceptable.
    """

    limits = thresholds or HealthThresholds()
    window = list(samples)[-max(limits.min_runs, 1) * 3 :]

    if len(window) < limits.min_runs:
        return HealthReport(
            domain=domain,
            state=HealthState.HEALTHY,
            runs_considered=len(window),
            signals=[
                f"only {len(window)} run(s); {limits.min_runs} are needed before a "
                "state change, because one bad night is not a broken profile"
            ],
            metrics={"insufficient_runs": True},
        )

    critical = _mean(s.critical_rate for s in window)
    conflicts = _mean(s.conflict_rate for s in window)
    escalation = _mean(s.escalation_rate for s in window)
    incomplete = _mean(s.incomplete_rate for s in window)
    drift = sum(s.schema_drift_events for s in window)

    signals: list[str] = []
    state = HealthState.HEALTHY

    def escalate(to: HealthState, why: str) -> None:
        nonlocal state
        signals.append(why)
        order = [HealthState.HEALTHY, HealthState.WATCH, HealthState.DEGRADED, HealthState.CRITICAL]
        if order.index(to) > order.index(state):
            state = to

    if critical is not None:
        if critical < limits.critical_critical:
            escalate(
                HealthState.CRITICAL,
                f"critical fields present on {critical:.1%} of records — the dataset is wrong, not thin",
            )
        elif critical < limits.critical_degraded:
            escalate(HealthState.DEGRADED, f"critical field availability {critical:.1%}")
        elif critical < limits.critical_watch:
            escalate(HealthState.WATCH, f"critical field availability {critical:.1%}")

        previous = (baseline or {}).get("critical_rate")
        if previous is not None and previous - critical >= 0.05:
            escalate(
                HealthState.DEGRADED,
                f"critical field availability fell from {previous:.1%} to {critical:.1%} "
                "against the certified baseline",
            )

    if conflicts is not None:
        if conflicts >= limits.conflict_degraded:
            escalate(
                HealthState.DEGRADED,
                f"{conflicts:.1%} of quorum comparisons disagree; one of the sources is wrong",
            )
        elif conflicts >= limits.conflict_watch:
            escalate(HealthState.WATCH, f"{conflicts:.1%} quorum conflicts")

    if escalation is not None:
        if escalation >= limits.escalation_degraded:
            escalate(
                HealthState.DEGRADED,
                f"{escalation:.1%} of URLs escalated past the profile's own route",
            )
        elif escalation >= limits.escalation_watch:
            escalate(HealthState.WATCH, f"{escalation:.1%} escalation rate")

    if drift >= limits.drift_degraded:
        escalate(HealthState.DEGRADED, f"{drift} schema drift events in the window")
    elif drift >= limits.drift_watch:
        escalate(HealthState.WATCH, f"{drift} schema drift event(s)")

    if incomplete is not None and incomplete >= limits.incomplete_degraded:
        escalate(
            HealthState.DEGRADED,
            f"{incomplete:.1%} of listings ended without proving they were complete",
        )

    return HealthReport(
        domain=domain,
        state=state,
        runs_considered=len(window),
        signals=signals,
        metrics={
            "critical_rate": critical,
            "conflict_rate": conflicts,
            "escalation_rate": escalation,
            "incomplete_rate": incomplete,
            "schema_drift_events": drift,
            "thresholds": limits.to_dict(),
        },
    )


def _mean(values: Iterable[float | None]) -> float | None:
    collected = [v for v in values if v is not None]
    if not collected:
        return None
    return sum(collected) / len(collected)
