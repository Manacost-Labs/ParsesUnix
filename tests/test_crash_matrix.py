"""What survives a process death at each point in a paid call.

The claim this file defends is narrow and worth stating exactly, because the
broader version is not true:

    We cannot guarantee that the account is never over-charged — a provider can
    bill for a request that left before we died. What we guarantee is that a
    crash never causes BLIND DOUBLE PAYMENT, and never turns real spend into an
    apparent zero.

Each test kills the escalator at one specific instant by raising from inside the
provider or the ledger, then restarts against the same database and asserts what
the new process sees. Nothing is mocked at the storage layer: the assertions are
about what is actually on disk.
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
from web_scraper.budget_state import BudgetState, ReservationState
from web_scraper.contracts import Verdict
from web_scraper.providers.base import ProviderCost, ProviderResponse, ProviderStrategy
from web_scraper.providers.breaker import ProviderBreakers
from web_scraper.providers.multi_escalation import MultiProviderEscalator
from web_scraper.providers.multi_router import MultiProviderRouter
from web_scraper.providers.stats import ProviderStatsStore

DOMAIN, URL_CLASS = "site.example", "page"
URL = "https://site.example/a"
GOOD = b"<html><body><article>" + b"word " * 200 + b"</article></body></html>"


class SimulatedCrash(BaseException):
    """Not an Exception: nothing in the code under test may catch this.

    A crash is not an error the application handles — it is the process ending.
    Inheriting from BaseException makes the simulation honest, because an
    `except Exception` anywhere in the path would otherwise quietly turn this
    into a graceful failure and the test would prove nothing.
    """


class CrashingVendor:
    name = "vendor"

    def __init__(self, *, crash: bool = False, cost="1"):
        self._crash, self._cost = crash, cost
        self.calls = 0

    def strategies(self):
        return (ProviderStrategy(id="normal", nominal_cost=Decimal("1"), premium_network=True),)

    def fetch(self, request):
        self.calls += 1
        if self._crash:
            # The request has left. The process dies before the answer lands.
            raise SimulatedCrash("process died waiting for the provider")
        return ProviderResponse(
            provider=self.name,
            strategy_id=request.strategy_id,
            target_status=200,
            provider_status=200,
            body=GOOD,
            headers={"Content-Type": "text/html"},
            cost=ProviderCost.parse(self._cost),
        )


class CrashMatrixCase(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)

    def ledger(self) -> BudgetLedger:
        """A fresh handle on the same file — this is what a restart looks like."""

        return BudgetLedger(self.root / "budget.sqlite3", daily_credit_limit="100")

    def escalator(self, vendor, ledger) -> MultiProviderEscalator:
        router = MultiProviderRouter(
            providers=[vendor],
            stats=ProviderStatsStore(self.root / "p.sqlite3"),
            breakers=ProviderBreakers(),
            _rng=lambda: 1.0,
        )
        return MultiProviderEscalator(router, budget=ledger)

    def attempt(self, vendor, ledger):
        return self.escalator(vendor, ledger).attempt(
            URL, verdict=Verdict.BLOCKED, domain=DOMAIN, url_class=URL_CLASS
        )


class BeforeTheCallTests(CrashMatrixCase):
    def test_a_crash_before_any_reservation_leaves_nothing_behind(self) -> None:
        ledger = self.ledger()
        # Nothing was reserved because nothing was attempted.
        restarted = self.ledger()
        self.assertEqual(restarted.open_reservations(), [])
        self.assertEqual(restarted.usage().credits, Decimal("0"))
        self.assertEqual(restarted.state(), BudgetState.OK)
        del ledger

    def test_a_reserved_but_unsent_hold_is_released_on_recovery(self) -> None:
        # The defining safe case: this request never reached the provider, so
        # nobody could have billed for it.
        ledger = self.ledger()
        held = ledger.reserve(provider="vendor", credits=5)
        self.assertEqual(held.state, ReservationState.RESERVED)

        restarted = self.ledger()
        report = restarted.recover_after_crash()
        self.assertEqual(len(report["released"]), 1)
        self.assertEqual(report["marked_unknown"], [])
        self.assertEqual(restarted.held_credits(), Decimal("0"), "the money came back")
        self.assertEqual(restarted.state(), BudgetState.OK, "no incident: nothing was spent")


class DuringTheCallTests(CrashMatrixCase):
    def test_a_crash_after_submission_becomes_unknown_not_released(self) -> None:
        ledger = self.ledger()
        vendor = CrashingVendor(crash=True)
        with self.assertRaises(SimulatedCrash):
            self.attempt(vendor, ledger)
        self.assertEqual(vendor.calls, 1, "the request did leave")

        restarted = self.ledger()
        open_now = restarted.open_reservations()
        self.assertEqual(len(open_now), 1)
        self.assertEqual(
            open_now[0].state,
            ReservationState.SUBMITTED,
            "the ledger says 'this may have been billed', not 'nothing happened'",
        )

        report = restarted.recover_after_crash()
        self.assertEqual(report["released"], [], "releasing would under-count real spend")
        self.assertEqual(len(report["marked_unknown"]), 1)
        self.assertEqual(restarted.state(), BudgetState.UNKNOWN_SPEND)

    def test_unknown_spend_stops_the_next_process_from_spending(self) -> None:
        ledger = self.ledger()
        with self.assertRaises(SimulatedCrash):
            self.attempt(CrashingVendor(crash=True), ledger)

        restarted = self.ledger()
        restarted.recover_after_crash()

        healthy = CrashingVendor(crash=False)
        outcome = self.attempt(healthy, restarted)
        self.assertFalse(outcome.attempted)
        self.assertIn("UNKNOWN_SPEND", outcome.reason)
        self.assertEqual(healthy.calls, 0, "no blind retry of a call that may have billed")

    def test_the_hold_keeps_holding_while_the_spend_is_unresolved(self) -> None:
        ledger = self.ledger()
        with self.assertRaises(SimulatedCrash):
            self.attempt(CrashingVendor(crash=True), ledger)
        restarted = self.ledger()
        restarted.recover_after_crash()
        self.assertGreater(restarted.held_credits(), Decimal("0"))


class AfterTheCallTests(CrashMatrixCase):
    def test_a_crash_between_the_answer_and_settlement_is_still_unknown(self) -> None:
        # The provider answered and told us the cost, but we died before writing
        # it. The next process cannot know that, and must not assume zero.
        ledger = self.ledger()
        held = ledger.reserve(provider="vendor", credits=3)
        ledger.mark_submitted(held)

        restarted = self.ledger()
        report = restarted.recover_after_crash()
        self.assertEqual(len(report["marked_unknown"]), 1)
        self.assertEqual(restarted.state(), BudgetState.UNKNOWN_SPEND)

    def test_a_settled_call_survives_a_restart_intact(self) -> None:
        ledger = self.ledger()
        outcome = self.attempt(CrashingVendor(cost="1"), ledger)
        self.assertTrue(outcome.succeeded)

        restarted = self.ledger()
        self.assertEqual(restarted.usage().credits, Decimal("1"))
        self.assertEqual(restarted.held_credits(), Decimal("0"))
        self.assertEqual(restarted.open_reservations(), [])
        self.assertEqual(restarted.state(), BudgetState.OK)

    def test_recovery_is_idempotent(self) -> None:
        # An operator who runs recovery twice must not double-count anything.
        ledger = self.ledger()
        with self.assertRaises(SimulatedCrash):
            self.attempt(CrashingVendor(crash=True), ledger)

        first = self.ledger()
        first.recover_after_crash()
        usage_after_first = first.usage().credits
        held_after_first = first.held_credits()

        second = self.ledger()
        report = second.recover_after_crash()
        self.assertEqual(report["released"], [])
        self.assertEqual(report["marked_unknown"], [], "already resolved, nothing left to do")
        self.assertEqual(second.usage().credits, usage_after_first)
        self.assertEqual(second.held_credits(), held_after_first)


class ReconciliationTests(CrashMatrixCase):
    def test_a_human_can_resolve_unknown_spend_and_unblock_the_system(self) -> None:
        ledger = self.ledger()
        with self.assertRaises(SimulatedCrash):
            self.attempt(CrashingVendor(crash=True), ledger)

        restarted = self.ledger()
        restarted.recover_after_crash()
        stuck = restarted.open_reservations()
        self.assertEqual(len(stuck), 1)

        # The operator looks the request up in the provider's dashboard.
        restarted.reconcile(
            reservation_id=stuck[0].reservation_id,
            actual_credits=1,
            detail="found in provider dashboard",
        )
        self.assertEqual(restarted.state(), BudgetState.OK)
        self.assertEqual(restarted.usage().credits, Decimal("1"))

        vendor = CrashingVendor(crash=False)
        outcome = self.attempt(vendor, restarted)
        self.assertTrue(outcome.attempted, "spending resumes once the books balance")

    def test_reconciling_to_zero_is_allowed_and_recorded(self) -> None:
        # "I checked; it was never billed" is a legitimate finding, and is
        # different from the system assuming zero on its own.
        ledger = self.ledger()
        with self.assertRaises(SimulatedCrash):
            self.attempt(CrashingVendor(crash=True), ledger)
        restarted = self.ledger()
        restarted.recover_after_crash()
        stuck = restarted.open_reservations()[0]

        restarted.reconcile(
            reservation_id=stuck.reservation_id, actual_credits=0, detail="not billed"
        )
        self.assertEqual(restarted.usage().credits, Decimal("0"))
        self.assertEqual(restarted.state(), BudgetState.OK)
        events = [e["event"] for e in restarted.events(stuck.reservation_id)]
        self.assertIn("submitted", events, "the audit trail still shows it left")


class AuditTrailTests(CrashMatrixCase):
    def test_every_crash_leaves_a_readable_history(self) -> None:
        ledger = self.ledger()
        with self.assertRaises(SimulatedCrash):
            self.attempt(CrashingVendor(crash=True), ledger)
        restarted = self.ledger()
        restarted.recover_after_crash()

        reservation = restarted.open_reservations()[0]
        events = [e["event"] for e in restarted.events(reservation.reservation_id)]
        self.assertEqual(events[:2], ["created", "submitted"])
        self.assertIn("marked_unknown", events)


if __name__ == "__main__":
    unittest.main()
