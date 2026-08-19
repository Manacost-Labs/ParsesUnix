"""What the calibration harness must get right before anyone trusts its table.

The tests are grouped by the way the exercise could quietly produce a wrong
answer rather than by the module they touch, because the failure modes are what
matter here: a benchmark that is merely buggy prints an error, while a benchmark
that is subtly unfair prints a number and moves money.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.calibration.caps import STOP, SpendCaps
from web_scraper.calibration.corpora import EXAMPLE_CORPUS
from web_scraper.calibration.corpus import Corpus, CorpusTarget, TargetKind, corpus_from_mapping
from web_scraper.calibration.harness import CalibrationHarness
from web_scraper.calibration.metrics import aggregate, rank
from web_scraper.calibration.promote import apply_promotion, plan_promotion
from web_scraper.calibration.report import CalibrationReport
from web_scraper.calibration.store import CalibrationStore
from web_scraper.contracts import ContentKind
from web_scraper.providers.base import (
    ProviderCost,
    ProviderError,
    ProviderErrorKind,
    ProviderResponse,
    ProviderStrategy,
)
from web_scraper.providers.pricing import PricingBook, PricingSnapshot, StrategyRate
from web_scraper.providers.stats import ProviderStatsStore

GOOD = b"<html><body><article>" + b"word " * 400 + b"</article></body></html>"
SHELL = b"<html><body><div id='root'></div><script src='/app.js'></script></body></html>"


def target(
    url: str = "https://site.example/a",
    *,
    kind: TargetKind = TargetKind.SSR_HTML,
    url_class: str = "page",
    status: int = 200,
    min_bytes: int = 500,
) -> CorpusTarget:
    return CorpusTarget(
        url=url,
        domain="site.example",
        url_class=url_class,
        kind=kind,
        expected_target_status=status,
        min_body_bytes=min_bytes,
    )


class FakeProvider:
    """A vendor whose every answer is scripted, including how it bills."""

    def __init__(
        self,
        name: str = "vendor",
        *,
        body: bytes = GOOD,
        target_status: int = 200,
        cost: ProviderCost | None = None,
        raises: ProviderError | None = None,
        strategies: tuple[ProviderStrategy, ...] | None = None,
        capture: list[dict[str, object]] | None = None,
    ) -> None:
        self.name = name
        self._body = body
        self._status = target_status
        self._cost = cost if cost is not None else ProviderCost.parse("1")
        self._raises = raises
        self._capture = capture or []
        self._strategies = strategies or (
            ProviderStrategy(id="cheap", nominal_cost=Decimal("1")),
            ProviderStrategy(id="dear", nominal_cost=Decimal("10"), premium_network=True),
        )
        self.calls: list[tuple[str, str]] = []

    def strategies(self) -> tuple[ProviderStrategy, ...]:
        return self._strategies

    def fetch(self, request):  # type: ignore[no-untyped-def]
        self.calls.append((request.strategy_id, request.url))
        if self._raises is not None:
            raise self._raises
        return ProviderResponse(
            provider=self.name,
            strategy_id=request.strategy_id,
            target_status=self._status,
            provider_status=200,
            body=self._body,
            headers={"Content-Type": "text/html"},
            latency_ms=100,
            cost=self._cost,
        )

    def fetch_with_capture(self, request):  # type: ignore[no-untyped-def]
        return self.fetch(request), list(self._capture)


def priced(provider: str, *, usd: str | None = "0.001") -> PricingBook:
    rate = StrategyRate(Decimal("1"), None if usd is None else Decimal(usd), deterministic=True)
    return PricingBook(
        (
            PricingSnapshot(
                provider=provider,
                native_unit="credits",
                pricing_source="test",
                docs_verified_at="2026-08-19",
                effective_at="2026-08-19",
                rates={"cheap": rate, "dear": rate},
            ),
        )
    )


class CorpusTests(unittest.TestCase):
    def test_a_corpus_refuses_a_url_that_carries_a_credential(self) -> None:
        """Corpus files are committed and printed into reports."""

        with self.assertRaises(ValueError):
            CorpusTarget(
                url="https://site.example/a?api_key=secret",
                domain="site.example",
                url_class="page",
                kind=TargetKind.SSR_HTML,
            )

    def test_the_fingerprint_changes_when_an_expectation_changes(self) -> None:
        """Two reports with different questions must not look comparable."""

        one = Corpus(name="c", targets=(target(),))
        two = Corpus(name="c", targets=(target(min_bytes=99999),))
        self.assertNotEqual(one.fingerprint, two.fingerprint)

    def test_rules_come_from_the_corpus_so_every_vendor_is_judged_alike(self) -> None:
        json_target = CorpusTarget(
            url="https://site.example/api",
            domain="site.example",
            url_class="api",
            kind=TargetKind.JSON_ENDPOINT,
            expected_content_kind=ContentKind.JSON,
            required_json_paths=("a.b",),
        )
        rules = json_target.rules()
        self.assertEqual(rules.required_json_paths, ("a.b",))
        self.assertEqual(rules.expected_content_type, "json")

    def test_a_dead_url_target_expects_no_body(self) -> None:
        """A 404 page is short by nature; a minimum size would fail it as thin."""

        self.assertEqual(target(status=404).rules().min_body_bytes, 0)

    def test_the_shipped_corpus_round_trips_through_its_manifest(self) -> None:
        payload = {
            "name": EXAMPLE_CORPUS.name,
            "targets": [
                {
                    "url": t.url,
                    "domain": t.domain,
                    "url_class": t.url_class,
                    "kind": t.kind.value,
                    "notes": t.notes,
                    "expected": {
                        "content_kind": t.expected_content_kind.value.lower(),
                        "target_status": t.expected_target_status,
                        "min_body_bytes": t.min_body_bytes,
                        "canaries": list(t.canaries),
                        "json_paths": list(t.required_json_paths),
                        "critical_fields": list(t.critical_fields),
                    },
                }
                for t in EXAMPLE_CORPUS.targets
            ],
        }
        self.assertEqual(corpus_from_mapping(payload).fingerprint, EXAMPLE_CORPUS.fingerprint)


class CapTests(unittest.TestCase):
    def test_the_hold_is_the_ceiling_not_the_typical_price(self) -> None:
        caps = SpendCaps(total_usd=Decimal("1"))
        decision = caps.admit("v", Decimal("0.05"))
        self.assertEqual(decision.hold_usd, Decimal("0.05"))

    def test_an_unknown_settlement_stays_charged_at_the_hold(self) -> None:
        """Releasing it would let the session spend money it may already owe."""

        caps = SpendCaps(total_usd=Decimal("1"))
        caps.commit("v", hold_usd=Decimal("0.05"), settled_usd=None)
        self.assertEqual(caps.spent_by("v"), Decimal("0.05"))

    def test_a_strategy_nobody_can_price_is_refused_unattended(self) -> None:
        caps = SpendCaps(total_usd=Decimal("1"))
        self.assertFalse(caps.admit("v", None).allowed)

    def test_an_operator_may_still_fire_an_unpriceable_call_deliberately(self) -> None:
        caps = SpendCaps(total_usd=Decimal("1"), allow_unbounded=True)
        self.assertTrue(caps.admit("v", None).allowed)

    def test_one_provider_hitting_its_cap_does_not_end_the_session(self) -> None:
        caps = SpendCaps(total_usd=Decimal("10"), per_provider_usd={"a": Decimal("0.001")})
        self.assertFalse(caps.admit("a", Decimal("1")).allowed)
        self.assertTrue(caps.admit("b", Decimal("1")).allowed)

    def test_the_session_ceiling_ends_it_for_everyone(self) -> None:
        caps = SpendCaps(total_usd=Decimal("0.10"))
        self.assertFalse(caps.admit("a", Decimal("1")).allowed)
        refusal = caps.admit("b", Decimal("0.01"))
        self.assertFalse(refusal.allowed)
        self.assertIn(STOP, refusal.reason)

    def test_caps_read_from_the_environment_not_from_a_default_in_code(self) -> None:
        caps = SpendCaps.from_env(
            {"MAX_PROVIDER_CALIBRATION_USD": "2.5", "MAX_ZYTE_CALIBRATION_USD": "0.25"}
        )
        self.assertEqual(caps.total_usd, Decimal("2.5"))
        self.assertEqual(caps.per_provider_usd["zyte"], Decimal("0.25"))


class FairnessTests(unittest.TestCase):
    def _harness(self, providers, corpus=None, **kw):  # type: ignore[no-untyped-def]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return CalibrationHarness(
            corpus=corpus or Corpus(name="c", targets=(target(), target("https://site.example/b"))),
            providers=providers,
            caps=SpendCaps(total_usd=Decimal("100")),
            store=CalibrationStore(tmp.name),
            session="s",
            pricing=priced("a") if len(providers) == 1 else PricingBook(()),
            **kw,
        )

    def test_every_provider_is_offered_every_target(self) -> None:
        harness = self._harness([FakeProvider("a"), FakeProvider("b")])
        fairness = harness.fairness()
        self.assertTrue(fairness["identical_corpus"])
        self.assertEqual(set(fairness["targets_offered"].values()), {2})

    def test_an_inapplicable_strategy_is_ineligible_not_a_failure(self) -> None:
        """Skipping is a fact about the tool, not evidence about the vendor."""

        renders_only = FakeProvider(
            "a",
            strategies=(
                ProviderStrategy(id="cheap", nominal_cost=Decimal("1"), renders_javascript=True),
            ),
        )
        harness = self._harness(
            [renders_only],
            corpus=Corpus(name="c", targets=(target(kind=TargetKind.HARD_BLOCK),)),
        )
        outcomes = harness.run()
        self.assertEqual(len(outcomes), 1)
        self.assertFalse(outcomes[0].attempted)
        self.assertFalse(outcomes[0].eligible)
        self.assertIn("INELIGIBLE", outcomes[0].skip_reason)
        metrics = next(iter(aggregate(outcomes).values()))
        self.assertEqual(metrics.attempts, 0)
        self.assertIsNone(metrics.success_rate)

    def test_a_cheap_success_stops_the_expensive_mode_on_an_easy_segment(self) -> None:
        corpus = Corpus(
            name="c",
            targets=tuple(target(f"https://site.example/{i}") for i in range(4)),
        )
        provider = FakeProvider("a")
        harness = self._harness([provider], corpus=corpus, early_stop_successes=2)
        outcomes = harness.run()
        dear = [o for o in outcomes if o.strategy == "dear"]
        self.assertTrue(any("early stop" in o.skip_reason for o in dear))
        self.assertTrue(all(s == "cheap" for s, _ in provider.calls[:4]))

    def test_a_hard_segment_never_early_stops(self) -> None:
        """The dear mode exists for exactly these pages; skipping it there
        would leave its price permanently unjustified."""

        corpus = Corpus(
            name="c",
            targets=tuple(
                target(f"https://site.example/{i}", kind=TargetKind.HARD_BLOCK) for i in range(4)
            ),
        )
        harness = self._harness([FakeProvider("a")], corpus=corpus, early_stop_successes=1)
        dear = [o for o in harness.run() if o.strategy == "dear"]
        self.assertTrue(all(o.attempted for o in dear))


class OutcomeTests(unittest.TestCase):
    def _run(self, provider, corpus=None, pricing=None, caps=None):  # type: ignore[no-untyped-def]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = CalibrationStore(tmp.name)
        harness = CalibrationHarness(
            corpus=corpus or Corpus(name="c", targets=(target(),)),
            providers=[provider],
            caps=caps or SpendCaps(total_usd=Decimal("100")),
            store=store,
            session="s",
            pricing=pricing or priced(provider.name),
        )
        return harness.run(), store

    def test_a_validated_result_is_a_triage_verdict_not_a_status_code(self) -> None:
        outcomes, _ = self._run(FakeProvider("a", body=SHELL))
        self.assertEqual(outcomes[0].target_status, 200)
        self.assertFalse(outcomes[0].validated)

    def test_a_provider_that_misreports_a_dead_url_fails_status_fidelity(self) -> None:
        """The defect that hit three adapters, caught by a corpus row."""

        corpus = Corpus(name="c", targets=(target(status=404),))
        honest, _ = self._run(FakeProvider("a", target_status=404, body=b"gone"), corpus=corpus)
        liar, _ = self._run(FakeProvider("a", target_status=200, body=b"gone"), corpus=corpus)
        self.assertTrue(honest[0].status_fidelity)
        self.assertFalse(liar[0].status_fidelity)

    def test_a_faithfully_reported_dead_url_is_not_held_against_the_vendor(self) -> None:
        corpus = Corpus(name="c", targets=(target(status=404),))
        outcomes, _ = self._run(FakeProvider("a", target_status=404, body=b"gone"), corpus=corpus)
        metrics = next(iter(aggregate(outcomes).values()))
        self.assertEqual(metrics.neutral_outcomes, 1)
        self.assertEqual(metrics.scored_attempts, 0)

    def test_an_unreported_cost_is_never_recorded_as_zero(self) -> None:
        outcomes, _ = self._run(
            FakeProvider("a", cost=ProviderCost.unattributed()),
            pricing=PricingBook(()),
        )
        self.assertEqual(outcomes[0].cost_certainty, "UNKNOWN")
        self.assertIsNone(outcomes[0].cost_usd)

    def test_a_provider_error_is_scored_and_leaves_the_hold_charged(self) -> None:
        caps = SpendCaps(total_usd=Decimal("100"))
        outcomes, _ = self._run(
            FakeProvider(
                "a",
                raises=ProviderError(
                    kind=ProviderErrorKind.PROVIDER_FAULT, message="boom", provider="a"
                ),
            ),
            caps=caps,
        )
        self.assertEqual(outcomes[0].error_kind, "PROVIDER_FAULT")
        self.assertGreater(Decimal(outcomes[0].charged_usd), Decimal("0"))
        metrics = next(iter(aggregate(outcomes).values()))
        self.assertEqual(metrics.provider_errors, 1)
        self.assertEqual(metrics.scored_attempts, 1)

    def test_calibration_never_touches_production_statistics(self) -> None:
        production = tempfile.TemporaryDirectory()
        self.addCleanup(production.cleanup)
        prod_stats = ProviderStatsStore(Path(production.name) / "provider_stats.sqlite3")
        _, store = self._run(FakeProvider("a"))
        self.assertTrue(store.stats.all_stats())
        self.assertEqual(prod_stats.all_stats(), [])

    def test_captured_traffic_is_judged_by_the_same_collector_a_browser_uses(self) -> None:
        provider = FakeProvider(
            "zyte",
            strategies=(ProviderStrategy(id="browser_capture", nominal_cost=Decimal("1")),),
            capture=[
                {
                    "url": "https://site.example/api/list",
                    "method": "GET",
                    "status": 200,
                    "content_type": "application/json",
                    "resource_type": "xhr",
                    "body": b'{"items":[{"id":1}]}',
                    "page_url": "https://site.example/a",
                }
            ],
        )
        book = PricingBook(
            (
                PricingSnapshot(
                    provider="zyte",
                    native_unit="requests",
                    pricing_source="test",
                    docs_verified_at="2026-08-19",
                    effective_at="2026-08-19",
                    rates={
                        "browser_capture": StrategyRate(
                            Decimal("1"), Decimal("0.001"), deterministic=True
                        )
                    },
                ),
            )
        )
        outcomes, _ = self._run(provider, pricing=book)
        self.assertEqual(outcomes[0].discovery_observed, 1)
        self.assertEqual(outcomes[0].discovery_candidates, 1)


class MetricTests(unittest.TestCase):
    def _metrics(self, **kw):  # type: ignore[no-untyped-def]
        from web_scraper.calibration.metrics import StrategyMetrics

        return StrategyMetrics(provider="a", strategy="s", domain="d", url_class="c", **kw)

    def test_the_headline_divides_by_validated_results_not_requests(self) -> None:
        """The whole point: a cheap strategy that half-works is not cheap."""

        cheap = self._metrics(
            attempts=10, scored_attempts=10, validated_successes=5, exact_usd=Decimal("0.010")
        )
        dear = self._metrics(
            attempts=10, scored_attempts=10, validated_successes=10, exact_usd=Decimal("0.015")
        )
        self.assertLess(cheap.usd_per_request or 1, dear.usd_per_request or 0)
        self.assertGreater(cheap.usd_per_validated_result or 0, dear.usd_per_validated_result or 1)

    def test_unattributed_spend_disqualifies_the_ratio_rather_than_shrinking_it(self) -> None:
        metrics = self._metrics(
            attempts=10,
            scored_attempts=10,
            validated_successes=10,
            exact_usd=Decimal("0.010"),
            unknown_cost_calls=1,
        )
        self.assertIsNone(metrics.usd_per_validated_result)
        self.assertIn("unattributed", metrics.cost_unavailable_reason or "")

    def test_one_success_does_not_clear_the_confidence_gate(self) -> None:
        lucky = self._metrics(attempts=1, scored_attempts=1, validated_successes=1)
        self.assertLess(lucky.confidence_bound, 0.7)

    def test_an_unpriced_strategy_sorts_below_every_priced_one(self) -> None:
        priced_metrics = self._metrics(
            attempts=5, scored_attempts=5, validated_successes=5, exact_usd=Decimal("1")
        )
        unpriced = self._metrics(
            attempts=5, scored_attempts=5, validated_successes=5, unknown_cost_calls=5
        )
        unpriced.strategy = "unpriced"
        ordered = rank([unpriced, priced_metrics], minimum_confidence=0.5)
        self.assertEqual(ordered[0].metrics.strategy, "s")

    def test_a_thin_sample_is_reported_as_such_rather_than_ranked(self) -> None:
        thin = self._metrics(attempts=1, scored_attempts=1, validated_successes=1)
        ordered = rank([thin], minimum_confidence=0.5, min_observations=3)
        self.assertFalse(ordered[0].passes_confidence)
        self.assertIn("scored attempt", ordered[0].reason)


class ReportTests(unittest.TestCase):
    def _report(self):  # type: ignore[no-untyped-def]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = CalibrationStore(tmp.name)
        corpus = Corpus(name="c", targets=(target(),))
        harness = CalibrationHarness(
            corpus=corpus,
            providers=[FakeProvider("a")],
            caps=SpendCaps(total_usd=Decimal("10")),
            store=store,
            session="sess",
            pricing=priced("a"),
        )
        outcomes = harness.run()
        return CalibrationReport(
            session="sess",
            corpus=corpus,
            outcomes=outcomes,
            pricing=priced("a"),
            caps={"total_usd": "10"},
            fairness=harness.fairness(),
            live=False,
            minimum_confidence=0.7,
        )

    def test_a_report_states_what_would_have_to_match_to_compare_it(self) -> None:
        repro = self._report().reproducibility()
        for key in ("corpus_fingerprint", "python", "pricing_versions", "workload"):
            self.assertIn(key, repro)

    def test_an_artifact_carries_no_bodies_and_no_headers(self) -> None:
        payload = str(self._report().to_dict())
        self.assertNotIn("<article", payload)
        self.assertNotIn("Content-Type", payload)

    def test_totals_keep_the_three_certainties_apart(self) -> None:
        payload = self._report().to_dict()["totals"]
        for key in ("exact_usd", "provisional_usd", "unknown_cost_calls"):
            self.assertIn(key, payload)

    def test_concentration_is_reported_so_a_single_vendor_is_visible(self) -> None:
        report = self._report()
        self.assertEqual(report.to_dict()["concentration"]["top_provider"], "a")


class PromotionTests(unittest.TestCase):
    def _stores(self):  # type: ignore[no-untyped-def]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        calibration = CalibrationStore(Path(tmp.name) / "cal")
        production = ProviderStatsStore(Path(tmp.name) / "prod.sqlite3")
        corpus = Corpus(name="c", targets=(target(), target("https://site.example/b")))
        CalibrationHarness(
            corpus=corpus,
            providers=[FakeProvider("a")],
            caps=SpendCaps(total_usd=Decimal("10")),
            store=calibration,
            session="s",
            pricing=priced("a"),
        ).run()
        return calibration, production

    def test_a_preview_writes_nothing(self) -> None:
        calibration, production = self._stores()
        plan = plan_promotion(calibration.stats, production)
        self.assertTrue(plan.items)
        self.assertEqual(production.all_stats(), [])
        self.assertIn("nothing has been written", plan.describe())

    def test_the_preview_names_every_key_it_would_change(self) -> None:
        calibration, production = self._stores()
        plan = plan_promotion(calibration.stats, production)
        described = plan.describe()
        for item in plan.items:
            self.assertIn(item.stats.key.domain, described)

    def test_applying_merges_rather_than_replacing(self) -> None:
        calibration, production = self._stores()
        plan = plan_promotion(calibration.stats, production)
        apply_promotion(plan, production)
        first = {s.key.strategy_ref: s.attempts for s in production.all_stats()}
        apply_promotion(plan, production)
        second = {s.key.strategy_ref: s.attempts for s in production.all_stats()}
        for ref, count in first.items():
            self.assertEqual(second[ref], count * 2)

    def test_the_command_itself_writes_nothing_without_an_explicit_yes(self) -> None:
        """The gate that matters is the one on the path an operator uses.

        Testing ``plan_promotion`` alone left the CLI free to apply the plan
        anyway — a mutation removing the ``--yes`` check survived the whole
        suite until this test existed.
        """

        from web_scraper.calibration.cli import main

        calibration, production = self._stores()
        argv = [
            "promote",
            "--state-dir",
            str(calibration.directory),
            "--production-stats",
            str(production.path),
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(argv), 0)
        self.assertEqual(production.all_stats(), [], "a preview must not write")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([*argv, "--yes"]), 0)
        self.assertTrue(production.all_stats(), "--yes must apply it")

    def test_a_key_with_nothing_scored_is_not_imported(self) -> None:
        calibration, production = self._stores()
        plan = plan_promotion(calibration.stats, production, min_scored_attempts=99)
        self.assertEqual(plan.items, ())


if __name__ == "__main__":
    unittest.main()
