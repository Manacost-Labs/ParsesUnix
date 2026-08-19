"""Breaker recovery: a breaker that never closes is our own outage.

The tests use an injected clock, so cooldowns are asserted exactly rather than
waited for.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Verdict
from web_scraper.providers.base import ProviderErrorKind
from web_scraper.providers.breaker import (
    DEFAULT_COOLDOWN_SECONDS,
    BreakerState,
    BreakerStore,
    ProviderBreakers,
)

P, S = "scrape.do", "normal"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BreakerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.breakers = ProviderBreakers(threshold=3, clock=self.clock)

    def trip_strategy(self, strategy: str = S) -> None:
        for _ in range(3):
            self.breakers.record_error(P, strategy, ProviderErrorKind.TIMEOUT)


class StateMachineTests(BreakerCase):
    def test_it_opens_only_at_the_threshold(self) -> None:
        for _ in range(2):
            self.breakers.record_error(P, S, ProviderErrorKind.TIMEOUT)
        self.assertFalse(self.breakers.is_open(P, S))
        self.breakers.record_error(P, S, ProviderErrorKind.TIMEOUT)
        self.assertTrue(self.breakers.is_open(P, S))

    def test_an_open_breaker_becomes_half_open_after_the_cooldown(self) -> None:
        self.trip_strategy()
        self.assertEqual(self.breakers.state_of(P, S), BreakerState.OPEN)

        self.clock.advance(DEFAULT_COOLDOWN_SECONDS - 1)
        self.assertEqual(self.breakers.state_of(P, S), BreakerState.OPEN, "not yet")

        self.clock.advance(2)
        self.assertEqual(self.breakers.state_of(P, S), BreakerState.HALF_OPEN)
        self.assertFalse(self.breakers.is_open(P, S), "half-open is not closed for business")

    def test_a_successful_probe_closes_the_breaker(self) -> None:
        self.trip_strategy()
        self.clock.advance(DEFAULT_COOLDOWN_SECONDS)
        admission = self.breakers.admit(P, S)
        self.assertTrue(admission.allowed)
        self.assertTrue(admission.is_probe)

        self.breakers.record_verdict(P, S, Verdict.OK)
        self.assertEqual(self.breakers.state_of(P, S), BreakerState.CLOSED)

    def test_a_failed_probe_reopens_with_a_longer_wait(self) -> None:
        self.trip_strategy()
        first_wait = self.breakers.state()[f"{P}:{S}"]["cooldown_seconds"]

        self.clock.advance(first_wait)
        self.breakers.admit(P, S)
        self.breakers.record_error(P, S, ProviderErrorKind.TIMEOUT)

        self.assertEqual(self.breakers.state_of(P, S), BreakerState.OPEN)
        second_wait = self.breakers.state()[f"{P}:{S}"]["cooldown_seconds"]
        self.assertGreater(second_wait, first_wait, "repeat trips back off")

    def test_the_backoff_is_capped(self) -> None:
        breakers = ProviderBreakers(threshold=1, clock=self.clock, max_cooldown_seconds=300.0)
        for _ in range(20):
            breakers.record_error(P, S, ProviderErrorKind.TIMEOUT)
        self.assertLessEqual(breakers.state()[f"{P}:{S}"]["cooldown_seconds"], 300.0)

    def test_only_one_probe_is_admitted_at_a_time(self) -> None:
        # Otherwise an expiring cooldown releases a paid stampede.
        self.trip_strategy()
        self.clock.advance(DEFAULT_COOLDOWN_SECONDS)
        first = self.breakers.admit(P, S)
        second = self.breakers.admit(P, S)
        self.assertTrue(first.allowed)
        self.assertFalse(second.allowed)
        self.assertIn("already being probed", second.reason)

    def test_an_abandoned_probe_slot_can_be_returned(self) -> None:
        self.trip_strategy()
        self.clock.advance(DEFAULT_COOLDOWN_SECONDS)
        self.breakers.admit(P, S)
        self.breakers.release_probe(P, S)
        self.assertTrue(self.breakers.admit(P, S).allowed, "the slot came back")


class ScopeTests(BreakerCase):
    def test_a_tripped_strategy_leaves_its_siblings_alone(self) -> None:
        self.trip_strategy("normal")
        self.assertTrue(self.breakers.is_open(P, "normal"))
        self.assertFalse(self.breakers.is_open(P, "super"))

    def test_bad_credentials_open_the_whole_provider(self) -> None:
        self.breakers.record_error(P, S, ProviderErrorKind.AUTH)
        self.assertTrue(self.breakers.is_open(P))
        self.assertTrue(self.breakers.is_open(P, "super"), "no strategy survives a bad key")

    def test_bad_credentials_do_not_reopen_on_a_timer(self) -> None:
        self.breakers.record_error(P, S, ProviderErrorKind.AUTH)
        self.clock.advance(86400)
        self.assertTrue(self.breakers.is_open(P), "waiting does not fix a wrong key")
        self.assertIn("needs a human", self.breakers.admit(P, S).reason)

    def test_a_human_can_clear_an_auth_breaker(self) -> None:
        self.breakers.record_error(P, S, ProviderErrorKind.AUTH)
        self.breakers.clear(P)
        self.assertFalse(self.breakers.is_open(P))
        self.assertTrue(self.breakers.admit(P, S).allowed)

    def test_an_exhausted_quota_recovers_on_the_billing_clock(self) -> None:
        breakers = ProviderBreakers(threshold=3, clock=self.clock, quota_cooldown_seconds=3600.0)
        breakers.record_error(P, S, ProviderErrorKind.QUOTA)
        self.assertTrue(breakers.is_open(P))
        self.clock.advance(1800)
        self.assertTrue(breakers.is_open(P), "half an hour is not the billing window")
        self.clock.advance(1801)
        self.assertEqual(breakers.state_of(P), BreakerState.HALF_OPEN)


class NeutralOutcomeTests(BreakerCase):
    def test_an_origin_outage_never_trips_a_strategy(self) -> None:
        for _ in range(10):
            self.breakers.record_verdict(P, S, Verdict.ORIGIN_DOWN)
        self.assertFalse(self.breakers.is_open(P, S))

    def test_a_neutral_verdict_does_not_consume_the_probe(self) -> None:
        # An origin outage is not a trial of the strategy, so the one probe
        # a half-open breaker allows must still be available afterwards.
        self.trip_strategy()
        self.clock.advance(DEFAULT_COOLDOWN_SECONDS)
        self.breakers.admit(P, S)
        self.breakers.record_verdict(P, S, Verdict.DEAD_URL)
        self.assertEqual(self.breakers.state_of(P, S), BreakerState.HALF_OPEN, "still on trial")
        self.assertTrue(self.breakers.admit(P, S).allowed)


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "breakers.sqlite3"
        self.clock = FakeClock()

    def breakers(self) -> ProviderBreakers:
        return ProviderBreakers(threshold=3, clock=self.clock, store=BreakerStore(self.path))

    def test_a_restart_does_not_forget_a_refusing_provider(self) -> None:
        first = self.breakers()
        first.record_error(P, S, ProviderErrorKind.AUTH)
        self.assertTrue(first.is_open(P))

        # A new process, same state directory.
        restarted = self.breakers()
        self.assertTrue(restarted.is_open(P), "restart must not buy the same failure again")
        self.assertIn("needs a human", restarted.admit(P, S).reason)

    def test_a_restart_keeps_the_remaining_cooldown(self) -> None:
        first = self.breakers()
        for _ in range(3):
            first.record_error(P, S, ProviderErrorKind.TIMEOUT)
        self.clock.advance(10)

        restarted = self.breakers()
        self.assertEqual(restarted.state_of(P, S), BreakerState.OPEN)
        self.clock.advance(DEFAULT_COOLDOWN_SECONDS)
        self.assertEqual(restarted.state_of(P, S), BreakerState.HALF_OPEN)

    def test_a_probe_in_flight_at_crash_time_does_not_wedge_the_breaker(self) -> None:
        first = self.breakers()
        for _ in range(3):
            first.record_error(P, S, ProviderErrorKind.TIMEOUT)
        self.clock.advance(DEFAULT_COOLDOWN_SECONDS)
        first.admit(P, S)  # claimed, then the process dies

        restarted = self.breakers()
        self.assertTrue(restarted.admit(P, S).allowed, "the dead probe is over, not eternal")

    def test_a_closed_breaker_stays_closed_across_a_restart(self) -> None:
        first = self.breakers()
        for _ in range(3):
            first.record_error(P, S, ProviderErrorKind.TIMEOUT)
        first.record_success(P, S)

        restarted = self.breakers()
        self.assertFalse(restarted.is_open(P, S))
        self.assertTrue(restarted.admit(P, S).allowed)


if __name__ == "__main__":
    unittest.main()
