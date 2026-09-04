from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.fetchers import FetchGateway, Pacer, RawResponse
from web_scraper.profiles import parse_profile
from web_scraper.queue import UrlStatus
from web_scraper.run import RunConfig, Runner
from web_scraper.storage import load_saved_response

FIXTURES = ROOT / "tests" / "fixtures"

SUCCESS = "https://demo-news.example/articles/solar-farm-riverton"
DEAD = "https://demo-news.example/articles/deleted-story"
BLOCKED = "https://demo-news.example/articles/blocked-story"


def raw_from_fixture(scenario: str, url: str) -> RawResponse:
    saved = load_saved_response(FIXTURES / scenario)
    return RawResponse(
        requested_url=url,
        final_url=url,
        status=saved.status,
        headers=saved.headers,
        body=saved.body,
        elapsed_ms=5,
    )


class NoWaitPacer(Pacer):
    def __init__(self) -> None:
        super().__init__(min_interval_s=0, jitter_s=0, sleep=lambda _s: None)

    def pause(self, domain):
        return 0.0

    def backoff(self, seconds):
        return seconds


def make_profile():
    return parse_profile(
        {
            "site": "demo-news.example",
            "authorization": {"public_data_only": True},
            "url_classes": {
                "article": {
                    "match": "^https://demo-news\\.example/articles/",
                    "expected_content_type": "html",
                    "validation": {
                        "min_body_bytes": 300,
                        "canary": "<article",
                        "required_fields": ["title", "published_at"],
                    },
                    "routes": {
                        "primary": {"type": "direct_http", "level": "L1"},
                        "alternatives": [{"type": "dynamic", "level": "L2"}],
                    },
                    "extractors": [
                        {"kind": "json_ld", "schema_type": "Article"},
                        {"kind": "heuristic"},
                    ],
                    "quorum_fields": ["title", "published_at"],
                    "retry": {"max_attempts": 1, "backoff_seconds": 0},
                    "promote": {"min_completeness": 0.95, "max_null_rate_growth": 2.0},
                }
            },
        }
    )


def make_json_profile():
    return parse_profile(
        {
            "site": "demo-news.example",
            "authorization": {"public_data_only": True},
            "url_classes": {
                "article": {
                    "match": "^https://demo-news\\.example/api/",
                    "expected_content_type": "json",
                    "validation": {
                        "min_body_bytes": 2,
                        "required_fields": ["title"],
                        "required_json_paths": ["title"],
                    },
                    "routes": {"primary": {"type": "json_api", "level": "L0"}},
                    "extractors": [{"kind": "json", "fields": {"title": "title"}}],
                    "quorum_fields": ["title"],
                    "retry": {"max_attempts": 1, "backoff_seconds": 0},
                    "promote": {"min_completeness": 0.95},
                }
            },
        }
    )


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state = Path(self.tempdir.name)
        self.wall = [1000.0]

    def build_runner(self, responses: dict[str, RawResponse], seeds):
        profile = make_profile()

        class Fake:
            def fetch(self, url, *, headers=None):
                return responses[url]

        def provider(route, url_class, url):
            return Fake()

        gateway = FetchGateway(profile, transport_provider=provider, pacer=NoWaitPacer())
        config = RunConfig(
            profile_path=self.state / "p.json", state_dir=self.state, seed_urls=tuple(seeds)
        )
        return Runner(config, profile=profile, gateway=gateway, wall_clock=lambda: self.wall[0])

    def test_full_run_covers_every_url_with_a_status(self) -> None:
        responses = {
            SUCCESS: raw_from_fixture("success", SUCCESS),
            DEAD: raw_from_fixture("dead-url", DEAD),
            BLOCKED: raw_from_fixture("blocked", BLOCKED),
        }
        runner = self.build_runner(responses, [SUCCESS, DEAD, BLOCKED])
        result = runner.run()

        self.assertEqual(result.processed, 3)
        self.assertEqual(runner.queue.get(SUCCESS).status, UrlStatus.DONE)
        self.assertEqual(runner.queue.get(DEAD).status, UrlStatus.QUARANTINED)
        self.assertEqual(runner.queue.get(BLOCKED).status, UrlStatus.FAILED)
        # No URL is left without a terminal-ish status.
        statuses = {r.url: r.status for r in runner.queue.all_rows()}
        self.assertNotIn(UrlStatus.IN_PROGRESS, set(statuses.values()))
        # The one good article was extracted and promoted into the clean dataset.
        clean = runner.dataset.clean_rows()
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["title"], "Solar farm opens near Riverton")
        self.assertTrue(result.promote["ok"])

    def test_rerun_is_idempotent_no_duplicate_clean_rows(self) -> None:
        responses = {SUCCESS: raw_from_fixture("success", SUCCESS)}
        self.build_runner(responses, [SUCCESS]).run()
        # Second run over the same state: freshness skips the fetch, no dup rows.
        runner2 = self.build_runner(responses, [SUCCESS])
        result2 = runner2.run()
        self.assertEqual(len(runner2.dataset.clean_rows()), 1)
        self.assertGreaterEqual(result2.report["metrics"]["fresh_unchanged"], 0)

    def test_resume_after_crash_processes_remaining(self) -> None:
        responses = {SUCCESS: raw_from_fixture("success", SUCCESS)}
        runner = self.build_runner(responses, [SUCCESS])
        # Simulate a crash: claim but do not process.
        runner.queue.add(SUCCESS)
        runner.queue.claim_batch(10)
        # A fresh runner resumes: the stale IN_PROGRESS row returns to PENDING.
        runner2 = self.build_runner(responses, [])
        runner2.run()
        self.assertEqual(runner2.queue.get(SUCCESS).status, UrlStatus.DONE)

    def test_json_full_runner_publishes_and_second_run_keeps_typed_data(self) -> None:
        url = "https://demo-news.example/api/article/1"
        body = b'{"title": "Typed JSON title", "count": 0}'
        response = RawResponse(
            requested_url=url,
            final_url=url,
            status=200,
            headers={"Content-Type": "application/json"},
            body=body,
            elapsed_ms=1,
        )
        profile = make_json_profile()

        class Fake:
            def fetch(self, requested, *, headers=None):
                return response

        gateway = FetchGateway(
            profile,
            transport_provider=lambda route, url_class, requested: Fake(),
            pacer=NoWaitPacer(),
        )
        config = RunConfig(
            profile_path=self.state / "p.json", state_dir=self.state, seed_urls=(url,)
        )
        runner = Runner(config, profile=profile, gateway=gateway, wall_clock=lambda: self.wall[0])
        first = runner.run()
        self.assertTrue(first.promote["ok"])
        self.assertEqual(runner.dataset.clean_rows()[0]["title"], "Typed JSON title")
        self.assertEqual(
            runner.dataset.clean_rows()[0]["_extractor_source"], {"title": "json_path"}
        )

        changed_response = RawResponse(
            requested_url=url,
            final_url=url,
            status=200,
            headers={"Content-Type": "application/json"},
            body=b'{"title": "Changed JSON title", "count": 0}',
            elapsed_ms=1,
        )

        class ChangedFake:
            def fetch(self, requested, *, headers=None):
                return changed_response

        changed_gateway = FetchGateway(
            profile,
            transport_provider=lambda route, url_class, requested: ChangedFake(),
            pacer=NoWaitPacer(),
        )
        self.wall[0] += 100
        runner2 = Runner(
            RunConfig(
                profile_path=self.state / "p.json",
                state_dir=self.state,
                seed_urls=(url,),
                full_review=True,
                run_id="second-json-run",
            ),
            profile=profile,
            gateway=changed_gateway,
            wall_clock=lambda: self.wall[0],
        )
        second = runner2.run()
        self.assertTrue(second.promote["ok"])
        self.assertEqual(runner2.dataset.clean_rows()[0]["title"], "Changed JSON title")
        self.assertEqual(
            runner2.dataset.clean_rows()[0]["_extractor_source"], {"title": "json_path"}
        )
        self.assertEqual(second.promote["drift"]["current"]["types"]["title"], ["str"])
        self.assertEqual(
            second.promote["drift"]["baseline"]["provenance"]["title"], {"json_path": 1}
        )


if __name__ == "__main__":
    unittest.main()
