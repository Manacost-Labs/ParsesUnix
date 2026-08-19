"""Running one corpus through every provider, fairly, under a hard ceiling.

The output of this module is evidence, not a decision. It measures what each
``provider:strategy`` actually did on each ``domain/url_class``, and hands the
numbers to the ranking in :mod:`web_scraper.calibration.metrics` and — only if
an operator approves it — to the router's own statistics.

What makes the comparison honest:

* **One corpus.** Every provider is offered every target. A strategy that could
  not be called records why, as ``INELIGIBLE``, which is a different fact from
  failing and never enters a success rate.
* **One definition of success.** Canonical triage, against the corpus target's
  own :class:`~web_scraper.contracts.ContentRules`. There is no benchmark-local
  idea of "worked".
* **One definition of cost.** :meth:`PricingBook.settle` — the same call the
  production escalator makes. An unreported cost is UNKNOWN here too, and an
  UNKNOWN is excluded from cost-per-result rather than counted as zero.
* **Cheapest first, then stop early.** Once a cheap strategy has proved itself
  on an easy segment there is nothing to learn from paying more there, so the
  expensive modes are skipped — except on the hard segments, which is the only
  place their price could ever be justified.

What it deliberately does not do: decide anything. No route is promoted, no
production statistic is written, no provider verdict changes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from web_scraper.calibration.caps import SpendCaps
from web_scraper.calibration.corpus import Corpus, CorpusTarget, TargetKind
from web_scraper.calibration.store import CalibrationStore
from web_scraper.contracts import Cost, Verdict
from web_scraper.extract import detect_content_kind, extract_response
from web_scraper.providers.base import (
    Provider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderStrategy,
)
from web_scraper.providers.breaker import ProviderBreakers
from web_scraper.providers.pricing import PricingBook
from web_scraper.providers.router import _inappropriate_reason, _strategy_is_appropriate
from web_scraper.providers.stats import ProviderStatsStore, ProviderStrategyKey
from web_scraper.triage import classify_response

#: The failure a target kind stands for, used only to ask the router's own rule
#: whether a strategy's powers could address it. Kinds with no implied failure
#: map to ``None``, which makes every strategy applicable — a plain page is not
#: a puzzle, and any vendor should be able to return it.
IMPLIED_VERDICT: dict[TargetKind, Verdict | None] = {
    TargetKind.HARD_BLOCK: Verdict.BLOCKED,
    TargetKind.CSR_SHELL: Verdict.CSR_REQUIRED,
    TargetKind.SSR_HTML: None,
    TargetKind.JSON_ENDPOINT: None,
    TargetKind.LISTING: None,
    TargetKind.LARGE_HTML: None,
    TargetKind.DEAD_URL: None,
    TargetKind.CROSS_ORIGIN_DATA: None,
}

#: Segments where a cheap success ends the questioning. The hard ones are
#: absent deliberately: an expensive mode exists precisely for those, and never
#: measuring it there would leave its price permanently unjustified.
EARLY_STOP_KINDS = frozenset(
    {
        TargetKind.SSR_HTML,
        TargetKind.LISTING,
        TargetKind.JSON_ENDPOINT,
        TargetKind.LARGE_HTML,
    }
)

#: Consecutive validated successes by a cheaper strategy of the same provider
#: that make a dearer one pointless on that segment.
DEFAULT_EARLY_STOP_SUCCESSES = 3

#: Strategies that ask the vendor to report the page's own network traffic.
CAPTURE_STRATEGIES: dict[str, frozenset[str]] = {
    "zyte": frozenset({"browser_capture"}),
    "zenrows": frozenset({"js", "js_premium"}),
}


@dataclass(frozen=True)
class AttemptOutcome:
    """One cell of the matrix: what this strategy did on this target."""

    provider: str
    strategy: str
    url: str
    domain: str
    url_class: str
    target_kind: str
    recorded_at: float
    #: False when the call did not happen. ``skip_reason`` says why, and this
    #: row is excluded from every rate.
    attempted: bool = False
    eligible: bool = True
    skip_reason: str = ""
    verdict: str | None = None
    #: Triage said OK against the corpus rules. The headline denominator.
    validated: bool = False
    target_status: int | None = None
    provider_status: int | None = None
    #: Did the provider report the status the site actually gives? Measured for
    #: every target, and the whole point of the dead-URL rows.
    status_fidelity: bool | None = None
    latency_ms: int | None = None
    cost_credits: str | None = None
    cost_usd: str | None = None
    cost_certainty: str = "UNKNOWN"
    #: What the session cap was charged. Equals the hold when the cost is
    #: unknown, because an unknown cost is not a free one.
    charged_usd: str = "0"
    content_kind: str | None = None
    content_kind_matches: bool | None = None
    body_bytes: int = 0
    truncated: bool = False
    fields_expected: int = 0
    fields_extracted: int = 0
    discovery_observed: int = 0
    discovery_candidates: int = 0
    discovery_validated: int = 0
    error_kind: str | None = None
    block_signature: str | None = None

    @property
    def scored(self) -> bool:
        """Does this attempt say anything about the strategy?

        Mirrors the production rule: a target that is dead, down or behind an
        auth wall is not evidence about the vendor that faithfully reported it.
        """

        if not self.attempted:
            return False
        if self.error_kind is not None:
            return True
        return self.verdict not in {
            Verdict.DEAD_URL.value,
            Verdict.ORIGIN_DOWN.value,
            Verdict.AUTH_REQUIRED.value,
            Verdict.ACCESS_DENIED.value,
            Verdict.RATE_LIMITED.value,
            Verdict.NOT_MODIFIED.value,
        }

    @property
    def cost_known(self) -> bool:
        return self.cost_certainty in {"EXACT", "PROVISIONAL"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "strategy": self.strategy,
            "url": self.url,
            "domain": self.domain,
            "url_class": self.url_class,
            "target_kind": self.target_kind,
            "recorded_at": self.recorded_at,
            "attempted": self.attempted,
            "eligible": self.eligible,
            "skip_reason": self.skip_reason,
            "verdict": self.verdict,
            "validated": self.validated,
            "scored": self.scored,
            "target_status": self.target_status,
            "provider_status": self.provider_status,
            "status_fidelity": self.status_fidelity,
            "latency_ms": self.latency_ms,
            "cost_credits": self.cost_credits,
            "cost_usd": self.cost_usd,
            "cost_certainty": self.cost_certainty,
            "charged_usd": self.charged_usd,
            "content_kind": self.content_kind,
            "content_kind_matches": self.content_kind_matches,
            "body_bytes": self.body_bytes,
            "truncated": self.truncated,
            "fields_expected": self.fields_expected,
            "fields_extracted": self.fields_extracted,
            "discovery_observed": self.discovery_observed,
            "discovery_candidates": self.discovery_candidates,
            "discovery_validated": self.discovery_validated,
            "error_kind": self.error_kind,
            "block_signature": self.block_signature,
        }


@dataclass(frozen=True)
class PlannedCall:
    """One intended call, before anything is spent."""

    provider: str
    strategy: ProviderStrategy
    target: CorpusTarget
    applicable: bool
    reason: str

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.strategy.id}"


@dataclass
class CalibrationHarness:
    """Runs the matrix. Owns no policy beyond fairness and the ceiling."""

    corpus: Corpus
    providers: Sequence[Provider]
    caps: SpendCaps
    store: CalibrationStore
    session: str
    pricing: PricingBook = field(default_factory=PricingBook)
    breakers: ProviderBreakers | None = None
    #: Skip strategies whose powers cannot address the segment's failure. On by
    #: default: the router would never choose them there, so paying to watch
    #: them fail buys nothing.
    skip_inapplicable: bool = True
    early_stop_successes: int = DEFAULT_EARLY_STOP_SUCCESSES
    #: Ask capture-capable strategies for the page's network traffic. Costs no
    #: extra call — the same fetch carries it.
    capture_discovery: bool = True
    record_stats: bool = True
    clock: Callable[[], float] = time.time
    outcomes: list[AttemptOutcome] = field(default_factory=list)

    # -- planning ----------------------------------------------------------

    def plan(self) -> list[PlannedCall]:
        """The full matrix, ordered so that early stopping can actually save.

        Built before anything runs so the fairness claim can be checked rather
        than asserted: every provider appears against every target.

        The order is ``segment -> provider -> price -> target``, and it is the
        order that makes §"stop early" mean anything. Walking target-first would
        run the expensive mode on the first two pages before the cheap one had
        finished proving itself, so the saving would arrive after the money was
        already spent. Sweeping the cheapest strategy across a whole segment
        first is what turns "the cheap one works here" into calls not made.
        """

        planned: list[PlannedCall] = []
        for provider in self.providers:
            for strategy in sorted(provider.strategies(), key=lambda s: s.nominal_cost):
                for target in self.corpus.targets:
                    verdict = IMPLIED_VERDICT.get(target.kind)
                    applicable = _strategy_is_appropriate(strategy, verdict)
                    planned.append(
                        PlannedCall(
                            provider=provider.name,
                            strategy=strategy,
                            target=target,
                            applicable=applicable,
                            reason=("" if applicable else _inappropriate_reason(strategy, verdict)),
                        )
                    )
        planned.sort(key=lambda c: (c.target.kind.value, c.provider, c.strategy.nominal_cost))
        return planned

    def fairness(self) -> dict[str, Any]:
        """Proof that no provider was handed an easier corpus."""

        offered: dict[str, set[str]] = {}
        for call in self.plan():
            offered.setdefault(call.provider, set()).add(call.target.url)
        sizes = {name: len(urls) for name, urls in offered.items()}
        all_urls = {t.url for t in self.corpus.targets}
        return {
            "targets_offered": sizes,
            "identical_corpus": all(urls == all_urls for urls in offered.values()),
            "corpus_size": len(all_urls),
        }

    # -- running -----------------------------------------------------------

    def run(self) -> list[AttemptOutcome]:
        """Execute the matrix, in target order, cheapest strategy first."""

        # (provider, kind) -> consecutive validated successes by a cheaper mode.
        proven: dict[tuple[str, str], int] = {}
        for call in self.plan():
            outcome = self._run_one(call, proven)
            self.outcomes.append(outcome)
            self.store.record(self.session, outcome)
            if outcome.validated:
                key = (call.provider, call.target.kind.value)
                proven[key] = proven.get(key, 0) + 1
        return self.outcomes

    def _row(self, call: PlannedCall, **fields: Any) -> AttemptOutcome:
        """Every outcome carries the same identity; only the findings differ."""

        return AttemptOutcome(
            provider=call.provider,
            strategy=call.strategy.id,
            url=call.target.url,
            domain=call.target.domain,
            url_class=call.target.url_class,
            target_kind=call.target.kind.value,
            recorded_at=self.clock(),
            **fields,
        )

    def _run_one(self, call: PlannedCall, proven: dict[tuple[str, str], int]) -> AttemptOutcome:
        target = call.target

        if self.skip_inapplicable and not call.applicable:
            return self._row(call, eligible=False, skip_reason=f"INELIGIBLE: {call.reason}")

        if self.breakers is not None and self.breakers.is_open(call.provider, call.strategy.id):
            return self._row(call, eligible=False, skip_reason="INELIGIBLE: circuit breaker open")

        cheaper_proved = proven.get((call.provider, target.kind.value), 0)
        if (
            target.kind in EARLY_STOP_KINDS
            and cheaper_proved >= self.early_stop_successes
            and call.strategy.nominal_cost > _cheapest_cost(self._provider(call.provider))
        ):
            return self._row(
                call,
                eligible=True,
                skip_reason=(
                    f"early stop: a cheaper {call.provider} strategy already validated "
                    f"{cheaper_proved} {target.kind.value} targets"
                ),
            )

        bound = self.pricing.upper_bound_usd(call.provider, call.strategy.id)
        decision = self.caps.admit(call.provider, bound)
        if not decision.allowed:
            return self._row(call, eligible=False, skip_reason=decision.reason)

        return self._call(call, hold_usd=decision.hold_usd)

    def _call(self, call: PlannedCall, *, hold_usd: Decimal) -> AttemptOutcome:
        target = call.target
        provider = self._provider(call.provider)
        request = ProviderRequest(url=target.url, strategy_id=call.strategy.id)
        key = ProviderStrategyKey(
            provider=call.provider,
            strategy_id=call.strategy.id,
            domain=target.domain,
            url_class=target.url_class,
        )
        captured: list[dict[str, Any]] = []

        try:
            if self.capture_discovery and call.strategy.id in CAPTURE_STRATEGIES.get(
                call.provider, frozenset()
            ):
                response, captured = provider.fetch_with_capture(request)  # type: ignore[attr-defined]
            else:
                response = provider.fetch(request)
        except ProviderError as exc:
            # The vendor refused or broke. We were not told whether it billed,
            # so the hold stands: an unknown charge is never released to zero.
            charged = self.caps.commit(call.provider, hold_usd=hold_usd, settled_usd=None)
            if self.breakers is not None:
                self.breakers.record_error(call.provider, call.strategy.id, exc.kind)
            if self.record_stats:
                self.store.stats.record(key, provider_error=True, cost=Cost.unknown())
            return self._row(
                call,
                attempted=True,
                error_kind=exc.kind.value,
                skip_reason=str(exc.message),
                charged_usd=str(charged),
                cost_certainty="UNKNOWN",
            )

        reported = response.cost.credits if response.cost.attributed else None
        cost = self.pricing.settle(
            call.provider,
            call.strategy.id,
            reported,
            reported_usd=response.cost.usd if response.cost.attributed else None,
        )
        charged = self.caps.commit(
            call.provider,
            hold_usd=hold_usd,
            settled_usd=cost.estimated_usd if cost.is_known else None,
        )

        triage = classify_response(
            status=response.target_status,
            body=response.body,
            headers=response.headers,
            rules=target.rules(),
        )
        if self.breakers is not None:
            self.breakers.record_verdict(call.provider, call.strategy.id, triage.verdict)
        if self.record_stats:
            self.store.stats.record(
                key, verdict=triage.verdict, cost=cost, latency_ms=response.latency_ms
            )

        kind = detect_content_kind(response.body, response.headers)
        fields = self._extract(target, response)
        discovery = self._discovery(target, captured)

        return self._row(
            call,
            attempted=True,
            verdict=triage.verdict.value,
            validated=triage.verdict is Verdict.OK,
            target_status=response.target_status,
            provider_status=response.provider_status,
            status_fidelity=response.target_status == target.expected_target_status,
            latency_ms=response.latency_ms,
            cost_credits=None if cost.credits is None else str(cost.credits),
            cost_usd=None if cost.estimated_usd is None else str(cost.estimated_usd),
            cost_certainty=cost.certainty.value,
            charged_usd=str(charged),
            content_kind=kind.value,
            content_kind_matches=kind is target.expected_content_kind,
            body_bytes=len(response.body),
            truncated=response.truncated,
            fields_expected=len(target.critical_fields),
            fields_extracted=fields,
            block_signature=triage.block_signature,
            **discovery,
        )

    # -- side measurements -------------------------------------------------

    def _extract(self, target: CorpusTarget, response: ProviderResponse) -> int:
        """How many declared fields this body actually yields.

        Reported next to the verdict, never folded into it. A provider that
        delivered the document did its job; a selector that missed is our
        profile's problem, and charging it to the vendor would rank vendors on
        the quality of our own extractors.
        """

        if not target.critical_fields:
            return 0
        try:
            result, _ = extract_response(
                response.body,
                headers=response.headers,
                extractors=[{"kind": "json_ld"}, {"kind": "meta"}, {"kind": "heuristic"}],
                fields=list(target.critical_fields),
                base_url=response.final_url or target.url,
            )
        except Exception:  # noqa: BLE001 - extraction quality must never end a run
            return 0
        return sum(1 for name in target.critical_fields if result.data.get(name))

    def _discovery(
        self, target: CorpusTarget, captured: Iterable[dict[str, Any]]
    ) -> dict[str, int]:
        """Judge vendor-captured traffic with the same collector a browser uses."""

        entries = list(captured)
        if not entries:
            return {
                "discovery_observed": 0,
                "discovery_candidates": 0,
                "discovery_validated": 0,
            }
        from web_scraper.discovery import DiscoveryCollector, observed_from_mapping

        collector = DiscoveryCollector(min_pages=1)
        for entry in entries:
            collector.observe(observed_from_mapping(entry, page_url=target.url))
        candidates = collector.candidates()
        return {
            "discovery_observed": len(entries),
            "discovery_candidates": len(candidates),
            "discovery_validated": sum(1 for c in candidates if c.verdict.is_usable),
        }

    def _provider(self, name: str) -> Provider:
        for provider in self.providers:
            if provider.name == name:
                return provider
        raise KeyError(name)  # pragma: no cover - names come from this same list


def _cheapest_cost(provider: Provider) -> Decimal:
    return min(s.nominal_cost for s in provider.strategies())


def calibration_stats_view(store: CalibrationStore) -> ProviderStatsStore:
    """The evidence this session gathered, in the router's own schema."""

    return store.stats


__all__ = [
    "CAPTURE_STRATEGIES",
    "EARLY_STOP_KINDS",
    "IMPLIED_VERDICT",
    "AttemptOutcome",
    "CalibrationHarness",
    "PlannedCall",
    "calibration_stats_view",
]
