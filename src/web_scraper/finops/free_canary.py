"""A few free fetches that can stop a hundred-thousand-URL run.

The paid canary asks "is the provider healthy?". This one asks the question that
comes first and matters more: **is the site still the site we wrote a profile
for?** A redesign, a DNS change, an origin outage, or an SSR page quietly
becoming client-rendered will all produce a run that completes, costs money at
the paid layer, and yields a dataset nobody can use.

Catching that after 100,000 URLs is expensive in three separate ways — the free
fetches, the paid escalations they trigger, and the hours before anyone notices.
Catching it in twelve fetches is free.

The sample is **stratified**, not the head of the queue. A queue head is usually
one domain and one url_class; a canary that only tests the easy corner grants
confidence it did not earn. Strata are chosen to cover the ways a profile breaks
independently:

* ``L0`` structured routes — a JSON endpoint can vanish while HTML still works;
* ``L1`` ordinary pages — the common path;
* ``CSR`` pages — the ones that need a browser, and the first to change;
* ``pagination`` entry points — where a listing shape change shows up;
* ``historically unstable`` URLs — whatever broke last time.

Verdicts are blunt on purpose:

``PASS``
    Representative URLs still resolve as the profile says. Start the run.
``WARN``
    Something degraded but the run is viable. Start it and watch.
``BLOCK_RUN``
    The profile no longer describes this site. A full run would burn budget to
    produce garbage.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from web_scraper.contracts import Verdict

#: Fraction of scored canaries that must resolve for a clean pass.
DEFAULT_PASS_RATE = 0.8

#: Below this the run is stopped rather than merely flagged.
DEFAULT_BLOCK_RATE = 0.5

#: Per stratum. Small enough to be free, large enough that one unlucky page
#: cannot decide a whole run.
DEFAULT_PER_STRATUM = 3

#: Verdicts that say something about the SITE rather than about this URL. A dead
#: URL in the sample is a stale seed list, not a redesign.
NEUTRAL_VERDICTS = frozenset({Verdict.DEAD_URL, Verdict.NOT_MODIFIED})

#: Verdicts that mean the profile no longer matches reality. These are the ones
#: worth stopping a run for, because every remaining URL will hit the same wall.
PROFILE_BROKEN_VERDICTS = frozenset(
    {Verdict.PARSE_FAIL, Verdict.THIN_CONTENT, Verdict.CSR_REQUIRED}
)


class FreeCanaryStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - a canary status, not a credential
    WARN = "WARN"
    BLOCK_RUN = "BLOCK_RUN"

    @property
    def allows_run(self) -> bool:
        return self is not FreeCanaryStatus.BLOCK_RUN


@dataclass(frozen=True)
class CanaryUrl:
    """One representative URL and what it is representative of."""

    url: str
    stratum: str
    url_class: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "stratum": self.stratum, "url_class": self.url_class}


@dataclass(frozen=True)
class FreeCanaryResult:
    url: str
    stratum: str
    verdict: Verdict | None
    level: str | None
    reason: str

    @property
    def is_neutral(self) -> bool:
        return self.verdict in NEUTRAL_VERDICTS if self.verdict is not None else False

    @property
    def resolved(self) -> bool:
        return self.verdict is Verdict.OK

    @property
    def profile_broken(self) -> bool:
        """The profile, not the network, is the problem."""

        return self.verdict in PROFILE_BROKEN_VERDICTS if self.verdict is not None else False

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "stratum": self.stratum,
            "verdict": self.verdict.value if self.verdict else None,
            "level": self.level,
            "resolved": self.resolved,
            "neutral": self.is_neutral,
            "profile_broken": self.profile_broken,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FreeCanaryOutcome:
    status: FreeCanaryStatus
    results: tuple[FreeCanaryResult, ...] = ()
    pass_rate_threshold: float = DEFAULT_PASS_RATE
    block_rate_threshold: float = DEFAULT_BLOCK_RATE
    detail: str = ""

    @property
    def scored(self) -> tuple[FreeCanaryResult, ...]:
        return tuple(r for r in self.results if not r.is_neutral)

    @property
    def resolved(self) -> int:
        return sum(1 for r in self.scored if r.resolved)

    @property
    def success_rate(self) -> float | None:
        return None if not self.scored else self.resolved / len(self.scored)

    @property
    def by_stratum(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for result in self.scored:
            bucket = out.setdefault(result.stratum, {"tested": 0, "resolved": 0})
            bucket["tested"] += 1
            bucket["resolved"] += 1 if result.resolved else 0
        return out

    @property
    def broken_strata(self) -> tuple[str, ...]:
        """Strata where nothing resolved. One stratum failing wholesale is a
        stronger signal than the same count of failures spread thinly."""

        return tuple(
            name
            for name, counts in sorted(self.by_stratum.items())
            if counts["tested"] and counts["resolved"] == 0
        )

    def explain(self) -> str:
        rate = "n/a" if self.success_rate is None else f"{self.success_rate:.0%}"
        lines = [
            f"free canary: {self.status.value}",
            f"resolved {self.resolved}/{len(self.scored)} scored URLs ({rate})",
            f"thresholds: pass >= {self.pass_rate_threshold:.0%}, "
            f"block < {self.block_rate_threshold:.0%}",
        ]
        if self.detail:
            lines.append(self.detail)
        for name, counts in sorted(self.by_stratum.items()):
            lines.append(f"  {name:<24} {counts['resolved']}/{counts['tested']}")
        neutral = [r for r in self.results if r.is_neutral]
        if neutral:
            lines.append(f"{len(neutral)} URLs excluded as neutral (dead seed, not modified)")
        for result in self.results:
            if not result.resolved and not result.is_neutral:
                lines.append(f"  FAIL [{result.stratum}] {result.url} — {result.reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "allows_run": self.status.allows_run,
            "resolved": self.resolved,
            "scored": len(self.scored),
            "success_rate": self.success_rate,
            "by_stratum": self.by_stratum,
            "broken_strata": list(self.broken_strata),
            "detail": self.detail,
            "results": [r.to_dict() for r in self.results],
            "explanation": self.explain(),
        }


def stratified_sample(
    candidates: Sequence[CanaryUrl],
    *,
    per_stratum: int = DEFAULT_PER_STRATUM,
    rng: random.Random | None = None,
) -> list[CanaryUrl]:
    """Take a few from each stratum rather than many from the easiest one."""

    # Sampling canaries, not generating secrets: reproducibility matters
    # here, cryptographic strength does not.
    picker = rng or random.Random()  # noqa: S311
    grouped: dict[str, list[CanaryUrl]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.stratum, []).append(candidate)

    chosen: list[CanaryUrl] = []
    for stratum in sorted(grouped):
        pool = grouped[stratum]
        if len(pool) <= per_stratum:
            chosen.extend(pool)
        else:
            chosen.extend(picker.sample(pool, per_stratum))
    return chosen


@dataclass
class FreeCanary:
    """Runs the free canary fetches and turns them into a go/no-go decision."""

    pass_rate: float = DEFAULT_PASS_RATE
    block_rate: float = DEFAULT_BLOCK_RATE
    per_stratum: int = DEFAULT_PER_STRATUM
    rng: random.Random | None = field(default=None)

    def __post_init__(self) -> None:
        if not 0.0 <= self.block_rate <= self.pass_rate <= 1.0:
            raise ValueError("need 0 <= block_rate <= pass_rate <= 1")

    def run(
        self,
        candidates: Sequence[CanaryUrl],
        *,
        fetch: Callable[[str], Any],
    ) -> FreeCanaryOutcome:
        """Fetch a stratified sample through the ordinary free gateway.

        ``fetch`` is the gateway's own ``fetch_url``, so the canary exercises the
        same routing, the same profile and the same triage the run will use. A
        canary with its own shortcut would be testing something else.
        """

        results: list[FreeCanaryResult] = []
        for candidate in stratified_sample(candidates, per_stratum=self.per_stratum, rng=self.rng):
            outcome = fetch(candidate.url)
            result = outcome.result
            last = result.attempts[-1] if result.attempts else None
            results.append(
                FreeCanaryResult(
                    url=candidate.url,
                    stratum=candidate.stratum,
                    verdict=result.verdict,
                    level=last.level.value if last else None,
                    reason=last.reason if last else "",
                )
            )
        return self._judge(tuple(results))

    def _judge(self, results: tuple[FreeCanaryResult, ...]) -> FreeCanaryOutcome:
        scored = [r for r in results if not r.is_neutral]

        if not results:
            return FreeCanaryOutcome(
                FreeCanaryStatus.PASS,
                results,
                self.pass_rate,
                self.block_rate,
                detail="no canary URLs to test; nothing to veto",
            )
        if not scored:
            # Everything was neutral. We learned nothing about the site, and
            # "learned nothing" must not read as "all clear".
            return FreeCanaryOutcome(
                FreeCanaryStatus.WARN,
                results,
                self.pass_rate,
                self.block_rate,
                detail=(
                    "every canary URL was neutral; the site was never actually "
                    "exercised, so this is not evidence of health"
                ),
            )

        rate = sum(1 for r in scored if r.resolved) / len(scored)
        broken = sum(1 for r in scored if r.profile_broken)

        outcome = FreeCanaryOutcome(FreeCanaryStatus.PASS, results, self.pass_rate, self.block_rate)
        # Order matters: both of the next two stop the run, but only one of them
        # tells the operator WHICH failure they are looking at. The specific
        # diagnosis is checked first so it is not masked by the generic rate.
        if broken and broken == len(scored):
            # Every scored URL failed for a profile reason rather than a network
            # one. That is a redesign, and a full run would produce a dataset
            # nobody can use, at whatever the paid layer costs on the way.
            status, detail = (
                FreeCanaryStatus.BLOCK_RUN,
                "every scored URL failed on extraction or rendering: profile drift, not an outage",
            )
        elif rate < self.block_rate:
            status, detail = (
                FreeCanaryStatus.BLOCK_RUN,
                "the site no longer resolves as the profile describes it",
            )
        elif outcome.broken_strata:
            status = FreeCanaryStatus.WARN
            detail = f"nothing resolved in: {', '.join(outcome.broken_strata)}"
        elif rate < self.pass_rate:
            status, detail = FreeCanaryStatus.WARN, "degraded but viable; watch the run"
        else:
            status, detail = FreeCanaryStatus.PASS, ""

        return FreeCanaryOutcome(status, results, self.pass_rate, self.block_rate, detail=detail)
