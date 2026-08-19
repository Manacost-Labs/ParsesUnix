"""Staged execution, and the crash that must not pay twice."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Verdict
from web_scraper.run.phases import (
    Phase,
    PhaseController,
    PhaseStore,
    admits,
)


class OrderingTests(unittest.TestCase):
    def test_phases_are_ordered_free_before_paid(self) -> None:
        self.assertLess(Phase.FREE.rank, Phase.FREE_RETRY.rank)
        self.assertLess(Phase.FREE_RETRY.rank, Phase.CHEAP_PAID.rank)
        self.assertLess(Phase.CHEAP_PAID.rank, Phase.EXPENSIVE_PAID.rank)

    def test_only_the_last_two_phases_can_spend(self) -> None:
        self.assertFalse(Phase.FREE.is_paid)
        self.assertFalse(Phase.FREE_RETRY.is_paid)
        self.assertTrue(Phase.CHEAP_PAID.is_paid)
        self.assertTrue(Phase.EXPENSIVE_PAID.is_paid)


class AdmissionTests(unittest.TestCase):
    """The rule that stops an origin wobble from becoming a paid batch."""

    def test_a_transient_failure_is_retried_for_free_first(self) -> None:
        for verdict in (Verdict.ORIGIN_DOWN, Verdict.RATE_LIMITED, Verdict.PROVIDER_ERROR):
            with self.subTest(verdict=verdict):
                self.assertTrue(admits(Phase.FREE_RETRY, verdict))

    def test_a_transient_failure_never_reaches_a_paid_phase(self) -> None:
        # 800 URLs failing during a five-minute origin wobble must not become
        # 800 paid calls; a free retry twenty minutes later gets them for zero.
        for verdict in (Verdict.ORIGIN_DOWN, Verdict.RATE_LIMITED):
            with self.subTest(verdict=verdict):
                self.assertFalse(admits(Phase.CHEAP_PAID, verdict))
                self.assertFalse(admits(Phase.EXPENSIVE_PAID, verdict))

    def test_a_block_is_not_wasted_on_a_free_retry(self) -> None:
        # Retrying a refusal identically just costs time.
        self.assertFalse(admits(Phase.FREE_RETRY, Verdict.BLOCKED))
        self.assertTrue(admits(Phase.CHEAP_PAID, Verdict.BLOCKED))

    def test_terminal_verdicts_are_admitted_nowhere(self) -> None:
        for verdict in (Verdict.DEAD_URL, Verdict.AUTH_REQUIRED, Verdict.ACCESS_DENIED):
            for phase in Phase:
                with self.subTest(verdict=verdict, phase=phase):
                    self.assertFalse(admits(phase, verdict))

    def test_a_resolved_url_is_admitted_nowhere(self) -> None:
        for phase in Phase:
            with self.subTest(phase=phase):
                self.assertFalse(admits(phase, Verdict.OK))

    def test_a_parse_failure_never_reaches_a_paid_phase(self) -> None:
        # A page that parsed into nothing will parse into nothing again; a
        # provider fetching it more expensively changes nothing.
        self.assertFalse(admits(Phase.CHEAP_PAID, Verdict.PARSE_FAIL))
        self.assertFalse(admits(Phase.EXPENSIVE_PAID, Verdict.PARSE_FAIL))

    def test_a_csr_page_is_the_browsers_job_not_a_providers(self) -> None:
        self.assertFalse(admits(Phase.CHEAP_PAID, Verdict.CSR_REQUIRED))

    def test_selection_filters_a_mixed_batch(self) -> None:
        controller = PhaseController(run_id="r")
        candidates = [
            ("https://s/ok", Verdict.OK),
            ("https://s/dead", Verdict.DEAD_URL),
            ("https://s/down", Verdict.ORIGIN_DOWN),
            ("https://s/blocked", Verdict.BLOCKED),
        ]
        self.assertEqual(controller.select(Phase.FREE_RETRY, candidates), ["https://s/down"])
        self.assertEqual(controller.select(Phase.CHEAP_PAID, candidates), ["https://s/blocked"])


class ResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.path = Path(tempdir.name) / "phases.sqlite3"

    def controller(self, run_id="run-1", **kw) -> PhaseController:
        return PhaseController(run_id=run_id, store=PhaseStore(self.path), **kw)

    def test_a_fresh_run_starts_at_the_free_phase(self) -> None:
        self.assertEqual(self.controller().current, Phase.FREE)

    def test_a_crash_in_a_paid_phase_resumes_there(self) -> None:
        # Restarting the paid phases would pay twice for the same URLs - the one
        # error the whole budget system exists to prevent.
        first = self.controller()
        first.complete(Phase.FREE, counts={"processed": 10_000})
        first.complete(Phase.FREE_RETRY, counts={"processed": 800})
        first.enter(Phase.CHEAP_PAID)

        restarted = self.controller()
        self.assertEqual(restarted.current, Phase.CHEAP_PAID)
        self.assertEqual(restarted.remaining(), [Phase.CHEAP_PAID, Phase.EXPENSIVE_PAID])

    def test_completed_phases_are_not_repeated(self) -> None:
        first = self.controller()
        first.complete(Phase.FREE)
        first.complete(Phase.FREE_RETRY)

        restarted = self.controller()
        self.assertNotIn(Phase.FREE, restarted.remaining())
        self.assertNotIn(Phase.FREE_RETRY, restarted.remaining())

    def test_per_phase_counts_survive_a_restart(self) -> None:
        first = self.controller()
        first.complete(Phase.FREE, counts={"processed": 9300, "resolved": 8900})

        restarted = self.controller()
        self.assertEqual(restarted.state.counts["A:free"]["resolved"], 8900)

    def test_two_runs_do_not_share_phase_state(self) -> None:
        a = self.controller("run-a")
        a.complete(Phase.FREE)
        b = self.controller("run-b")
        self.assertEqual(b.current, Phase.FREE)
        self.assertEqual(b.state.completed, [])

    def test_a_finished_run_reports_complete(self) -> None:
        controller = self.controller()
        for phase in Phase:
            controller.complete(phase)
        self.assertTrue(controller.state.is_complete)
        self.assertEqual(controller.remaining(), [])


class BudgetGateTests(unittest.TestCase):
    def test_an_unfunded_run_cannot_reach_a_paid_phase(self) -> None:
        # Impossible rather than unlikely: the paid phases are not in the list.
        controller = PhaseController(run_id="r", allowed=(Phase.FREE, Phase.FREE_RETRY))
        self.assertEqual(controller.remaining(), [Phase.FREE, Phase.FREE_RETRY])
        self.assertFalse(any(p.is_paid for p in controller.remaining()))

    def test_a_funded_run_reaches_all_four(self) -> None:
        self.assertEqual(len(PhaseController(run_id="r").remaining()), 4)


if __name__ == "__main__":
    unittest.main()
