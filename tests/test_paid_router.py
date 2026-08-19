"""Choosing a paid strategy: cheapest that clears the bar, and always explainable."""

from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.budget import BudgetExceeded, BudgetLedger
from web_scraper.contracts import Verdict
from web_scraper.providers.router import PaidProviderRouter
from web_scraper.providers.scrape_do import NORMAL, RENDER, STRATEGIES, SUPER, SUPER_RENDER
from web_scraper.routing import RouteKey, RouteStatsStore

DOMAIN, URL_CLASS, PROVIDER = "x.example", "page", "scrape.do"


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.stats = RouteStatsStore(Path(self.tempdir.name) / "r.sqlite3", now=lambda: 1.0)

    def router(self, **kwargs: object) -> PaidProviderRouter:
        kwargs.setdefault("_rng", lambda: 1.0)  # no shadow probe unless asked
        return PaidProviderRouter(stats=self.stats, **kwargs)  # type: ignore[arg-type]

    def feed(self, strategy, *, successes: int, failures: int) -> None:
        level = "L4" if strategy.premium_network else "L3"
        key = RouteKey(DOMAIN, URL_CLASS, f"{PROVIDER}:{strategy.id}", level)
        for _ in range(successes):
            self.stats.record(key, verdict=Verdict.OK)
        for _ in range(failures):
            self.stats.record(key, verdict=Verdict.BLOCKED)

    def choose(self, **kwargs: object):
        return self.router(**kwargs.pop("router_kwargs", {})).choose(  # type: ignore[arg-type]
            STRATEGIES, provider=PROVIDER, domain=DOMAIN, url_class=URL_CLASS, **kwargs
        )

    def test_with_no_history_the_cheapest_strategy_is_tried_first(self) -> None:
        # Cold start must converge downward, not reach for the strongest tool.
        decision = self.choose(verdict=Verdict.BLOCKED)
        self.assertEqual(decision.strategy_id, "normal")
        self.assertEqual(decision.estimated_cost, Decimal("1"))

    def test_a_proven_cheap_strategy_is_preferred_over_an_expensive_one(self) -> None:
        self.feed(NORMAL, successes=20, failures=0)
        self.feed(SUPER, successes=20, failures=0)
        self.assertEqual(self.choose(verdict=Verdict.BLOCKED).strategy_id, "normal")

    def test_a_cheap_strategy_that_fails_here_is_skipped(self) -> None:
        self.feed(NORMAL, successes=0, failures=20)
        self.feed(SUPER, successes=20, failures=0)
        decision = self.choose(verdict=Verdict.BLOCKED)
        self.assertEqual(decision.strategy_id, "super")
        self.assertEqual(decision.estimated_cost, Decimal("10"))

    def test_strategies_are_judged_separately_not_as_one_provider(self) -> None:
        # `normal` failing says nothing about whether `super` would work.
        self.feed(NORMAL, successes=0, failures=20)
        assessments = {
            a.strategy.id: a
            for a in self.router().assess(
                STRATEGIES,
                provider=PROVIDER,
                domain=DOMAIN,
                url_class=URL_CLASS,
                verdict=Verdict.BLOCKED,
            )
        }
        self.assertFalse(assessments["normal"].meets_target)
        self.assertTrue(assessments["super"].meets_target)  # untried, not condemned

    def test_expensive_is_not_a_synonym_for_reliable(self) -> None:
        # Every strategy is proven bad here: nothing is selected rather than
        # defaulting to the priciest one.
        for strategy in (NORMAL, RENDER, SUPER, SUPER_RENDER):
            self.feed(strategy, successes=0, failures=20)
        decision = self.choose(verdict=Verdict.BLOCKED)
        self.assertIsNone(decision.strategy_id)
        self.assertFalse(decision.chosen)
        self.assertIn("no strategy clears the target", decision.explain())

    def test_a_higher_target_can_reject_a_marginal_strategy(self) -> None:
        self.feed(NORMAL, successes=17, failures=3)  # ~85%
        lenient = self.router(target=0.5).choose(
            STRATEGIES,
            provider=PROVIDER,
            domain=DOMAIN,
            url_class=URL_CLASS,
            verdict=Verdict.BLOCKED,
        )
        strict = self.router(target=0.99).choose(
            STRATEGIES,
            provider=PROVIDER,
            domain=DOMAIN,
            url_class=URL_CLASS,
            verdict=Verdict.BLOCKED,
        )
        self.assertEqual(lenient.strategy_id, "normal")
        self.assertNotEqual(strict.strategy_id, "normal")


class CapabilityTests(unittest.TestCase):
    """Paying for the wrong capability is how a budget disappears."""

    def router(self) -> PaidProviderRouter:
        return PaidProviderRouter(stats=None, _rng=lambda: 1.0)

    def assess(self, verdict: Verdict) -> dict[str, bool]:
        return {
            a.strategy.id: a.meets_target
            for a in self.router().assess(
                STRATEGIES, provider=PROVIDER, domain=DOMAIN, url_class=URL_CLASS, verdict=verdict
            )
        }

    def test_a_premium_network_is_only_for_proven_blocking(self) -> None:
        for verdict in (Verdict.CSR_REQUIRED, Verdict.PARSE_FAIL, Verdict.ORIGIN_DOWN):
            with self.subTest(verdict=verdict):
                self.assertFalse(self.assess(verdict)["super"])
        self.assertTrue(self.assess(Verdict.BLOCKED)["super"])

    def test_rendering_is_not_offered_for_a_dead_or_failing_origin(self) -> None:
        # Measured: a 404 through the provider still costs a credit, and
        # rendering it costs five.
        for verdict in (Verdict.DEAD_URL, Verdict.ORIGIN_DOWN, Verdict.PARSE_FAIL):
            with self.subTest(verdict=verdict):
                self.assertFalse(self.assess(verdict)["render"])

    def test_rendering_is_offered_for_a_client_rendered_page(self) -> None:
        self.assertTrue(self.assess(Verdict.CSR_REQUIRED)["render"])

    def test_the_reason_names_the_mismatch(self) -> None:
        assessments = {
            a.strategy.id: a
            for a in self.router().assess(
                STRATEGIES,
                provider=PROVIDER,
                domain=DOMAIN,
                url_class=URL_CLASS,
                verdict=Verdict.DEAD_URL,
            )
        }
        self.assertIn("DEAD_URL", assessments["render"].reason)


class ShadowProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.stats = RouteStatsStore(Path(self.tempdir.name) / "r.sqlite3", now=lambda: 1.0)
        for strategy, ok, bad in ((NORMAL, 0, 20), (SUPER, 20, 0)):
            level = "L4" if strategy.premium_network else "L3"
            key = RouteKey(DOMAIN, URL_CLASS, f"{PROVIDER}:{strategy.id}", level)
            for _ in range(ok):
                self.stats.record(key, verdict=Verdict.OK)
            for _ in range(bad):
                self.stats.record(key, verdict=Verdict.BLOCKED)

    def test_a_probe_re_tests_the_cheaper_strategy(self) -> None:
        router = PaidProviderRouter(stats=self.stats, shadow_probe_rate=1.0, _rng=lambda: 0.0)
        decision = router.choose(
            STRATEGIES,
            provider=PROVIDER,
            domain=DOMAIN,
            url_class=URL_CLASS,
            verdict=Verdict.BLOCKED,
        )
        self.assertEqual(decision.strategy_id, "normal")
        self.assertTrue(decision.shadow_probe)

    def test_without_a_probe_the_proven_strategy_is_used(self) -> None:
        router = PaidProviderRouter(stats=self.stats, _rng=lambda: 1.0)
        decision = router.choose(
            STRATEGIES,
            provider=PROVIDER,
            domain=DOMAIN,
            url_class=URL_CLASS,
            verdict=Verdict.BLOCKED,
        )
        self.assertEqual(decision.strategy_id, "super")
        self.assertFalse(decision.shadow_probe)


class BudgetReservationTests(unittest.TestCase):
    """The window between 'we checked' and 'the provider charged us'."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.ledger = BudgetLedger(Path(self.tempdir.name) / "b.sqlite3", daily_credit_limit="10")

    def test_a_hold_counts_against_the_limit_immediately(self) -> None:
        self.ledger.reserve(provider=PROVIDER, credits=6)
        self.assertEqual(self.ledger.held_credits(), Decimal("6"))
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve(provider=PROVIDER, credits=6)

    def test_settling_records_what_was_actually_charged(self) -> None:
        # The estimate was 5; the provider billed 1. The ledger keeps the fact.
        reservation = self.ledger.reserve(provider=PROVIDER, credits=5)
        self.ledger.settle(reservation, actual_credits=1)
        self.assertEqual(self.ledger.usage().credits, Decimal("1"))
        self.assertEqual(self.ledger.held_credits(), Decimal("0"))

    def test_a_call_that_never_happened_is_released(self) -> None:
        reservation = self.ledger.reserve(provider=PROVIDER, credits=5)
        self.ledger.release(reservation)
        self.assertEqual(self.ledger.held_credits(), Decimal("0"))
        self.assertEqual(self.ledger.usage().credits, Decimal("0"))

    def test_concurrent_workers_cannot_overspend(self) -> None:
        import threading

        granted: list[object] = []
        barrier = threading.Barrier(20)

        def worker() -> None:
            barrier.wait()  # maximise contention
            try:
                reservation = self.ledger.reserve(provider=PROVIDER, credits=1)
            except BudgetExceeded:
                return
            granted.append(reservation)
            self.ledger.settle(reservation, actual_credits=1)  # type: ignore[arg-type]

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(granted), 10, "exactly the budget, no more")
        self.assertEqual(self.ledger.usage().credits, Decimal("10"))
        self.assertEqual(self.ledger.held_credits(), Decimal("0"))


if __name__ == "__main__":
    unittest.main()


class ReliabilityTargetSemanticsTests(unittest.TestCase):
    """The target is a LOWER bound, which is not the same as a success rate."""

    def test_the_default_is_reachable_with_a_realistic_sample(self) -> None:
        from web_scraper.providers.router import DEFAULT_RELIABILITY_TARGET
        from web_scraper.routing.stats import wilson_lower_bound

        # A perfect record of this size must clear the default, or the router
        # refuses everything and the setting is decorative.
        self.assertGreaterEqual(wilson_lower_bound(20, 20), DEFAULT_RELIABILITY_TARGET)

    def test_a_target_of_0_95_needs_far_more_evidence_than_it_looks(self) -> None:
        # Documented so nobody sets 0.95 expecting "95% success".
        from web_scraper.routing.stats import wilson_lower_bound

        self.assertLess(wilson_lower_bound(20, 20), 0.95)
        self.assertGreaterEqual(wilson_lower_bound(70, 70), 0.94)

    def test_an_invalid_target_is_rejected(self) -> None:
        for bad in (0.0, -1.0, 1.5):
            with self.assertRaises(ValueError):
                PaidProviderRouter(stats=None, target=bad)
