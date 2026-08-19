"""The production pipeline through the real Runner, not through its parts.

Testing PhaseController, FreeCanary and check_drift in isolation proves those
modules work. It does not prove the Runner calls them — and for most of this
project's life, it did not. Every test here goes through ``Runner.run()``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.fetchers import FetchGateway, Pacer, RawResponse
from web_scraper.profiles import parse_profile
from web_scraper.run.config import RunConfig
from web_scraper.run.paid_ledger import PaidAttemptLedger, PaidAttemptState
from web_scraper.run.phases import Phase
from web_scraper.run.runner import Runner
from web_scraper.storage import load_saved_response

FIXTURES = ROOT / "tests" / "fixtures"
DOMAIN = "demo-news.example"


def url(i: int) -> str:
    return f"https://demo-news.example/articles/{i}"


def raw(scenario: str, target: str) -> RawResponse:
    saved = load_saved_response(FIXTURES / scenario)
    return RawResponse(
        requested_url=target,
        final_url=target,
        status=saved.status,
        headers=saved.headers,
        body=saved.body,
        elapsed_ms=5,
    )


class NoWaitPacer(Pacer):
    def __init__(self) -> None:
        super().__init__(min_interval_s=0.0, jitter_s=0.0, sleep=lambda _s: None)


def profile():
    return parse_profile(
        {
            "site": DOMAIN,
            "authorization": {"public_data_only": True},
            "url_classes": {
                "article": {
                    "match": r"^https://demo-news\.example/articles/",
                    "expected_content_type": "html",
                    "validation": {
                        "min_body_bytes": 300,
                        "canary": "<article",
                        "required_fields": ["title"],
                    },
                    "routes": {"primary": {"type": "direct_http", "level": "L1"}},
                    "extractors": [{"kind": "json_ld"}, {"kind": "heuristic"}],
                    "quorum_fields": ["title"],
                    "retry": {"max_attempts": 1, "backoff_seconds": 0},
                }
            },
        }
    )


class ScriptedTransport:
    """Serves a scenario per URL and records every fetch."""

    def __init__(self, by_url: dict[str, str], log: list[str]) -> None:
        self._by_url, self._log = by_url, log

    def fetch(self, target: str, *, headers: object = None) -> RawResponse:
        self._log.append(target)
        return raw(self._by_url.get(target, "success"), target)


class PipelineCase(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.state = Path(tempdir.name)
        self.fetches: list[str] = []

    def runner(self, by_url: dict[str, str], *, count: int = 60, **cfg) -> Runner:
        profile_obj = profile()
        config = RunConfig(
            profile_path=self.state / "p.json",
            state_dir=self.state,
            seed_urls=tuple(url(i) for i in range(count)),
            **cfg,
        )
        return Runner(
            config,
            profile=profile_obj,
            gateway=FetchGateway(
                profile_obj,
                transport_provider=lambda r, c, u: ScriptedTransport(by_url, self.fetches),
                pacer=NoWaitPacer(),
            ),
            wall_clock=lambda: 1000.0,
        )


class PhaseWiringTests(PipelineCase):
    def test_the_runner_actually_walks_the_phases(self) -> None:
        result = self.runner({}).run()
        phases = result.report["phases"]
        self.assertIn("A:free", phases["completed"])
        self.assertIn("B:free-retry", phases["completed"])

    def test_a_run_without_a_budget_never_enters_a_paid_phase(self) -> None:
        # Impossible by construction: the paid phases are not in `allowed`.
        result = self.runner({}).run()
        self.assertEqual(result.report["phases"]["allowed"], ["A:free", "B:free-retry"])

    def test_phase_a_has_no_paid_escalator_attached(self) -> None:
        # Not a flag someone can flip — there is no code path to a provider.
        runner = self.runner({})
        self.assertIsNone(runner._gateway._paid)
        self.assertIsNone(runner._paid_gateway)


class AccountingTests(PipelineCase):
    def test_every_seeded_url_is_accounted_for(self) -> None:
        by_url = {url(i): "blocked" for i in range(10)}
        by_url.update({url(i): "dead-url" for i in range(10, 15)})
        result = self.runner(by_url).run()
        self.assertEqual(result.report["accounting"]["unaccounted"], 0)

    def test_a_url_a_phase_declines_is_deferred_not_lost(self) -> None:
        # The failure mode this guards: a phase claims a URL, decides it is not
        # its work, and leaves it IN_PROGRESS forever. `lost_in_progress` counts
        # exactly that, and it must be zero.
        #
        # Transient failures are used rather than blocks, so the run is not
        # vetoed by the canary before the phases get a chance to defer anything.
        result = self.runner({url(i): "origin-down" for i in range(60)}).run()
        accounting = result.report["accounting"]
        self.assertEqual(accounting["unaccounted"], 0)
        self.assertEqual(accounting["lost_in_progress"], 0, "nothing stranded mid-flight")
        self.assertGreater(
            sum(accounting["carried_to_next_run"].values()),
            0,
            "unresolved work is carried, not silently dropped",
        )


class FreeCanaryWiringTests(PipelineCase):
    def test_a_redesigned_site_stops_the_run_before_the_crawl(self) -> None:
        # Every page fetches fine and none of them parse. A full run would
        # produce an unusable dataset.
        result = self.runner({url(i): "redesigned" for i in range(200)}, count=200).run()
        self.assertTrue(result.report.get("aborted"), "the run was stopped")
        self.assertEqual(result.report["canaries"]["free"]["status"], "BLOCK_RUN")

    def test_an_aborted_run_promotes_nothing(self) -> None:
        result = self.runner({url(i): "redesigned" for i in range(200)}, count=200).run()
        self.assertIsNone(result.promote, "the consumer stays on the previous dataset")

    def test_a_healthy_site_passes_the_canary_and_runs(self) -> None:
        result = self.runner({}, count=200).run()
        self.assertFalse(result.report.get("aborted"))
        self.assertEqual(result.report["canaries"]["free"]["status"], "PASS")

    def test_the_canary_is_skipped_when_it_would_cover_the_whole_queue(self) -> None:
        # A canary is a sample. On a tiny queue it just fetches the run twice.
        result = self.runner({}, count=5).run()
        self.assertNotIn("free", result.report["canaries"])

    def test_a_canary_that_raises_does_not_abort_the_run(self) -> None:
        class Exploding:
            def fetch(self, target, *, headers=None):
                raise RuntimeError("transport blew up")

        profile_obj = profile()
        config = RunConfig(
            profile_path=self.state / "p.json",
            state_dir=self.state,
            seed_urls=tuple(url(i) for i in range(80)),
        )
        runner = Runner(
            config,
            profile=profile_obj,
            gateway=FetchGateway(
                profile_obj,
                transport_provider=lambda r, c, u: Exploding(),
                pacer=NoWaitPacer(),
            ),
            wall_clock=lambda: 1000.0,
        )
        result = runner.run()  # must not raise
        self.assertEqual(result.report["accounting"]["unaccounted"], 0)


class DriftWiringTests(PipelineCase):
    def test_the_drift_gate_runs_before_promotion(self) -> None:
        result = self.runner({}, count=60).run()
        self.assertIsNotNone(result.promote)
        assert result.promote is not None
        self.assertIn("drift", result.promote, "the gate was consulted, not skipped")

    def test_a_first_run_reports_that_drift_was_not_evaluated(self) -> None:
        result = self.runner({}, count=60).run()
        assert result.promote is not None
        self.assertEqual(result.promote["drift"]["verdict"], "PASS_WITHOUT_BASELINE")


class FreshnessWiringTests(PipelineCase):
    def test_availability_is_reported_per_url_class(self) -> None:
        result = self.runner({}, count=60).run()
        availability = result.report["metrics"]["availability"]
        self.assertIn("by_url_class", availability)


class PaidLedgerTests(PipelineCase):
    def test_a_url_with_an_unfinished_paid_attempt_is_never_offered_again(self) -> None:
        # The crash case: we do not know whether that call was billed, so the
        # URL is held out rather than paid for a second time.
        ledger = PaidAttemptLedger(self.state / "paid_attempts.sqlite3")
        ledger.start(url(1), provider="scrape.do", strategy_id="normal")

        self.assertFalse(ledger.may_attempt(url(1)))
        reason = ledger.blocked_reason(url(1))
        assert reason is not None
        self.assertIn("may already have been billed", reason)

    def test_a_refused_attempt_leaves_the_url_eligible(self) -> None:
        ledger = PaidAttemptLedger(self.state / "paid_attempts.sqlite3")
        ledger.start(url(2), provider="p", strategy_id="s")
        ledger.finish(url(2), state=PaidAttemptState.REFUSED, reason="budget exhausted")
        self.assertTrue(ledger.may_attempt(url(2)), "nothing was spent")

    def test_a_settled_attempt_is_not_repeated(self) -> None:
        from web_scraper.contracts import Cost

        ledger = PaidAttemptLedger(self.state / "paid_attempts.sqlite3")
        ledger.start(url(3), provider="p", strategy_id="s")
        ledger.finish(url(3), state=PaidAttemptState.SETTLED, cost=Cost.of("5"))
        self.assertFalse(ledger.may_attempt(url(3)))

    def test_stranded_attempts_are_surfaced_for_reconciliation(self) -> None:
        ledger = PaidAttemptLedger(self.state / "paid_attempts.sqlite3")
        ledger.start(url(4), provider="p", strategy_id="s")
        self.assertEqual(len(ledger.stranded()), 1)
        self.assertEqual(ledger.summary()["stranded"], 1)

    def test_the_ledger_survives_a_restart(self) -> None:
        first = PaidAttemptLedger(self.state / "paid_attempts.sqlite3")
        first.start(url(5), provider="p", strategy_id="s")

        restarted = PaidAttemptLedger(self.state / "paid_attempts.sqlite3")
        self.assertFalse(restarted.may_attempt(url(5)), "a new process inherits the record")


class ResumeTests(PipelineCase):
    def test_a_second_run_does_not_replay_completed_phases_mid_cycle(self) -> None:
        runner = self.runner({url(i): "blocked" for i in range(60)})
        runner.run()
        # A completed cycle resets, which is what separates "resume after a
        # crash" from "run again tomorrow".
        second = self.runner({})
        self.assertEqual(second._phases.current, Phase.FREE)


if __name__ == "__main__":
    unittest.main()
