"""The five real adapters, driven end to end through the machinery that pays.

Everything below uses the production classes — the real escalator, the real
budget ledger on a real SQLite file, the real breakers, the real router — with
only the socket replaced. The point is not to test those classes again; it is to
prove that each *vendor adapter* participates in them correctly, because an
adapter that maps its errors to the wrong kind is invisible until the night a
vendor has an outage and the breaker never trips.

Three questions, asked of all five:

* a process that dies mid-call must not let the next process blindly pay again;
* each vendor's own failures must move its breaker the way that failure
  deserves — credentials need a human, quota needs the billing clock;
* the fleet the estimate prices must be the fleet the run actually uses.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_provider_matrix import VENDORS, FakeHTTP, VendorCase
from web_scraper.budget import BudgetLedger
from web_scraper.budget_state import BudgetState, ReservationState
from web_scraper.contracts import Verdict
from web_scraper.providers.base import ProviderErrorKind
from web_scraper.providers.breaker import (
    DEFAULT_COOLDOWN_SECONDS,
    BreakerState,
    ProviderBreakers,
)
from web_scraper.providers.multi_escalation import MultiProviderEscalator
from web_scraper.providers.multi_router import MultiProviderRouter
from web_scraper.providers.stats import ProviderStatsStore

URL = "https://example.com/a"
DOMAIN, URL_CLASS = "example.com", "page"


class SimulatedCrash(BaseException):
    """Not an Exception: nothing in the code under test may catch this.

    A crash is the process ending, not an error the application handles. An
    `except Exception` anywhere on the path would otherwise turn the simulation
    into a graceful failure and the test would prove nothing.
    """


class CrashingHTTP:
    """Dies the moment the request would leave, after it has been recorded."""

    def __init__(self) -> None:
        self.calls = 0

    def urlopen(self, request, timeout=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise SimulatedCrash("process died with the request in flight")


class FleetCrashSafety(unittest.TestCase):
    """One question, five vendors: can a crash cause a blind second payment?"""

    def _ledger(self, directory: Path) -> BudgetLedger:
        return BudgetLedger(directory / "budget.sqlite3", daily_credit_limit="1000")

    def _escalator(self, case: VendorCase, http, directory: Path) -> MultiProviderEscalator:
        provider = case.build(http)
        router = MultiProviderRouter(
            providers=[provider],
            stats=ProviderStatsStore(directory / "stats.sqlite3"),
            _rng=lambda: 1.0,
        )
        return MultiProviderEscalator(router, budget=self._ledger(directory))

    def test_a_crash_in_flight_leaves_the_spend_unknown_for_every_vendor(self) -> None:
        for case in VENDORS:
            with self.subTest(case.name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                http = CrashingHTTP()
                escalator = self._escalator(case, http, directory)
                with self.assertRaises(SimulatedCrash):
                    escalator.attempt(
                        URL, verdict=Verdict.BLOCKED, domain=DOMAIN, url_class=URL_CLASS
                    )
                self.assertEqual(http.calls, 1, "the request did leave")

                restarted = self._ledger(directory)
                open_now = restarted.open_reservations()
                self.assertEqual(len(open_now), 1)
                self.assertEqual(
                    open_now[0].state,
                    ReservationState.SUBMITTED,
                    f"{case.name}: a crashed call must look like possible spend",
                )

    def test_the_next_process_refuses_to_pay_again_for_every_vendor(self) -> None:
        """The narrow guarantee: not 'never over-charged', but never BLINDLY."""

        for case in VENDORS:
            with self.subTest(case.name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                escalator = self._escalator(case, CrashingHTTP(), directory)
                with self.assertRaises(SimulatedCrash):
                    escalator.attempt(
                        URL, verdict=Verdict.BLOCKED, domain=DOMAIN, url_class=URL_CLASS
                    )

                restarted = self._ledger(directory)
                restarted.recover_after_crash()
                self.assertEqual(restarted.state(), BudgetState.UNKNOWN_SPEND)

                healthy = FakeHTTP(*case.target_ok)
                second = self._escalator(case, healthy, directory)
                outcome = second.attempt(
                    URL, verdict=Verdict.BLOCKED, domain=DOMAIN, url_class=URL_CLASS
                )
                self.assertFalse(outcome.attempted, f"{case.name} paid again after a crash")
                self.assertIn("UNKNOWN_SPEND", outcome.reason)


class FleetBreakerBehaviour(unittest.TestCase):
    """Each vendor's own failures, and what they should do to its breaker."""

    class Clock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    def _run(self, case: VendorCase, script, breakers, directory: Path):  # type: ignore[no-untyped-def]
        provider = case.build(FakeHTTP(*script))
        router = MultiProviderRouter(
            providers=[provider],
            stats=ProviderStatsStore(directory / "stats.sqlite3"),
            breakers=breakers,
            _rng=lambda: 1.0,
        )
        escalator = MultiProviderEscalator(
            router,
            budget=BudgetLedger(directory / "budget.sqlite3", daily_credit_limit="100000"),
            breakers=breakers,
        )
        return escalator.attempt(URL, verdict=Verdict.BLOCKED, domain=DOMAIN, url_class=URL_CLASS)

    def test_bad_credentials_stop_the_whole_vendor_and_need_a_human(self) -> None:
        for case in VENDORS:
            with self.subTest(case.name), tempfile.TemporaryDirectory() as tmp:
                clock = self.Clock()
                breakers = ProviderBreakers(threshold=3, clock=clock)
                self._run(case, case.auth, breakers, Path(tmp))
                self.assertTrue(
                    breakers.is_open(case.name),
                    f"{case.name}: an auth failure must stop the vendor, not one strategy",
                )
                clock.advance(DEFAULT_COOLDOWN_SECONDS * 100)
                self.assertTrue(
                    breakers.is_open(case.name),
                    f"{case.name}: bad credentials must not reopen on a timer",
                )

    def test_an_exhausted_quota_waits_for_the_billing_clock_not_a_retry(self) -> None:
        for case in VENDORS:
            with self.subTest(case.name), tempfile.TemporaryDirectory() as tmp:
                clock = self.Clock()
                breakers = ProviderBreakers(threshold=3, clock=clock)
                self._run(case, case.quota, breakers, Path(tmp))
                self.assertTrue(breakers.is_open(case.name))
                clock.advance(DEFAULT_COOLDOWN_SECONDS + 1)
                self.assertTrue(
                    breakers.is_open(case.name),
                    f"{case.name}: a quota wall outlasts an ordinary cooldown",
                )

    def test_every_vendor_walks_the_whole_half_open_cycle(self) -> None:
        """OPEN -> cooldown -> HALF_OPEN -> exactly one probe -> CLOSED."""

        for case in VENDORS:
            with self.subTest(case.name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                clock = self.Clock()
                breakers = ProviderBreakers(threshold=2, clock=clock)
                # Every strategy, not just one: a provider whose cheap mode is
                # tripped still has dearer ones, and the router would simply
                # route around the broken one — which is correct behaviour and
                # would make this test prove nothing.
                strategies = [s.id for s in case.build(FakeHTTP()).strategies()]
                for strategy in strategies:
                    for _ in range(2):
                        breakers.record_error(case.name, strategy, ProviderErrorKind.PROVIDER_FAULT)
                self.assertEqual(breakers.state_of(case.name, case.strategy), BreakerState.OPEN)

                refused = self._run(case, case.target_ok, breakers, directory)
                self.assertFalse(refused.attempted, "an open breaker is not a candidate")

                clock.advance(DEFAULT_COOLDOWN_SECONDS + 1)
                self.assertEqual(
                    breakers.state_of(case.name, case.strategy), BreakerState.HALF_OPEN
                )

                probe = self._run(case, case.target_ok, breakers, directory)
                self.assertTrue(probe.attempted, f"{case.name}: the probe must be admitted")
                # Which strategy the router sends the probe through is its
                # business — it picks the cheapest half-open one. What matters
                # is that the one it probed came back into service.
                self.assertEqual(
                    breakers.state_of(case.name, probe.strategy_id or case.strategy),
                    BreakerState.CLOSED,
                    f"{case.name}: a successful probe closes the breaker",
                )

    def test_a_faithfully_reported_dead_url_never_trips_a_breaker(self) -> None:
        """The site being gone is not the vendor being broken."""

        for case in VENDORS:
            with self.subTest(case.name), tempfile.TemporaryDirectory() as tmp:
                breakers = ProviderBreakers(threshold=2, clock=self.Clock())
                for _ in range(4):
                    self._run(case, case.target_404, breakers, Path(tmp))
                self.assertFalse(breakers.is_open(case.name, case.strategy))


class FleetParity(unittest.TestCase):
    """The estimate and the run must price the same fleet."""

    def test_the_estimator_and_the_runner_build_the_fleet_from_one_function(self) -> None:
        """Two lists assembled separately drift; one function cannot."""

        import inspect

        from web_scraper.run import runner as runner_module
        from web_scraper.run.estimate_cli import configured_providers

        source = inspect.getsource(runner_module)
        self.assertIn("from web_scraper.run.estimate_cli import configured_providers", source)
        self.assertIn("configured_providers(self.config.allowed_providers)", source)
        self.assertTrue(callable(configured_providers))

    def test_every_shipped_adapter_is_reachable_from_that_one_function(self) -> None:
        """A provider nobody can build is a provider nobody can use."""

        import os
        from unittest import mock

        from web_scraper.run.estimate_cli import configured_providers

        env = {
            "SCRAPE_DO_TOKEN": "t",
            "FIRECRAWL_API_KEY": "k",
            "BRIGHTDATA_API_KEY": "k",
            "BRIGHTDATA_ZONE": "z",
            "ZENROWS_API_KEY": "k",
            "ZYTE_API_KEY": "k",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            built = {p.name for p in configured_providers([case.name for case in VENDORS])}
        self.assertEqual(built, {case.name for case in VENDORS})

    def test_credentials_alone_never_enable_a_paid_provider(self) -> None:
        import os
        from unittest import mock

        from web_scraper.run.estimate_cli import configured_providers

        env = {
            "SCRAPE_DO_TOKEN": "t",
            "BRIGHTDATA_API_KEY": "k",
            "BRIGHTDATA_ZONE": "z",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(configured_providers(), [])
            self.assertEqual(
                [provider.name for provider in configured_providers(("scrape.do",))],
                ["scrape.do"],
            )

    def test_a_vendor_without_its_key_is_absent_rather_than_broken(self) -> None:
        import os
        from unittest import mock

        from web_scraper.run.estimate_cli import configured_providers

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_providers(), [])


class PreflightReadiness(unittest.TestCase):
    def _report(self, env: dict, *allowed: str):  # type: ignore[no-untyped-def]
        import os
        from unittest import mock

        from web_scraper.run.config import RunConfig
        from web_scraper.run.preflight import preflight

        with mock.patch.dict(os.environ, env, clear=True), tempfile.TemporaryDirectory() as tmp:
            return preflight(
                RunConfig(
                    profile_path=Path(tmp) / "p.json",
                    state_dir=Path(tmp),
                    seed_urls=(),
                    allowed_providers=allowed,
                )
            )

    def test_a_configured_vendor_is_reported_with_its_live_verification_date(self) -> None:
        report = self._report({"SCRAPE_DO_TOKEN": "t"}, "scrape.do")
        line = next(c for c in report.checks if c.name == "provider_scrape.do")
        self.assertTrue(line.ok)
        self.assertIn("live verified", line.detail)

    def test_a_vendor_nobody_can_price_is_flagged_before_the_run_not_during(self) -> None:
        report = self._report({"ZYTE_API_KEY": "k"}, "zyte")
        line = next(c for c in report.checks if c.name == "provider_zyte")
        self.assertFalse(line.ok)
        self.assertIn("unpriceable", line.detail)

    def test_supplying_the_ceiling_clears_it(self) -> None:
        report = self._report(
            {
                "ZYTE_API_KEY": "k",
                "ZYTE_HTTP_MAX_USD": "0.002",
                "ZYTE_BROWSER_MAX_USD": "0.005",
                "ZYTE_CAPTURE_MAX_USD": "0.008",
            },
            "zyte",
        )
        line = next(c for c in report.checks if c.name == "provider_zyte")
        self.assertTrue(line.ok, line.detail)


if __name__ == "__main__":
    unittest.main()
