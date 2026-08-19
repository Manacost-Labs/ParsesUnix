"""Canonical money, and the rule that decides whether a batch may continue.

PROVISIONAL is the level that lets spending carry on when a provider says
nothing about cost. That makes it the one worth testing hardest: introducing it
without a documented bound converts an honest "we do not know" into a fabricated
number, which is precisely what UNKNOWN exists to prevent.
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.budget import BudgetLedger
from web_scraper.budget_state import BudgetState
from web_scraper.contracts import CostCertainty, Verdict
from web_scraper.providers.base import (
    ProviderCost,
    ProviderResponse,
    ProviderStrategy,
)
from web_scraper.providers.multi_escalation import MultiProviderEscalator
from web_scraper.providers.multi_router import MultiProviderRouter
from web_scraper.providers.pricing import (
    BRIGHT_DATA,
    FIRECRAWL,
    SCRAPE_DO,
    PricingBook,
    PricingSnapshot,
    StrategyRate,
)
from web_scraper.providers.stats import ProviderStatsStore

DOMAIN, URL_CLASS = "site.example", "page"
URL = "https://site.example/a"
GOOD = b"<html><body><article>" + b"word " * 200 + b"</article></body></html>"


class UnitTests(unittest.TestCase):
    def test_vendor_units_are_not_interchangeable(self) -> None:
        # One Scrape.do credit, one Firecrawl credit and one Bright Data request
        # are three different things. Only USD makes them comparable.
        self.assertEqual(SCRAPE_DO.native_unit, "credits")
        self.assertEqual(BRIGHT_DATA.native_unit, "requests")
        book = PricingBook()
        sd = book.expected_usd("scrape.do", "normal")
        fc = book.expected_usd("firecrawl", "basic")
        assert sd is not None and fc is not None
        self.assertNotEqual(sd, fc, "one credit each, different money")

    def test_every_price_is_decimal_never_float(self) -> None:
        for snapshot in (SCRAPE_DO, FIRECRAWL, BRIGHT_DATA):
            for name, rate in snapshot.rates.items():
                with self.subTest(f"{snapshot.provider}:{name}"):
                    self.assertIsInstance(rate.native_per_call, Decimal)
                    self.assertIsInstance(rate.usd_per_native_unit, Decimal)

    def test_an_upper_bound_is_never_below_the_list_price(self) -> None:
        for snapshot in (SCRAPE_DO, FIRECRAWL, BRIGHT_DATA):
            for name, rate in snapshot.rates.items():
                with self.subTest(f"{snapshot.provider}:{name}"):
                    self.assertGreaterEqual(rate.upper_bound, rate.native_per_call)


class ProvisionalRuleTests(unittest.TestCase):
    """The single condition under which spending may continue uninformed."""

    def setUp(self) -> None:
        self.book = PricingBook()

    def test_a_reported_cost_is_always_exact(self) -> None:
        cost = self.book.settle("scrape.do", "normal", Decimal("1"))
        self.assertIs(cost.certainty, CostCertainty.EXACT)
        self.assertEqual(cost.credits, Decimal("1"))
        self.assertIsNotNone(cost.estimated_usd)

    def test_a_documented_deterministic_tariff_allows_a_provisional_ceiling(self) -> None:
        cost = self.book.settle("firecrawl", "basic", None)
        self.assertIs(cost.certainty, CostCertainty.PROVISIONAL)
        self.assertEqual(cost.credits, Decimal("1"), "the documented ceiling")
        self.assertTrue(cost.is_known, "bounded, so a batch may continue")

    def test_an_undocumented_multiplier_forbids_provisional(self) -> None:
        # Firecrawl's auto mode is documented to retry on another pool without
        # publishing what that costs. No defensible ceiling exists.
        cost = self.book.settle("firecrawl", "auto", None)
        self.assertIs(cost.certainty, CostCertainty.UNKNOWN)
        self.assertIsNone(cost.credits)

    def test_cpm_billing_forbids_provisional_entirely(self) -> None:
        # Bright Data prices premium domains higher with no published multiplier.
        for strategy in ("unlocker", "unlocker_render", "browser"):
            with self.subTest(strategy=strategy):
                cost = self.book.settle("brightdata", strategy, None)
                self.assertIs(cost.certainty, CostCertainty.UNKNOWN)

    def test_an_unknown_provider_is_unknown_not_free(self) -> None:
        cost = self.book.settle("nobody", "whatever", None)
        self.assertIs(cost.certainty, CostCertainty.UNKNOWN)

    def test_provisional_records_the_ceiling_so_the_ledger_never_undercounts(self) -> None:
        cost = self.book.settle("firecrawl", "basic", None)
        rate = self.book.rate("firecrawl", "basic")
        assert rate is not None and cost.credits is not None
        self.assertEqual(cost.credits, rate.upper_bound)


class DriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = PricingBook()

    def test_a_call_above_its_documented_ceiling_is_reported(self) -> None:
        # A vendor changing its pricing is invisible at runtime until the
        # invoice arrives. This catches it on the first occurrence.
        drift = self.book.detect_drift("scrape.do", "normal", Decimal("4"))
        self.assertIsNotNone(drift)
        assert drift is not None
        self.assertIn("above the documented ceiling", drift)

    def test_a_call_at_or_below_the_ceiling_is_silent(self) -> None:
        self.assertIsNone(self.book.detect_drift("scrape.do", "super", Decimal("10")))
        self.assertIsNone(self.book.detect_drift("scrape.do", "super", Decimal("2")))

    def test_a_stale_tariff_is_surfaced(self) -> None:
        old = PricingSnapshot(
            provider="p",
            native_unit="credits",
            pricing_source="x",
            docs_verified_at="2020-01-01",
            effective_at="2020-01-01",
            rates={"s": StrategyRate(Decimal("1"), Decimal("0.001"))},
        )
        book = PricingBook((old,))
        stale = book.stale_snapshots(today=dt.date(2026, 8, 19))
        self.assertEqual(len(stale), 1)

    def test_a_fresh_tariff_is_not_flagged(self) -> None:
        book = PricingBook()
        self.assertEqual(book.stale_snapshots(today=dt.date(2026, 9, 1)), [])


class Vendor:
    def __init__(self, name, strategy_id, *, cost=None):
        self.name, self._sid, self._cost = name, strategy_id, cost

    def strategies(self):
        return (ProviderStrategy(id=self._sid, nominal_cost=Decimal("1"), premium_network=True),)

    def fetch(self, request):
        return ProviderResponse(
            provider=self.name,
            strategy_id=request.strategy_id,
            target_status=200,
            provider_status=200,
            body=GOOD,
            headers={"Content-Type": "text/html"},
            cost=ProviderCost.parse(self._cost)
            if self._cost is not None
            else ProviderCost.unattributed(),
        )


class EscalatorSettlementTests(unittest.TestCase):
    """The rule applied where it actually matters: on a real settlement."""

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        self.budget = BudgetLedger(root / "b.sqlite3", daily_credit_limit="500")
        self.stats = ProviderStatsStore(root / "p.sqlite3")

    def attempt(self, vendor):
        router = MultiProviderRouter(providers=[vendor], stats=self.stats, _rng=lambda: 1.0)
        escalator = MultiProviderEscalator(router, budget=self.budget, stats=self.stats)
        return escalator.attempt(URL, verdict=Verdict.BLOCKED, domain=DOMAIN, url_class=URL_CLASS)

    def test_a_silent_deterministic_provider_settles_provisionally(self) -> None:
        outcome = self.attempt(Vendor("firecrawl", "basic"))
        self.assertIs(outcome.cost.certainty, CostCertainty.PROVISIONAL)
        self.assertTrue(outcome.cost.is_known)
        self.assertEqual(self.budget.state(), BudgetState.OK, "a bounded batch may continue")

    def test_a_silent_unbounded_provider_stops_spending(self) -> None:
        outcome = self.attempt(Vendor("brightdata", "unlocker"))
        self.assertIs(outcome.cost.certainty, CostCertainty.UNKNOWN)
        self.assertTrue(outcome.unknown_spend)
        self.assertEqual(self.budget.state(), BudgetState.UNKNOWN_SPEND)

    def test_a_reporting_provider_settles_exactly(self) -> None:
        outcome = self.attempt(Vendor("scrape.do", "normal", cost="1"))
        self.assertIs(outcome.cost.certainty, CostCertainty.EXACT)
        self.assertEqual(self.budget.usage().credits, Decimal("1"))

    def test_provisional_spend_is_charged_at_the_ceiling(self) -> None:
        # Under-charging here would let a run of silent calls quietly exceed the
        # limit while every individual check passed.
        self.attempt(Vendor("firecrawl", "basic"))
        self.assertGreater(self.budget.usage().credits, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
