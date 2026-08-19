"""Multi-run soak: does the system stay correct at scale, across restarts?

Not a benchmark. Every assertion here is an invariant that a single small run
cannot exercise, because the interesting failures are the ones that need volume
and repetition to appear: accounting drift over thousands of URLs, a budget race
between concurrent workers, reservations stranded by a crash, and — the point of
the multi-run part — whether the system actually gets cheaper as it learns.

Size is configurable through ``WS_SOAK_URLS`` so the same scenario can be run at
10,000 or 100,000 outside CI. The default is deliberately modest: a test that
takes four minutes on every PR gets deleted, and then nothing is checked at all.
"""

from __future__ import annotations

import os
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
from web_scraper.contracts import Cost, Verdict
from web_scraper.providers.base import ProviderStrategy
from web_scraper.providers.breaker import BreakerStore, ProviderBreakers
from web_scraper.providers.multi_router import MultiProviderRouter
from web_scraper.providers.stats import ProviderStatsStore, ProviderStrategyKey
from web_scraper.run.phases import Phase, PhaseController, PhaseStore

#: Override for a real soak. 2,000 keeps the suite fast; 100,000 is the target
#: shape and runs in the same code path.
SOAK_URLS = int(os.environ.get("WS_SOAK_URLS", "2000"))

DOMAIN, URL_CLASS = "soak.example", "page"


def synthetic_population(count: int) -> list[tuple[str, Verdict]]:
    """A URL population shaped like a real crawl rather than a happy path.

    The proportions matter more than the exact numbers: most URLs resolve free,
    a meaningful slice is transiently broken, a smaller slice is genuinely
    blocked, and a few are simply dead.
    """

    out: list[tuple[str, Verdict]] = []
    for i in range(count):
        bucket = i % 100
        if bucket < 70:
            verdict = Verdict.OK
        elif bucket < 80:
            verdict = Verdict.ORIGIN_DOWN
        elif bucket < 85:
            verdict = Verdict.RATE_LIMITED
        elif bucket < 93:
            verdict = Verdict.BLOCKED
        elif bucket < 96:
            verdict = Verdict.SOFT_BLOCK
        elif bucket < 98:
            verdict = Verdict.DEAD_URL
        else:
            verdict = Verdict.PARSE_FAIL
        out.append((f"https://soak.example/item/{i}", verdict))
    return out


class SoakCase(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        self.population = synthetic_population(SOAK_URLS)


class AccountingTests(SoakCase):
    """Every URL must end somewhere. Unaccounted is the cardinal sin."""

    def test_every_url_lands_in_exactly_one_phase_or_is_terminal(self) -> None:
        controller = PhaseController(run_id="soak", store=PhaseStore(self.root / "p.sqlite3"))

        free = controller.select(Phase.FREE, self.population)
        retry = controller.select(Phase.FREE_RETRY, self.population)
        cheap = controller.select(Phase.CHEAP_PAID, self.population)

        resolved = [u for u, v in self.population if v is Verdict.OK]
        terminal = [
            u
            for u, v in self.population
            if v in {Verdict.DEAD_URL, Verdict.AUTH_REQUIRED, Verdict.ACCESS_DENIED}
        ]
        # PARSE_FAIL is admitted to the free phase but never to a paid one: it
        # is neither resolved nor terminal, and it must still be accounted for.
        parse_fail = [u for u, v in self.population if v is Verdict.PARSE_FAIL]

        accounted = set(resolved) | set(terminal) | set(free)
        self.assertEqual(
            len(accounted), len(self.population), "unaccounted URLs are the cardinal sin"
        )
        self.assertTrue(set(parse_fail).issubset(set(free)))
        self.assertEqual(set(retry) & set(cheap), set(), "no URL is in two phases at once")

    def test_no_duplicates_survive_selection(self) -> None:
        controller = PhaseController(run_id="soak")
        for phase in Phase:
            with self.subTest(phase=phase):
                selected = controller.select(phase, self.population)
                self.assertEqual(len(selected), len(set(selected)))

    def test_paid_phases_see_only_block_verdicts(self) -> None:
        controller = PhaseController(run_id="soak")
        by_url = dict(self.population)
        for phase in (Phase.CHEAP_PAID, Phase.EXPENSIVE_PAID):
            for url in controller.select(phase, self.population):
                self.assertIn(by_url[url], {Verdict.BLOCKED, Verdict.SOFT_BLOCK})

    def test_a_transient_wobble_never_becomes_a_paid_batch(self) -> None:
        # The scenario the phases exist for: ~15% of the population failed
        # transiently. None of it may reach a provider.
        controller = PhaseController(run_id="soak")
        transient = [
            u for u, v in self.population if v in {Verdict.ORIGIN_DOWN, Verdict.RATE_LIMITED}
        ]
        self.assertGreater(len(transient), SOAK_URLS // 10, "the scenario has real volume")
        paid = set(controller.select(Phase.CHEAP_PAID, self.population))
        self.assertEqual(set(transient) & paid, set())


class BudgetPressureTests(SoakCase):
    """Concurrency and limits, at a volume where a race would show."""

    def test_concurrent_workers_cannot_exceed_the_limit(self) -> None:
        limit = 100
        ledger = BudgetLedger(self.root / "b.sqlite3", daily_credit_limit=str(limit))
        granted: list[object] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(20):
                try:
                    reservation = ledger.reserve(provider="p", credits=1)
                except BudgetExceeded:
                    return
                with lock:
                    granted.append(reservation)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertLessEqual(len(granted), limit, "the limit held under 16 threads")
        self.assertEqual(ledger.held_credits(), Decimal(len(granted)))

    def test_a_long_run_leaves_no_stranded_reservations(self) -> None:
        ledger = BudgetLedger(self.root / "b.sqlite3", daily_credit_limit="100000")
        for i in range(500):
            reservation = ledger.reserve(provider="p", credits=1, strategy_id="normal")
            reservation = ledger.mark_submitted(reservation)
            ledger.settle(reservation, actual_credits=1 if i % 2 else 0)

        self.assertEqual(ledger.open_reservations(), [])
        self.assertEqual(ledger.held_credits(), Decimal("0"))
        self.assertEqual(ledger.state(), BudgetState.OK)

    def test_a_crash_mid_run_strands_nothing_that_recovery_cannot_resolve(self) -> None:
        ledger = BudgetLedger(self.root / "b.sqlite3", daily_credit_limit="100000")
        for i in range(200):
            reservation = ledger.reserve(provider="p", credits=1)
            if i % 3 == 0:
                continue  # died before submitting: safe to release
            reservation = ledger.mark_submitted(reservation)
            if i % 3 == 1:
                continue  # died after submitting: may have been billed
            ledger.settle(reservation, actual_credits=1)

        restarted = BudgetLedger(self.root / "b.sqlite3", daily_credit_limit="100000")
        report = restarted.recover_after_crash()

        self.assertGreater(len(report["released"]), 0, "pre-submit holds came back")
        self.assertGreater(len(report["marked_unknown"]), 0, "post-submit holds stayed held")
        self.assertEqual(
            [r for r in restarted.open_reservations() if r.state is ReservationState.RESERVED],
            [],
            "nothing safe was left stranded",
        )


class LearningTests(SoakCase):
    """Run 1 cold, run 2 informed, run 3 degraded, run 4 recovered."""

    def strategies(self):
        return (
            ProviderStrategy(id="cheap", nominal_cost=Decimal("1"), premium_network=True),
            ProviderStrategy(id="dear", nominal_cost=Decimal("10"), premium_network=True),
        )

    def setUp(self) -> None:
        super().setUp()
        self.stats = ProviderStatsStore(self.root / "stats.sqlite3")
        self.breakers = ProviderBreakers(threshold=5, store=BreakerStore(self.root / "brk.sqlite3"))

        outer = self

        class Vendor:
            name = "v"

            def strategies(self):
                return outer.strategies()

            def fetch(self, request):  # pragma: no cover - routing only
                raise AssertionError("soak routing must not fetch")

        self.vendor = Vendor()

    def router(self) -> MultiProviderRouter:
        return MultiProviderRouter(
            providers=[self.vendor],
            stats=self.stats,
            breakers=self.breakers,
            _rng=lambda: 1.0,
        )

    def observe(self, sid, *, ok, fail, cost="1"):
        key = ProviderStrategyKey(provider="v", strategy_id=sid, domain=DOMAIN, url_class=URL_CLASS)
        for _ in range(ok):
            self.stats.record(key, verdict=Verdict.OK, cost=Cost.of(cost))
        for _ in range(fail):
            self.stats.record(key, verdict=Verdict.BLOCKED, cost=Cost.of(cost))

    def choose(self):
        return self.router().choose(domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED)

    def test_the_system_gets_cheaper_as_it_learns(self) -> None:
        # Run 1: no history. Exploration takes the cheapest option.
        first = self.choose()
        self.assertEqual(first.strategy_id, "cheap")
        self.assertTrue(first.candidates[0].exploring)

        # Run 2: the cheap strategy has proven itself. Still cheap, now on
        # evidence rather than on hope.
        self.observe("cheap", ok=40, fail=0)
        second = self.choose()
        self.assertEqual(second.strategy_id, "cheap")
        self.assertFalse(next(c for c in second.candidates if c.strategy.id == "cheap").exploring)
        self.assertGreater(second.candidates[0].confidence, 0.8)

        # Run 3: the site hardens. The cheap door stops working and the router
        # moves up rather than continuing to pay for failures.
        self.observe("cheap", ok=0, fail=60)
        self.observe("dear", ok=40, fail=0, cost="10")
        third = self.choose()
        self.assertEqual(third.strategy_id, "dear", "it stopped throwing money at a wall")

        # Run 4: the site relaxes. A shadow probe re-tests the cheap door, which
        # is the only way a domain ever comes back down in price.
        probing = MultiProviderRouter(
            providers=[self.vendor],
            stats=self.stats,
            breakers=self.breakers,
            shadow_probe_rate=0.05,
            _rng=lambda: 0.0,
        )
        fourth = probing.choose(domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED)
        self.assertTrue(fourth.shadow_probe)
        self.assertEqual(fourth.strategy_id, "cheap")

    def test_breaker_state_carries_across_runs(self) -> None:
        from web_scraper.providers.base import ProviderErrorKind

        for _ in range(5):
            self.breakers.record_error("v", "cheap", ProviderErrorKind.TIMEOUT)
        self.observe("dear", ok=40, fail=0, cost="10")

        restarted = ProviderBreakers(threshold=5, store=BreakerStore(self.root / "brk.sqlite3"))
        router = MultiProviderRouter(
            providers=[self.vendor], stats=self.stats, breakers=restarted, _rng=lambda: 1.0
        )
        decision = router.choose(domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED)
        self.assertEqual(decision.strategy_id, "dear", "the next run inherited the lesson")


class PhaseResumeSoakTests(SoakCase):
    def test_a_restart_never_replays_a_paid_phase(self) -> None:
        store = PhaseStore(self.root / "phases.sqlite3")
        first = PhaseController(run_id="big", store=store)
        first.complete(Phase.FREE, counts={"processed": SOAK_URLS})
        first.complete(Phase.FREE_RETRY, counts={"processed": SOAK_URLS // 10})
        first.enter(Phase.CHEAP_PAID)

        for _ in range(5):  # five crashes in a row
            resumed = PhaseController(run_id="big", store=store)
            self.assertEqual(resumed.current, Phase.CHEAP_PAID)
            self.assertNotIn(Phase.FREE, resumed.remaining())
            self.assertNotIn(Phase.FREE_RETRY, resumed.remaining())


if __name__ == "__main__":
    unittest.main()
