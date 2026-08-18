from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".agents/skills/web-scraper/scripts"
sys.path.insert(0, str(SCRIPTS))

from budget import BudgetExceeded, BudgetLedger, scrape_do_request_cost


class BudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db = Path(self.tempdir.name) / "budget.sqlite3"

    def test_records_usage_and_enforces_limit(self) -> None:
        ledger = BudgetLedger(self.db, daily_credit_limit="10")
        usage = ledger.record(provider="scrape.do", credits="4", request_id="one", day="2026-08-18")
        self.assertEqual(usage.credits, Decimal("4"))
        with self.assertRaises(BudgetExceeded):
            ledger.record(provider="scrape.do", credits="7", request_id="two", day="2026-08-18")
        self.assertEqual(ledger.usage(day="2026-08-18").credits, Decimal("4"))

    def test_request_id_is_idempotent(self) -> None:
        ledger = BudgetLedger(self.db, daily_credit_limit="10")
        ledger.record(provider="firecrawl", credits="2", request_id="same", day="2026-08-18")
        usage = ledger.record(
            provider="firecrawl", credits="2", request_id="same", day="2026-08-18"
        )
        self.assertEqual(usage.requests, 1)
        self.assertEqual(usage.credits, Decimal("2"))

    def test_reads_authoritative_scrape_do_header_case_insensitively(self) -> None:
        cost = scrape_do_request_cost({"Scrape.do-Request-Cost": "25"})
        self.assertEqual(cost, Decimal("25"))

    def test_replaying_request_id_with_different_amount_is_rejected(self) -> None:
        ledger = BudgetLedger(self.db, daily_credit_limit="100")
        ledger.record(provider="scrape.do", credits="3", request_id="r0", day="2026-08-18")
        with self.assertRaises(ValueError):
            ledger.record(provider="scrape.do", credits="9", request_id="r0", day="2026-08-18")
        self.assertEqual(ledger.usage(day="2026-08-18").credits, Decimal("3"))

    def test_money_limit_is_enforced(self) -> None:
        ledger = BudgetLedger(self.db, daily_credit_limit="1000", daily_money_limit="1.00")
        ledger.record(
            provider="firecrawl", credits="1", money="0.80", request_id="m1", day="2026-08-18"
        )
        with self.assertRaises(BudgetExceeded):
            ledger.record(
                provider="firecrawl", credits="1", money="0.50", request_id="m2", day="2026-08-18"
            )

    def test_many_records_do_not_leak_connections(self) -> None:
        ledger = BudgetLedger(self.db, daily_credit_limit="10000")
        for i in range(50):
            ledger.record(provider="scrape.do", credits="1", request_id=f"id{i}", day="2026-08-18")
        self.assertEqual(ledger.usage(day="2026-08-18").requests, 50)


if __name__ == "__main__":
    unittest.main()
