"""One paid attempt per URL, chosen across the whole fleet."""

from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.budget import BudgetLedger
from web_scraper.contracts import Cost, Verdict
from web_scraper.providers.base import (
    ProviderCost,
    ProviderError,
    ProviderErrorKind,
    ProviderResponse,
    ProviderStrategy,
)
from web_scraper.providers.breaker import ProviderBreakers
from web_scraper.providers.multi_escalation import MultiProviderEscalator
from web_scraper.providers.multi_router import MultiProviderRouter
from web_scraper.providers.stats import ProviderStatsStore, ProviderStrategyKey

DOMAIN, URL_CLASS = "site.example", "page"
URL = "https://site.example/a"
GOOD = b"<html><body><article>" + b"word " * 200 + b"</article></body></html>"


def strategy(sid, cost):
    return ProviderStrategy(
        id=sid, nominal_cost=Decimal(str(cost)), premium_network=True, renders_javascript=False
    )


class Vendor:
    def __init__(self, name, strategies, *, body=GOOD, cost="1", error=None, target_status=200):
        self.name, self._strategies = name, strategies
        self._body, self._cost, self._error, self._status = body, cost, error, target_status
        self.calls: list[str] = []

    def strategies(self):
        return self._strategies

    def fetch(self, request):
        self.calls.append(request.strategy_id)
        if self._error is not None:
            raise self._error
        return ProviderResponse(
            provider=self.name,
            strategy_id=request.strategy_id,
            target_status=self._status,
            provider_status=200,
            body=self._body,
            headers={"Content-Type": "text/html"},
            cost=ProviderCost.parse(self._cost)
            if self._cost is not None
            else ProviderCost.unattributed(),
        )


class EscalatorCase(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        self.budget = BudgetLedger(root / "b.sqlite3", daily_credit_limit="500")
        self.stats = ProviderStatsStore(root / "p.sqlite3")
        self.breakers = ProviderBreakers(threshold=3)

    def escalator(self, vendors, **kw) -> MultiProviderEscalator:
        router = MultiProviderRouter(
            providers=vendors, stats=self.stats, breakers=self.breakers, _rng=lambda: 1.0, **kw
        )
        return MultiProviderEscalator(
            router, budget=self.budget, stats=self.stats, breakers=self.breakers
        )

    def observe(self, provider, sid, *, ok, fail, cost="1"):
        key = ProviderStrategyKey(
            provider=provider, strategy_id=sid, domain=DOMAIN, url_class=URL_CLASS
        )
        for _ in range(ok):
            self.stats.record(key, verdict=Verdict.OK, cost=Cost.of(cost))
        for _ in range(fail):
            self.stats.record(key, verdict=Verdict.BLOCKED, cost=Cost.of(cost))

    def attempt(self, vendors, verdict=Verdict.BLOCKED, **kw):
        return self.escalator(vendors, **kw).attempt(
            URL, verdict=verdict, domain=DOMAIN, url_class=URL_CLASS
        )


class SingleAttemptTests(EscalatorCase):
    def test_exactly_one_vendor_is_called_per_url(self) -> None:
        # Walking the fleet until something works is how one blocked URL costs
        # sixty credits. The router's first choice is the answer.
        a = Vendor("a", (strategy("normal", "1"),), body=b"<html>Just a moment...</html>")
        b = Vendor("b", (strategy("unlocker", "20"),))
        outcome = self.attempt([a, b])

        self.assertTrue(outcome.attempted)
        self.assertFalse(outcome.succeeded, "the chosen vendor was also blocked")
        self.assertEqual(len(a.calls) + len(b.calls), 1, "no second vendor was tried")

    def test_the_cheapest_trusted_vendor_is_the_one_called(self) -> None:
        cheap = Vendor("cheap", (strategy("normal", "1"),))
        dear = Vendor("dear", (strategy("unlocker", "20"),), cost="20")
        self.observe("cheap", "normal", ok=40, fail=0)
        self.observe("dear", "unlocker", ok=40, fail=0, cost="20")

        outcome = self.attempt([cheap, dear])
        self.assertEqual(outcome.provider, "cheap")
        self.assertEqual(dear.calls, [])

    def test_an_unreliable_cheap_vendor_is_skipped_for_a_dearer_one(self) -> None:
        cheap = Vendor("cheap", (strategy("normal", "1"),))
        dear = Vendor("dear", (strategy("unlocker", "20"),), cost="20")
        self.observe("cheap", "normal", ok=1, fail=30)
        self.observe("dear", "unlocker", ok=40, fail=0, cost="20")

        outcome = self.attempt([cheap, dear])
        self.assertEqual(outcome.provider, "dear")
        self.assertEqual(cheap.calls, [])


class AccountingTests(EscalatorCase):
    def test_the_worst_case_is_held_and_the_real_cost_settled(self) -> None:
        vendor = Vendor("a", (strategy("normal", "1"),), cost="1")
        outcome = self.attempt([vendor])
        self.assertEqual(outcome.reserved, Decimal("2"), "default hold is a margin over nominal")
        self.assertEqual(outcome.cost.credits, Decimal("1"))
        self.assertEqual(self.budget.held_credits(), Decimal("0"))
        self.assertEqual(self.budget.usage().credits, Decimal("1"))

    def test_a_silent_provider_produces_unknown_spend_not_zero(self) -> None:
        vendor = Vendor("a", (strategy("normal", "1"),), cost=None)
        outcome = self.attempt([vendor])
        self.assertTrue(outcome.unknown_spend)
        self.assertFalse(outcome.cost.is_known)
        self.assertIsNone(outcome.cost.credits)

    def test_a_provider_error_keeps_the_money_held(self) -> None:
        vendor = Vendor(
            "a",
            (strategy("normal", "1"),),
            error=ProviderError(kind=ProviderErrorKind.TIMEOUT, message="slow", provider="a"),
        )
        outcome = self.attempt([vendor])
        self.assertTrue(outcome.unknown_spend)
        self.assertGreater(self.budget.held_credits(), Decimal("0"))

    def test_the_outcome_is_learned_for_next_time(self) -> None:
        vendor = Vendor("a", (strategy("normal", "1"),), cost="1")
        self.attempt([vendor])
        record = self.stats.get(
            ProviderStrategyKey(
                provider="a", strategy_id="normal", domain=DOMAIN, url_class=URL_CLASS
            )
        )
        assert record is not None
        self.assertEqual(record.validated_successes, 1)
        self.assertEqual(record.known_cost, Decimal("1"))
        self.assertEqual(record.cost_per_valid_result, Decimal("1"))

    def test_a_provider_error_is_learned_as_a_provider_error(self) -> None:
        vendor = Vendor(
            "a",
            (strategy("normal", "1"),),
            error=ProviderError(kind=ProviderErrorKind.TIMEOUT, message="x", provider="a"),
        )
        self.attempt([vendor])
        record = self.stats.get(
            ProviderStrategyKey(
                provider="a", strategy_id="normal", domain=DOMAIN, url_class=URL_CLASS
            )
        )
        assert record is not None
        self.assertEqual(record.provider_errors, 1)
        self.assertEqual(record.unknown_cost_calls, 1, "unknown, not free")


class RefusalTests(EscalatorCase):
    def test_a_dead_url_never_reaches_any_vendor(self) -> None:
        vendor = Vendor("a", (strategy("normal", "1"),))
        outcome = self.attempt([vendor], verdict=Verdict.DEAD_URL)
        self.assertFalse(outcome.attempted)
        self.assertEqual(vendor.calls, [])

    def test_an_exhausted_budget_stops_the_fleet(self) -> None:
        self.budget.settle(self.budget.reserve(provider="a", credits=500), actual_credits=500)
        vendor = Vendor("a", (strategy("normal", "1"),))
        outcome = self.attempt([vendor])
        self.assertFalse(outcome.attempted)
        self.assertIn("EXHAUSTED", outcome.reason)
        self.assertEqual(vendor.calls, [])

    def test_a_tripped_vendor_is_replaced_not_retried(self) -> None:
        for _ in range(3):
            self.breakers.record_error("cheap", "normal", ProviderErrorKind.TIMEOUT)

        cheap = Vendor("cheap", (strategy("normal", "1"),))
        dear = Vendor("dear", (strategy("unlocker", "20"),), cost="20")
        self.observe("cheap", "normal", ok=40, fail=0)
        self.observe("dear", "unlocker", ok=40, fail=0, cost="20")

        outcome = self.attempt([cheap, dear])
        self.assertEqual(outcome.provider, "dear")
        self.assertEqual(cheap.calls, [])

    def test_bad_credentials_take_that_vendor_out_of_the_fleet(self) -> None:
        self.breakers.record_error("cheap", "normal", ProviderErrorKind.AUTH)
        cheap = Vendor("cheap", (strategy("normal", "1"),))
        dear = Vendor("dear", (strategy("unlocker", "20"),), cost="20")
        self.observe("dear", "unlocker", ok=40, fail=0, cost="20")

        outcome = self.attempt([cheap, dear])
        self.assertEqual(outcome.provider, "dear")


class ValidationTests(EscalatorCase):
    def test_a_vendor_200_carrying_a_challenge_is_not_success(self) -> None:
        vendor = Vendor(
            "a", (strategy("normal", "1"),), body=b"<html><title>Just a moment...</title></html>"
        )
        outcome = self.attempt([vendor])
        self.assertFalse(outcome.succeeded)
        assert outcome.triage is not None
        self.assertEqual(outcome.triage.verdict, Verdict.SOFT_BLOCK)

    def test_a_site_404_through_a_working_vendor_is_a_dead_url(self) -> None:
        vendor = Vendor("a", (strategy("normal", "1"),), target_status=404, body=b"gone")
        outcome = self.attempt([vendor])
        assert outcome.triage is not None
        self.assertEqual(outcome.triage.verdict, Verdict.DEAD_URL)
        self.assertFalse(outcome.succeeded)

    def test_the_decision_explains_itself(self) -> None:
        cheap = Vendor("cheap", (strategy("normal", "1"),))
        dear = Vendor("dear", (strategy("unlocker", "20"),), cost="20")
        self.observe("cheap", "normal", ok=1, fail=30)
        self.observe("dear", "unlocker", ok=40, fail=0, cost="20")

        outcome = self.attempt([cheap, dear])
        assert outcome.decision is not None
        text = outcome.decision.explain()
        self.assertIn("cheap:normal", text)
        self.assertIn("selected: dear:unlocker", text)


if __name__ == "__main__":
    unittest.main()
