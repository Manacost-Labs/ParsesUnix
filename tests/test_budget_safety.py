"""What the ledger must guarantee about money.

The guarantee is deliberately narrow, because a wider one would be a lie: an
external provider can bill more than we estimated after the request has left.
What is guaranteed is that no paid call starts without a sufficient hold, that
real spend is recorded truthfully, that unaccountable spend stops further
spending, and that a crash never leads to paying blindly twice.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.budget import BudgetExceeded, BudgetLedger
from web_scraper.budget_state import BudgetState, ReservationState

PROVIDER = "scrape.do"


class LedgerCase(unittest.TestCase):
    def ledger(self, limit: str | None = "100") -> BudgetLedger:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return BudgetLedger(Path(tempdir.name) / "b.sqlite3", daily_credit_limit=limit)

    def reopen(self, ledger: BudgetLedger, limit: str | None = "100") -> BudgetLedger:
        """A fresh process opening the same file."""

        return BudgetLedger(ledger.path, daily_credit_limit=limit)


class ReservationLifecycleTests(LedgerCase):
    def test_a_hold_counts_immediately(self) -> None:
        ledger = self.ledger("10")
        ledger.reserve(provider=PROVIDER, credits=6)
        self.assertEqual(ledger.held_credits(), Decimal("6"))
        with self.assertRaises(BudgetExceeded):
            ledger.reserve(provider=PROVIDER, credits=6)

    def test_actual_below_the_estimate_frees_the_difference(self) -> None:
        ledger = self.ledger("10")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        ledger.settle(reservation, actual_credits=1)
        self.assertEqual(ledger.usage().credits, Decimal("1"))
        self.assertEqual(ledger.held_credits(), Decimal("0"))

    def test_actual_equal_to_the_estimate(self) -> None:
        ledger = self.ledger("10")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        ledger.settle(reservation, actual_credits=5)
        self.assertEqual(ledger.usage().credits, Decimal("5"))
        self.assertEqual(ledger.state(), BudgetState.OK)

    def test_actual_above_the_estimate_is_recorded_and_hard_stops(self) -> None:
        # The limit was breached by the provider after the request left. Hiding
        # that to protect the limit would make the ledger lie.
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=1)
        ledger.mark_submitted(reservation, provider_request_id="req-1")
        ledger.settle(reservation, actual_credits=10)

        self.assertEqual(ledger.usage().credits, Decimal("10"))
        self.assertEqual(ledger.state(), BudgetState.OVERSPENT)
        self.assertTrue(ledger.state().is_incident)
        with self.assertRaises(BudgetExceeded):
            ledger.reserve(provider=PROVIDER, credits=1)

    def test_an_unreported_cost_holds_the_money_and_stops_paid_work(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        ledger.mark_submitted(reservation, provider_request_id="req-2")
        ledger.settle(reservation, actual_credits=None)  # no cost header

        self.assertEqual(ledger.held_credits(), Decimal("5"), "unknown spend keeps holding")
        self.assertEqual(ledger.state(), BudgetState.UNKNOWN_SPEND)
        with self.assertRaises(BudgetExceeded):
            ledger.reserve(provider=PROVIDER, credits=1)

    def test_reconciliation_resolves_unknown_spend(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        ledger.mark_submitted(reservation)
        ledger.settle(reservation, actual_credits=None)
        ledger.reconcile(reservation.reservation_id, actual_credits=2, detail="provider dashboard")

        self.assertEqual(ledger.usage().credits, Decimal("2"))
        self.assertEqual(ledger.held_credits(), Decimal("0"))
        self.assertEqual(ledger.state(), BudgetState.OK)


class IdempotencyTests(LedgerCase):
    def test_settling_twice_does_not_double_charge(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        ledger.settle(reservation, actual_credits=3)
        ledger.settle(reservation, actual_credits=3)
        self.assertEqual(ledger.usage().credits, Decimal("3"))
        self.assertEqual(ledger.usage().requests, 1)

    def test_releasing_twice_is_harmless(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        self.assertTrue(ledger.release(reservation))
        self.assertFalse(ledger.release(reservation))
        self.assertEqual(ledger.held_credits(), Decimal("0"))

    def test_marking_submitted_twice_is_harmless(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        first = ledger.mark_submitted(reservation, provider_request_id="req-a")
        second = ledger.mark_submitted(first, provider_request_id="req-b")
        self.assertEqual(second.state, ReservationState.SUBMITTED)
        events = [e["event"] for e in ledger.events(reservation.reservation_id)]
        self.assertEqual(events.count("submitted"), 1)

    def test_a_submitted_reservation_cannot_be_released(self) -> None:
        # That money may genuinely have been spent.
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        submitted = ledger.mark_submitted(reservation)
        self.assertFalse(ledger.release(submitted))
        self.assertEqual(ledger.held_credits(), Decimal("5"))

    def test_settling_a_released_reservation_changes_nothing(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        ledger.release(reservation)
        ledger.settle(reservation, actual_credits=5)
        self.assertEqual(ledger.usage().credits, Decimal("0"))


class CrashRecoveryTests(LedgerCase):
    def test_a_crash_before_submission_releases_the_hold(self) -> None:
        ledger = self.ledger("100")
        ledger.reserve(provider=PROVIDER, credits=5)

        restarted = self.reopen(ledger)  # the process died; a new one starts
        outcome = restarted.recover_after_crash()

        self.assertEqual(len(outcome["released"]), 1)
        self.assertEqual(outcome["marked_unknown"], [])
        self.assertEqual(restarted.held_credits(), Decimal("0"))
        self.assertEqual(restarted.state(), BudgetState.OK)

    def test_a_crash_after_submission_is_never_released_on_a_guess(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        ledger.mark_submitted(reservation, provider_request_id="req-x")

        restarted = self.reopen(ledger)
        outcome = restarted.recover_after_crash()

        self.assertEqual(outcome["released"], [])
        self.assertEqual(len(outcome["marked_unknown"]), 1)
        self.assertEqual(restarted.held_credits(), Decimal("5"), "the money stays held")
        self.assertEqual(restarted.state(), BudgetState.UNKNOWN_SPEND)

    def test_recovery_does_not_repay_a_settled_call(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        ledger.mark_submitted(reservation)
        ledger.settle(reservation, actual_credits=5)

        restarted = self.reopen(ledger)
        outcome = restarted.recover_after_crash()
        self.assertEqual(outcome["released"], [])
        self.assertEqual(outcome["marked_unknown"], [])
        self.assertEqual(restarted.usage().credits, Decimal("5"))

    def test_recovery_is_idempotent(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        ledger.mark_submitted(reservation)
        restarted = self.reopen(ledger)
        first = restarted.recover_after_crash()
        second = restarted.recover_after_crash()
        self.assertEqual(len(first["marked_unknown"]), 1)
        self.assertEqual(second["marked_unknown"], [], "already resolved")
        self.assertEqual(restarted.held_credits(), Decimal("5"))


class AuditTrailTests(LedgerCase):
    def test_every_transition_leaves_a_trace(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=1, strategy_id="normal")
        ledger.mark_submitted(reservation, provider_request_id="req-1")
        ledger.settle(reservation, actual_credits=10)  # overspend

        events = [e["event"] for e in ledger.events(reservation.reservation_id)]
        self.assertEqual(events, ["created", "submitted", "settled", "overspend"])

    def test_the_trail_records_the_overspend_amounts(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=1)
        ledger.settle(reservation, actual_credits=7)
        detail = next(
            e["detail"]
            for e in ledger.events(reservation.reservation_id)
            if e["event"] == "overspend"
        )
        self.assertIn("held 1", detail)
        self.assertIn("charged 7", detail)

    def test_no_token_or_url_is_stored(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(
            provider=PROVIDER, credits=1, target_hash="sha256:abcd", strategy_id="normal"
        )
        payload = str(reservation.to_dict())
        self.assertIn("sha256:abcd", payload)  # a hash, not the URL
        self.assertNotIn("http", payload)


class ConcurrencyTests(LedgerCase):
    def test_twenty_workers_cannot_overspend(self) -> None:
        ledger = self.ledger("10")
        granted: list[object] = []
        barrier = threading.Barrier(20)

        def worker() -> None:
            barrier.wait()  # maximise contention
            try:
                reservation = ledger.reserve(provider=PROVIDER, credits=1)
            except BudgetExceeded:
                return
            granted.append(reservation)
            ledger.settle(reservation, actual_credits=1)  # type: ignore[arg-type]

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(granted), 10)
        self.assertEqual(ledger.usage().credits, Decimal("10"))
        self.assertEqual(ledger.held_credits(), Decimal("0"))

    def test_concurrent_settles_of_one_reservation_charge_once(self) -> None:
        ledger = self.ledger("100")
        reservation = ledger.reserve(provider=PROVIDER, credits=5)
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            ledger.settle(reservation, actual_credits=5)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(ledger.usage().credits, Decimal("5"))
        self.assertEqual(ledger.usage().requests, 1)


class BudgetStateTests(LedgerCase):
    def test_states_progress_with_use(self) -> None:
        ledger = self.ledger("10")
        self.assertEqual(ledger.state(), BudgetState.OK)
        ledger.settle(ledger.reserve(provider=PROVIDER, credits=8), actual_credits=8)
        self.assertEqual(ledger.state(), BudgetState.WARNING)
        ledger.settle(ledger.reserve(provider=PROVIDER, credits=2), actual_credits=2)
        self.assertEqual(ledger.state(), BudgetState.EXHAUSTED)

    def test_exhaustion_stops_paid_work_but_is_not_an_incident(self) -> None:
        # Free crawling continues; only paid work stops.
        ledger = self.ledger("5")
        ledger.settle(ledger.reserve(provider=PROVIDER, credits=5), actual_credits=5)
        self.assertFalse(ledger.state().allows_paid_work)
        self.assertFalse(ledger.state().is_incident)

    def test_an_incident_needs_a_human(self) -> None:
        for state in (BudgetState.OVERSPENT, BudgetState.UNKNOWN_SPEND):
            self.assertTrue(state.is_incident)
            self.assertFalse(state.allows_paid_work)

    def test_no_limit_means_no_exhaustion(self) -> None:
        ledger = self.ledger(None)
        ledger.settle(ledger.reserve(provider=PROVIDER, credits=1000), actual_credits=1000)
        self.assertEqual(ledger.state(), BudgetState.OK)


class WorstCaseReservationTests(unittest.TestCase):
    def test_a_strategy_reserves_more_than_it_typically_costs(self) -> None:
        from web_scraper.providers.scrape_do import NORMAL, RENDER, SUPER

        for strategy in (NORMAL, RENDER, SUPER):
            with self.subTest(strategy=strategy.id):
                self.assertGreater(strategy.worst_case_cost, strategy.nominal_cost)

    def test_the_hold_is_never_below_the_nominal_cost(self) -> None:
        from decimal import Decimal as D

        from web_scraper.providers.base import ProviderStrategy

        careless = ProviderStrategy(id="x", nominal_cost=D("10"), reservation_cost=D("1"))
        self.assertEqual(careless.worst_case_cost, D("10"))


if __name__ == "__main__":
    unittest.main()
