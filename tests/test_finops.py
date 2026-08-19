"""Cost planning, canaries and spend analysis."""

from __future__ import annotations

import random
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Cost, Verdict
from web_scraper.finops import (
    CanaryStatus,
    PaidCanary,
    counterfactual_savings,
    detect_anomalies,
    estimate_run_cost,
    select_canary_urls,
    summarise_spend,
)
from web_scraper.finops.estimate import UnresolvedUrl
from web_scraper.providers.base import ProviderStrategy
from web_scraper.providers.multi_router import MultiProviderRouter
from web_scraper.providers.stats import ProviderStatsStore, ProviderStrategyKey

DOMAIN, URL_CLASS = "site.example", "page"


def strategy(sid, cost, hold=None):
    return ProviderStrategy(
        id=sid,
        nominal_cost=Decimal(str(cost)),
        reservation_cost=Decimal(str(hold)) if hold else None,
        premium_network=True,
    )


class FakeProvider:
    def __init__(self, name, strategies):
        self.name, self._strategies = name, strategies

    def strategies(self):
        return self._strategies

    def fetch(self, request):  # pragma: no cover
        raise AssertionError("estimation must never fetch")


class EstimateTests(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.stats = ProviderStatsStore(Path(tempdir.name) / "p.sqlite3")

    def observe(self, provider, sid, *, ok, fail, cost="1"):
        key = ProviderStrategyKey(
            provider=provider, strategy_id=sid, domain=DOMAIN, url_class=URL_CLASS
        )
        for _ in range(ok):
            self.stats.record(key, verdict=Verdict.OK, cost=Cost.of(cost))
        for _ in range(fail):
            self.stats.record(key, verdict=Verdict.BLOCKED, cost=Cost.of(cost))

    def urls(self, n: int):
        return [
            UnresolvedUrl(f"https://site.example/{i}", DOMAIN, URL_CLASS, Verdict.BLOCKED)
            for i in range(n)
        ]

    def router(self, providers):
        return MultiProviderRouter(providers=providers, stats=self.stats, _rng=lambda: 1.0)

    def test_estimating_never_calls_a_provider(self) -> None:
        # FakeProvider.fetch raises; reaching the network would fail the test.
        cheap = FakeProvider("cheap", (strategy("normal", "1", hold="3"),))
        self.observe("cheap", "normal", ok=40, fail=0)
        estimate = estimate_run_cost(self.urls(100), router=self.router([cheap]))
        self.assertEqual(estimate.unresolved_urls, 100)

    def test_reserved_exceeds_expected_and_both_are_reported(self) -> None:
        # Quoting only the expected cost is how a run is approved and then
        # stalls at 60% with its budget consumed by holds.
        cheap = FakeProvider("cheap", (strategy("normal", "1", hold="3"),))
        self.observe("cheap", "normal", ok=40, fail=0)
        estimate = estimate_run_cost(self.urls(10), router=self.router([cheap]))
        self.assertEqual(estimate.expected_cost, Decimal("10"))
        self.assertEqual(estimate.reserved_cost, Decimal("30"))
        self.assertGreater(estimate.reserved_cost, estimate.expected_cost)

    def test_budget_fit_is_judged_on_holds_not_expectations(self) -> None:
        cheap = FakeProvider("cheap", (strategy("normal", "1", hold="3"),))
        self.observe("cheap", "normal", ok=40, fail=0)
        estimate = estimate_run_cost(
            self.urls(10), router=self.router([cheap]), budget_remaining=Decimal("15")
        )
        self.assertFalse(estimate.fits_budget, "expected 10 fits, but 30 of holds does not")
        self.assertIn("DOES NOT FIT", estimate.explain())

    def test_expected_cost_reflects_how_often_a_strategy_works(self) -> None:
        # 90/100 clears the 0.80 bound (Wilson 0.826) but is not free of retries:
        # at a 0.9 success rate each usable page costs 1/0.9 = 1.11 credits.
        imperfect = FakeProvider("imperfect", (strategy("normal", "1", hold="2"),))
        self.observe("imperfect", "normal", ok=90, fail=10)
        estimate = estimate_run_cost(self.urls(10), router=self.router([imperfect]))
        self.assertEqual(estimate.expected_cost, Decimal("11.10"))
        self.assertGreater(estimate.expected_cost, Decimal("10"), "list price would say 10")

    def test_a_strategy_below_the_bound_is_not_priced_it_is_excluded(self) -> None:
        # 25/50 looks like "half the time" but its Wilson bound is 0.366, far
        # below 0.80. It is not a cheaper option; it is not an option.
        flaky = FakeProvider("flaky", (strategy("normal", "1", hold="2"),))
        self.observe("flaky", "normal", ok=25, fail=25)
        estimate = estimate_run_cost(self.urls(10), router=self.router([flaky]))
        self.assertEqual(estimate.expected_cost, Decimal("0"))
        self.assertEqual(estimate.unroutable_urls, 10)

    def test_unroutable_urls_are_counted_and_cost_nothing(self) -> None:
        weak = FakeProvider("weak", (strategy("normal", "1"),))
        self.observe("weak", "normal", ok=0, fail=30)
        estimate = estimate_run_cost(self.urls(10), router=self.router([weak]))
        self.assertEqual(estimate.unroutable_urls, 10)
        self.assertEqual(estimate.expected_cost, Decimal("0"))
        self.assertIn("stay unresolved", estimate.explain())

    def test_cheap_and_expensive_paid_work_land_in_different_phases(self) -> None:
        cheap = FakeProvider("cheap", (strategy("normal", "1", hold="3"),))
        dear = FakeProvider("dear", (strategy("unlocker", "20", hold="40"),))
        self.observe("cheap", "normal", ok=40, fail=0)
        self.observe("dear", "unlocker", ok=40, fail=0, cost="20")

        estimate = estimate_run_cost(self.urls(5), router=self.router([cheap, dear]))
        phases = {p.name: p for p in estimate.phases}
        self.assertEqual(phases["C:cheap-paid"].url_count, 5, "the cheap door is chosen")
        self.assertEqual(phases["D:expensive-paid"].url_count, 0)

    def test_free_phases_are_reported_with_zero_cost(self) -> None:
        cheap = FakeProvider("cheap", (strategy("normal", "1"),))
        estimate = estimate_run_cost(
            [], router=self.router([cheap]), free_url_count=9000, free_retry_count=120
        )
        phases = {p.name: p for p in estimate.phases}
        self.assertEqual(phases["A:free"].url_count, 9000)
        self.assertEqual(phases["A:free"].expected_cost, Decimal("0"))
        self.assertEqual(phases["B:free-retry"].url_count, 120)


class CanarySelectionTests(unittest.TestCase):
    def test_it_samples_rather_than_taking_the_head_of_the_queue(self) -> None:
        # The head is often one domain or one url_class; a canary that only tests
        # the easy corner grants confidence it did not earn.
        urls = [f"https://site/{i}" for i in range(100)]
        picked = select_canary_urls(urls, size=5, rng=random.Random(7))
        self.assertEqual(len(picked), 5)
        self.assertNotEqual(picked, urls[:5])

    def test_the_size_is_bounded_at_both_ends(self) -> None:
        urls = [f"https://site/{i}" for i in range(100)]
        self.assertEqual(len(select_canary_urls(urls, size=1, rng=random.Random(1))), 3)
        self.assertEqual(len(select_canary_urls(urls, size=500, rng=random.Random(1))), 10)

    def test_a_short_queue_is_used_whole(self) -> None:
        urls = ["https://site/a", "https://site/b"]
        self.assertEqual(select_canary_urls(urls, size=5), urls)


class FakeAttempt:
    def __init__(self, *, succeeded=True, verdict=Verdict.OK, attempted=True, cost="1"):
        self.attempted, self._succeeded = attempted, succeeded
        self.triage = type("T", (), {"verdict": verdict})() if verdict else None
        self.cost = Cost.of(cost) if cost is not None else Cost.unknown()
        self.provider = "p"
        self.reason = verdict.value if verdict else "not attempted"

    @property
    def succeeded(self):
        return self._succeeded


class CanaryJudgementTests(unittest.TestCase):
    def canary(self, outcomes):
        queue = [
            (f"https://site/{i}", DOMAIN, URL_CLASS, Verdict.BLOCKED) for i in range(len(outcomes))
        ]
        it = iter(outcomes)

        def attempt(url, *, verdict, domain, url_class):
            return next(it)

        return PaidCanary(size=len(outcomes), rng=random.Random(1)).run(queue, attempt=attempt)

    def test_a_healthy_sample_passes(self) -> None:
        outcome = self.canary([FakeAttempt() for _ in range(5)])
        self.assertEqual(outcome.status, CanaryStatus.PASS)
        self.assertEqual(outcome.spent.credits, Decimal("5"))

    def test_sharp_degradation_blocks_the_batch(self) -> None:
        outcomes = [FakeAttempt(succeeded=False, verdict=Verdict.BLOCKED) for _ in range(4)]
        outcomes.append(FakeAttempt())
        outcome = self.canary(outcomes)
        self.assertEqual(outcome.status, CanaryStatus.BLOCK_PAID_PHASE)
        self.assertFalse(outcome.status.allows_batch)
        self.assertIn("multiply", outcome.explain())

    def test_partial_degradation_warns_without_stopping(self) -> None:
        outcomes = [FakeAttempt() for _ in range(3)]
        outcomes += [FakeAttempt(succeeded=False, verdict=Verdict.BLOCKED) for _ in range(2)]
        outcome = self.canary(outcomes)
        self.assertEqual(outcome.status, CanaryStatus.WARN)
        self.assertTrue(outcome.status.allows_batch)

    def test_an_origin_outage_does_not_veto_a_paid_phase(self) -> None:
        # Blocking the run because someone's origin was down would be an outage
        # of our own making, on top of theirs.
        outcomes = [FakeAttempt(succeeded=False, verdict=Verdict.ORIGIN_DOWN) for _ in range(4)]
        outcomes.append(FakeAttempt())
        outcome = self.canary(outcomes)
        self.assertEqual(outcome.status, CanaryStatus.PASS)
        self.assertEqual(len(outcome.scored), 1, "the neutral four were excluded")
        self.assertIn("excluded as neutral", outcome.explain())

    def test_learning_nothing_is_not_all_clear(self) -> None:
        outcomes = [FakeAttempt(attempted=False, verdict=None) for _ in range(5)]
        outcome = self.canary(outcomes)
        self.assertEqual(outcome.status, CanaryStatus.WARN)
        self.assertIn("never actually tested", outcome.explain())

    def test_an_unknown_canary_cost_makes_the_spend_unknown(self) -> None:
        outcomes = [FakeAttempt(), FakeAttempt(cost=None), FakeAttempt()]
        outcome = self.canary(outcomes)
        self.assertFalse(outcome.spent.is_known)


class SpendAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.stats = ProviderStatsStore(Path(tempdir.name) / "p.sqlite3")

    def record(self, provider, sid, *, ok, fail, cost="1", unknown=0):
        key = ProviderStrategyKey(
            provider=provider, strategy_id=sid, domain=DOMAIN, url_class=URL_CLASS
        )
        for _ in range(ok):
            self.stats.record(key, verdict=Verdict.OK, cost=Cost.of(cost))
        for _ in range(fail):
            self.stats.record(key, verdict=Verdict.BLOCKED, cost=Cost.of(cost))
        for _ in range(unknown):
            self.stats.record(key, verdict=Verdict.OK, cost=Cost.unknown())

    def test_cost_per_valid_result_is_the_headline(self) -> None:
        self.record("scrape_do", "normal", ok=8, fail=2, cost="1")
        report = summarise_spend(self.stats.all_stats(), total_urls=100)
        self.assertEqual(report.known_spend, Decimal("10"))
        self.assertEqual(report.validated_paid_results, 8)
        self.assertEqual(report.cost_per_valid_result, Decimal("1.250"))

    def test_unknown_spend_makes_the_headline_unavailable(self) -> None:
        self.record("scrape_do", "normal", ok=8, fail=2, cost="1", unknown=1)
        report = summarise_spend(self.stats.all_stats(), total_urls=100)
        self.assertFalse(report.spend_is_complete)
        self.assertIsNone(report.cost_per_valid_result)

    def test_savings_are_measured_against_a_named_policy(self) -> None:
        self.record("scrape_do", "normal", ok=90, fail=10, cost="1")
        report = summarise_spend(self.stats.all_stats(), total_urls=1000)
        savings = counterfactual_savings(
            report, policy_name="always brightdata:unlocker", policy_cost_per_call=Decimal("20")
        )
        self.assertEqual(savings.actual_spend, Decimal("100"))
        self.assertEqual(savings.policy_spend, Decimal("2000"))
        self.assertEqual(savings.saved, Decimal("1900"))
        self.assertIn("always brightdata:unlocker", savings.explain())

    def test_savings_are_refused_when_the_real_spend_is_incomplete(self) -> None:
        self.record("scrape_do", "normal", ok=5, fail=0, cost="1", unknown=3)
        report = summarise_spend(self.stats.all_stats(), total_urls=100)
        savings = counterfactual_savings(
            report, policy_name="always dear", policy_cost_per_call=Decimal("20")
        )
        self.assertFalse(savings.complete)
        self.assertIsNone(savings.to_dict()["saved"])
        self.assertIn("would be invented", savings.explain())


class AnomalyTests(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.stats = ProviderStatsStore(Path(tempdir.name) / "p.sqlite3")

    def record(self, provider, sid, *, ok=0, fail=0, cost="1", unknown=0):
        key = ProviderStrategyKey(
            provider=provider, strategy_id=sid, domain=DOMAIN, url_class=URL_CLASS
        )
        for _ in range(ok):
            self.stats.record(key, verdict=Verdict.OK, cost=Cost.of(cost))
        for _ in range(fail):
            self.stats.record(key, verdict=Verdict.BLOCKED, cost=Cost.of(cost))
        for _ in range(unknown):
            self.stats.record(key, verdict=Verdict.OK, cost=Cost.unknown())

    def test_a_quiet_run_raises_nothing(self) -> None:
        self.record("scrape_do", "normal", ok=9, fail=1)
        report = summarise_spend(self.stats.all_stats(), total_urls=1000)
        self.assertEqual(detect_anomalies(report), [])

    def test_unknown_spend_is_critical(self) -> None:
        self.record("scrape_do", "normal", ok=5, unknown=2)
        report = summarise_spend(self.stats.all_stats(), total_urls=1000)
        kinds = {a.kind: a for a in detect_anomalies(report)}
        self.assertIn("unknown_spend", kinds)
        self.assertEqual(kinds["unknown_spend"].severity, "critical")

    def test_a_paid_share_spike_points_at_the_free_layer(self) -> None:
        self.record("scrape_do", "normal", ok=50, fail=10)
        report = summarise_spend(self.stats.all_stats(), total_urls=100)
        kinds = {a.kind for a in detect_anomalies(report)}
        self.assertIn("paid_share_spike", kinds)

    def test_leaning_on_the_expensive_fallback_is_flagged(self) -> None:
        self.record("scrape_do", "normal", ok=5, fail=0)
        self.record("brightdata", "unlocker", ok=5, fail=0, cost="20")
        report = summarise_spend(self.stats.all_stats(), total_urls=1000)
        kinds = {a.kind for a in detect_anomalies(report)}
        self.assertIn("fallback_provider_spike", kinds)

    def test_a_cost_spike_is_measured_against_a_baseline(self) -> None:
        self.record("scrape_do", "normal", ok=2, fail=8, cost="1")
        report = summarise_spend(self.stats.all_stats(), total_urls=1000)
        anomalies = detect_anomalies(report, baseline_cost_per_valid_result=Decimal("1.2"))
        self.assertIn("cost_per_valid_result_spike", {a.kind for a in anomalies})


if __name__ == "__main__":
    unittest.main()
