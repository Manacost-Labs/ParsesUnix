"""The absolute invariant: every input URL is accounted for, always.

Not every URL can be fetched — dead pages, outages and login walls are real —
but a URL that is neither fetched nor named in the ledger is a defect. These
tests pin that: after any run, ``unaccounted == 0`` and nothing is left in the
"claimed but never resolved" state.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Verdict
from web_scraper.fetchers import FetchGateway, Pacer, RawResponse
from web_scraper.observability.accounting import UrlAccounting, build_accounting
from web_scraper.profiles import parse_profile
from web_scraper.run import RunConfig, Runner
from web_scraper.storage import load_saved_response

FIXTURES = ROOT / "tests" / "fixtures"


class NoWaitPacer(Pacer):
    """Pacing is irrelevant to accounting; never sleep in these tests."""

    def __init__(self) -> None:
        super().__init__(min_interval_s=0, jitter_s=0, sleep=lambda _s: None)

    def pause(self, domain: str) -> float:
        return 0.0

    def backoff(self, seconds: float) -> float:
        return seconds


SUCCESS = "https://demo-news.example/articles/solar-farm-riverton"
DEAD = "https://demo-news.example/articles/deleted-story"
BLOCKED = "https://demo-news.example/articles/blocked-story"


class BuildAccountingTests(unittest.TestCase):
    def test_all_settled_reconciles_to_zero(self) -> None:
        ledger = build_accounting({"DONE": 96, "QUARANTINED": 3, "FAILED": 1})
        self.assertEqual(ledger.input_urls, 100)
        self.assertEqual(ledger.accounted, 100)
        self.assertEqual(ledger.unaccounted, 0)
        self.assertTrue(ledger.is_complete)

    def test_carried_urls_are_accounted_not_lost(self) -> None:
        # A deadline that leaves work for the next run is legitimate, but the
        # remaining URLs must still be counted.
        ledger = build_accounting({"DONE": 50, "PENDING": 40, "RETRY": 10})
        self.assertEqual(ledger.unaccounted, 0)
        self.assertEqual(ledger.carried, {"PENDING": 40, "RETRY": 10})
        self.assertTrue(ledger.is_complete)

    def test_in_progress_at_report_time_is_a_loss(self) -> None:
        ledger = build_accounting({"DONE": 99, "IN_PROGRESS": 1})
        self.assertEqual(ledger.lost, 1)
        self.assertFalse(ledger.is_complete)  # claimed but never resolved

    def test_seeded_url_missing_from_queue_is_surfaced(self) -> None:
        ledger = build_accounting(
            {"DONE": 1}, seeded_urls={"https://x.example/a": True, "https://x.example/b": False}
        )
        self.assertEqual(ledger.missing_from_queue, ("https://x.example/b",))
        self.assertFalse(ledger.is_complete)
        self.assertGreater(ledger.unaccounted, 0)

    def test_unknown_status_still_reconciles(self) -> None:
        # A status the ledger has never seen must not silently vanish.
        ledger = build_accounting({"DONE": 5, "SOMETHING_NEW": 2})
        self.assertEqual(ledger.unaccounted, 0)
        self.assertIn("SOMETHING_NEW", ledger.settled)

    def test_empty_run_is_complete(self) -> None:
        self.assertTrue(build_accounting({}).is_complete)

    def test_report_is_serializable(self) -> None:
        payload = build_accounting({"DONE": 1}).to_dict()
        self.assertEqual(payload["unaccounted"], 0)
        self.assertTrue(payload["complete"])


def raw(scenario: str, url: str) -> RawResponse:
    saved = load_saved_response(FIXTURES / scenario)
    return RawResponse(
        requested_url=url,
        final_url=url,
        status=saved.status,
        headers=saved.headers,
        body=saved.body,
        elapsed_ms=1,
    )


def make_profile():
    return parse_profile(
        {
            "site": "demo-news.example",
            "authorization": {"public_data_only": True},
            "url_classes": {
                "article": {
                    "match": r"^https://demo-news\.example/articles/",
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


class RunAccountingTests(unittest.TestCase):
    """End-to-end: whatever happens, the run's ledger balances."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state = Path(self.tempdir.name)

    def build(self, responses: dict[str, RawResponse], seeds, **config_kwargs) -> Runner:
        profile = make_profile()

        class Fake:
            def fetch(self, url, *, headers=None):
                return responses[url]

        gateway = FetchGateway(
            profile, transport_provider=lambda r, c, u: Fake(), pacer=NoWaitPacer()
        )
        config = RunConfig(
            profile_path=self.state / "p.json",
            state_dir=self.state,
            seed_urls=tuple(seeds),
            **config_kwargs,
        )
        return Runner(config, profile=profile, gateway=gateway, wall_clock=lambda: 1000.0)

    def assert_balanced(self, result) -> UrlAccounting:
        ledger = result.report["accounting"]
        self.assertIsNotNone(ledger, "every report must carry a ledger")
        self.assertEqual(ledger["unaccounted"], 0, f"unaccounted URLs: {ledger}")
        self.assertEqual(ledger["lost_in_progress"], 0, f"URLs left claimed: {ledger}")
        self.assertEqual(ledger["missing_from_queue"], [])
        self.assertTrue(ledger["complete"])
        return ledger

    def test_mixed_outcomes_are_fully_accounted(self) -> None:
        responses = {
            SUCCESS: raw("success", SUCCESS),
            DEAD: raw("dead-url", DEAD),
            BLOCKED: raw("blocked", BLOCKED),
        }
        result = self.build(responses, [SUCCESS, DEAD, BLOCKED]).run()
        ledger = self.assert_balanced(result)
        self.assertEqual(ledger["input_urls"], 3)
        self.assertEqual(ledger["settled"]["DONE"], 1)
        self.assertEqual(ledger["settled"]["QUARANTINED"], 1)
        self.assertEqual(ledger["settled"]["FAILED"], 1)

    def test_a_crashing_url_does_not_abort_the_run_or_escape_the_ledger(self) -> None:
        class Exploding:
            def fetch(self, url, *, headers=None):
                raise RuntimeError("transport blew up")

        profile = make_profile()
        gateway = FetchGateway(
            profile, transport_provider=lambda r, c, u: Exploding(), pacer=NoWaitPacer()
        )
        config = RunConfig(
            profile_path=self.state / "p.json",
            state_dir=self.state,
            seed_urls=(SUCCESS, DEAD),
        )
        runner = Runner(config, profile=profile, gateway=gateway, wall_clock=lambda: 1000.0)

        result = runner.run()  # must not raise
        ledger = self.assert_balanced(result)
        self.assertEqual(ledger["input_urls"], 2)
        # Both URLs got a real verdict instead of disappearing.
        self.assertEqual(
            [r.verdict for r in runner._results], [Verdict.PARSE_FAIL, Verdict.PARSE_FAIL]
        )

    def test_deadline_leaves_urls_carried_but_accounted(self) -> None:
        responses = {SUCCESS: raw("success", SUCCESS), DEAD: raw("dead-url", DEAD)}
        # A deadline of 0 stops before any batch is claimed.
        runner = self.build(responses, [SUCCESS, DEAD], deadline_seconds=0.0)
        result = runner.run()
        ledger = self.assert_balanced(result)
        self.assertEqual(ledger["input_urls"], 2)
        self.assertEqual(sum(ledger["carried_to_next_run"].values()), 2)


if __name__ == "__main__":
    unittest.main()
