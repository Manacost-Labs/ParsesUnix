"""A few real paid calls that can veto a large paid batch.

Sending ten thousand URLs to a provider on the strength of yesterday's
statistics is a bet that nothing changed overnight. Sites deploy new defences,
vendors have bad days, and an account's quota runs out. The canary spends a
handful of credits to find out which of those is true *before* the batch spends
thousands.

The three outcomes are deliberately blunt, because an operator reading this at
three in the morning needs a decision, not a nuance:

``PASS``
    Enough canaries validated. Run the batch.
``WARN``
    Degraded but working. Run it, watch it — and the report says what degraded.
``BLOCK_PAID_PHASE``
    Do not spend the rest. Something is wrong that the batch will only multiply.

An important asymmetry: canary URLs that come back with a *neutral* verdict — a
dead URL, an origin outage — are excluded from the scoring rather than counted
as failures. Blocking a paid phase because someone's origin was down for a
minute would be an outage of our own making, on top of theirs.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from web_scraper.contracts import Cost, Verdict
from web_scraper.providers.stats import NEUTRAL_VERDICTS

#: Fraction of scored canaries that must validate for a clean pass.
DEFAULT_PASS_RATE = 0.7

#: Below this, the batch is stopped rather than merely flagged.
DEFAULT_BLOCK_RATE = 0.4

#: How many URLs to spend on finding out. Small enough to be cheap, large enough
#: that one unlucky page cannot decide the whole run.
DEFAULT_CANARY_SIZE = 5
MIN_CANARY_SIZE = 3
MAX_CANARY_SIZE = 10


class CanaryStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - a canary status, not a credential
    WARN = "WARN"
    BLOCK_PAID_PHASE = "BLOCK_PAID_PHASE"

    @property
    def allows_batch(self) -> bool:
        return self is not CanaryStatus.BLOCK_PAID_PHASE


class PaidAttemptLike(Protocol):
    """The part of a paid attempt the canary reads."""

    attempted: bool
    reason: str

    @property
    def succeeded(self) -> bool: ...


@dataclass(frozen=True)
class CanaryResult:
    url: str
    attempted: bool
    succeeded: bool
    verdict: Verdict | None
    provider: str | None
    cost: Cost
    reason: str

    @property
    def is_neutral(self) -> bool:
        """A verdict about the target, which says nothing about the provider."""

        return self.verdict in NEUTRAL_VERDICTS if self.verdict is not None else False

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "verdict": self.verdict.value if self.verdict else None,
            "provider": self.provider,
            "cost": self.cost.to_dict(),
            "neutral": self.is_neutral,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CanaryOutcome:
    """The verdict on whether the paid batch may run."""

    status: CanaryStatus
    results: tuple[CanaryResult, ...] = ()
    pass_rate_threshold: float = DEFAULT_PASS_RATE
    block_rate_threshold: float = DEFAULT_BLOCK_RATE
    detail: str = ""

    @property
    def scored(self) -> tuple[CanaryResult, ...]:
        """Canaries that actually tested the provider."""

        return tuple(r for r in self.results if r.attempted and not r.is_neutral)

    @property
    def validated(self) -> int:
        return sum(1 for r in self.scored if r.succeeded)

    @property
    def success_rate(self) -> float | None:
        return None if not self.scored else self.validated / len(self.scored)

    @property
    def spent(self) -> Cost:
        """What finding this out cost. Unknown if any canary's cost was."""

        if any(not r.cost.is_known for r in self.results):
            return Cost.unknown()
        return Cost.of(sum((r.cost.known_credits for r in self.results), Decimal("0")))

    def explain(self) -> str:
        rate = "n/a" if self.success_rate is None else f"{self.success_rate:.0%}"
        lines = [
            f"paid canary: {self.status.value}",
            f"validated {self.validated}/{len(self.scored)} scored canaries ({rate})",
            f"thresholds: pass >= {self.pass_rate_threshold:.0%}, "
            f"block < {self.block_rate_threshold:.0%}",
            f"spent: {self.spent}",
        ]
        if self.detail:
            lines.append(self.detail)
        neutral = [r for r in self.results if r.is_neutral]
        if neutral:
            lines.append(
                f"{len(neutral)} canary URLs excluded as neutral (dead URL / origin down): "
                "not evidence about the provider"
            )
        for result in self.results:
            mark = "ok  " if result.succeeded else "FAIL"
            lines.append(f"  {mark} {result.url} — {result.reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "allows_batch": self.status.allows_batch,
            "validated": self.validated,
            "scored": len(self.scored),
            "success_rate": self.success_rate,
            "spent": self.spent.to_dict(),
            "detail": self.detail,
            "results": [r.to_dict() for r in self.results],
            "explanation": self.explain(),
        }


def select_canary_urls(
    unresolved: Sequence[str],
    *,
    size: int = DEFAULT_CANARY_SIZE,
    rng: random.Random | None = None,
) -> list[str]:
    """Pick a representative handful, deterministically when given a seeded rng.

    Sampling rather than taking the first N: the head of a queue is often all one
    domain or all one url_class, and a canary that only tests the easy corner of
    the batch is worse than none — it grants confidence it did not earn.
    """

    bounded = max(MIN_CANARY_SIZE, min(size, MAX_CANARY_SIZE))
    if len(unresolved) <= bounded:
        return list(unresolved)
    # Sampling canaries, not generating secrets: reproducibility matters here,
    # cryptographic strength does not.
    picker = rng or random.Random()  # noqa: S311
    return picker.sample(list(unresolved), bounded)


@dataclass
class PaidCanary:
    """Runs the canary calls and turns them into a go/no-go decision."""

    pass_rate: float = DEFAULT_PASS_RATE
    block_rate: float = DEFAULT_BLOCK_RATE
    size: int = DEFAULT_CANARY_SIZE
    rng: random.Random | None = field(default=None)

    def __post_init__(self) -> None:
        if not 0.0 <= self.block_rate <= self.pass_rate <= 1.0:
            raise ValueError("need 0 <= block_rate <= pass_rate <= 1")

    def run(
        self,
        unresolved: Sequence[tuple[str, str, str, Verdict]],
        *,
        attempt: Any,
    ) -> CanaryOutcome:
        """Spend a few credits to decide whether to spend many.

        ``unresolved`` is (url, domain, url_class, verdict); ``attempt`` is the
        escalator's ``attempt`` callable, so the canary uses the *same* path the
        batch will — including its budget holds and its breakers. A canary that
        took a shortcut would not be testing the thing that is about to run.
        """

        chosen_urls = set(
            select_canary_urls([item[0] for item in unresolved], size=self.size, rng=self.rng)
        )
        results: list[CanaryResult] = []
        for url, domain, url_class, verdict in unresolved:
            if url not in chosen_urls:
                continue
            outcome = attempt(url, verdict=verdict, domain=domain, url_class=url_class)
            results.append(
                CanaryResult(
                    url=url,
                    attempted=bool(outcome.attempted),
                    succeeded=bool(outcome.succeeded),
                    verdict=outcome.triage.verdict if outcome.triage else None,
                    provider=getattr(outcome, "provider", None),
                    cost=getattr(outcome, "cost", Cost.free()),
                    reason=outcome.reason,
                )
            )

        return self._judge(tuple(results))

    def _judge(self, results: tuple[CanaryResult, ...]) -> CanaryOutcome:
        scored = [r for r in results if r.attempted and not r.is_neutral]

        if not results:
            return CanaryOutcome(
                CanaryStatus.PASS,
                results,
                self.pass_rate,
                self.block_rate,
                detail="no canary URLs to test; nothing to veto",
            )
        if not scored:
            # Every canary was refused or neutral. We learned nothing about the
            # provider, and "learned nothing" must not read as "all clear".
            return CanaryOutcome(
                CanaryStatus.WARN,
                results,
                self.pass_rate,
                self.block_rate,
                detail=(
                    "no canary produced a scored outcome; the provider was never "
                    "actually tested, so this is not evidence of health"
                ),
            )

        rate = sum(1 for r in scored if r.succeeded) / len(scored)
        if rate < self.block_rate:
            status, detail = (
                CanaryStatus.BLOCK_PAID_PHASE,
                "sharp degradation: the batch would multiply this failure",
            )
        elif rate < self.pass_rate:
            status, detail = CanaryStatus.WARN, "degraded but working; watch the run"
        else:
            status, detail = CanaryStatus.PASS, ""
        return CanaryOutcome(status, results, self.pass_rate, self.block_rate, detail=detail)
