"""Unknown spend must survive every layer between the provider and the report.

The failure this guards against is not a crash: it is a run that looks cheaper
than it was. Each test below is one place where a zero could have been invented.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.budget import BudgetLedger
from web_scraper.contracts import Attempt, Cost, Level, Result, Verdict
from web_scraper.observability.metrics import RunMetrics
from web_scraper.providers.base import (
    ProviderCost,
    ProviderError,
    ProviderErrorKind,
    ProviderResponse,
)
from web_scraper.providers.escalation import PaidEscalator
from web_scraper.providers.router import PaidProviderRouter
from web_scraper.providers.scrape_do import STRATEGIES

URL = "https://x.example/a"


class CostValueTests(unittest.TestCase):
    def test_the_three_states_are_distinguishable(self) -> None:
        self.assertEqual(Cost.free().credits, Decimal("0"))
        self.assertTrue(Cost.free().is_known)
        self.assertEqual(Cost.of("5").credits, Decimal("5"))
        self.assertTrue(Cost.of("5").is_known)
        self.assertIsNone(Cost.unknown().credits)
        self.assertFalse(Cost.unknown().is_known)

    def test_a_measured_zero_is_not_an_unknown(self) -> None:
        self.assertNotEqual(Cost.free(), Cost.unknown())

    def test_an_unparseable_cost_is_unknown_not_zero(self) -> None:
        for junk in ("", "n/a", None, "abc"):
            with self.subTest(junk=junk):
                self.assertFalse(Cost.of(junk).is_known)
                self.assertIsNone(Cost.of(junk).credits)

    def test_an_attributed_unknown_cannot_be_constructed(self) -> None:
        # Otherwise a caller could build something that reads as a known zero.
        with self.assertRaises(ValueError):
            Cost(credits=None, attributed=True)
        with self.assertRaises(ValueError):
            Cost(credits=Decimal("5"), attributed=False)

    def test_it_survives_a_round_trip(self) -> None:
        for cost in (Cost.free(), Cost.of("2.5"), Cost.unknown()):
            with self.subTest(cost=cost):
                self.assertEqual(Cost.from_dict(cost.to_dict()), cost)

    def test_json_says_null_for_unknown_not_zero(self) -> None:
        self.assertIsNone(Cost.unknown().to_dict()["credits"])
        self.assertFalse(Cost.unknown().to_dict()["attributed"])


class MetricsTests(unittest.TestCase):
    """RunMetrics: the last place a total is formed before a human reads it."""

    def result(self, cost: Cost) -> Result:
        return Result(
            url=URL,
            verdict=Verdict.OK,
            attempts=(
                Attempt(
                    url=URL,
                    level=Level.L3,
                    verdict=Verdict.OK,
                    reason="paid",
                    provider="scrape.do",
                    cost=cost,
                ),
            ),
        )

    def test_an_unknown_cost_does_not_add_zero_to_the_total(self) -> None:
        metrics = RunMetrics()
        metrics.observe(self.result(Cost.unknown()))
        self.assertEqual(metrics.unattributed_costs, 1)
        self.assertEqual(metrics.paid_calls, 1)
        self.assertFalse(metrics.cost_is_complete, "the total is a floor, and says so")

    def test_a_run_with_known_costs_reports_a_complete_total(self) -> None:
        metrics = RunMetrics()
        metrics.observe(self.result(Cost.of("5")))
        metrics.observe(self.result(Cost.of("1")))
        self.assertEqual(metrics.cost_credits, Decimal("6"))
        self.assertTrue(metrics.cost_is_complete)

    def test_one_unknown_call_makes_the_whole_total_incomplete(self) -> None:
        metrics = RunMetrics()
        metrics.observe(self.result(Cost.of("5")))
        metrics.observe(self.result(Cost.unknown()))
        self.assertEqual(metrics.cost_credits, Decimal("5"), "known part still reported")
        self.assertFalse(metrics.cost_is_complete)
        self.assertEqual(metrics.to_dict()["unattributed_costs"], 1)
        self.assertIs(metrics.to_dict()["cost_is_complete"], False)


class EndToEndCostTests(unittest.TestCase):
    """provider -> PaidAttempt -> GatewayOutcome -> Attempt -> RunMetrics."""

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.budget = BudgetLedger(Path(tempdir.name) / "b.sqlite3", daily_credit_limit="100")

    def escalator(self, provider):
        return PaidEscalator(
            provider,
            budget=self.budget,
            router=PaidProviderRouter(stats=None, _rng=lambda: 1.0),
        )

    def provider_returning(self, cost: ProviderCost):
        class P:
            name = "scrape.do"

            def strategies(self):
                return STRATEGIES

            def fetch(self, request):
                return ProviderResponse(
                    provider="scrape.do",
                    strategy_id=request.strategy_id,
                    target_status=200,
                    provider_status=200,
                    body=b"<html><body><article>" + b"w " * 300 + b"</article></body></html>",
                    headers={"Content-Type": "text/html"},
                    cost=cost,
                )

        return P()

    def test_a_silent_provider_yields_unknown_all_the_way_to_the_report(self) -> None:
        paid = self.escalator(self.provider_returning(ProviderCost.unattributed())).attempt(
            URL, verdict=Verdict.BLOCKED, domain="x.example", url_class="page"
        )
        self.assertTrue(paid.attempted)
        self.assertTrue(paid.unknown_spend)
        self.assertIsNone(paid.actual_cost)

        attempt = Attempt(
            url=URL,
            level=Level.L3,
            verdict=Verdict.OK,
            reason=paid.reason,
            provider="scrape.do",
            cost=Cost.unknown() if paid.actual_cost is None else Cost.of(paid.actual_cost),
        )
        self.assertIsNone(attempt.to_dict()["cost"]["credits"])

        metrics = RunMetrics()
        metrics.observe(Result(url=URL, verdict=Verdict.OK, attempts=(attempt,)))
        self.assertFalse(metrics.cost_is_complete)
        self.assertEqual(metrics.cost_credits, Decimal("0"))
        self.assertEqual(metrics.unattributed_costs, 1)

    def test_a_provider_crash_leaves_unknown_spend_not_a_refund(self) -> None:
        class Broken:
            name = "scrape.do"

            def strategies(self):
                return STRATEGIES

            def fetch(self, request):
                raise ProviderError(
                    kind=ProviderErrorKind.TIMEOUT, message="gone", provider="scrape.do"
                )

        paid = self.escalator(Broken()).attempt(
            URL, verdict=Verdict.BLOCKED, domain="x.example", url_class="page"
        )
        self.assertTrue(paid.unknown_spend)
        self.assertGreater(self.budget.held_credits(), Decimal("0"), "money stays held")


if __name__ == "__main__":
    unittest.main()
