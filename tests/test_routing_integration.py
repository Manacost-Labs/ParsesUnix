"""The router and the gateway, wired together.

Two things must hold at once: the gateway learns from history, and none of the
escalation invariants bend to accommodate that learning.
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
from web_scraper.profiles import parse_profile
from web_scraper.routing import RouteKey, RouteStatsStore
from web_scraper.routing.router import AdaptiveRouter
from web_scraper.run import RunConfig, Runner
from web_scraper.storage import load_saved_response

FIXTURES = ROOT / "tests" / "fixtures"
PAGE = "https://demo-news.example/articles/solar-farm-riverton"
DOMAIN = "demo-news.example"


class NoWaitPacer(Pacer):
    def __init__(self) -> None:
        super().__init__(min_interval_s=0, jitter_s=0, sleep=lambda _s: None)

    def pause(self, domain: str) -> float:
        return 0.0

    def backoff(self, seconds: float) -> float:
        return seconds


def raw(scenario: str, url: str = PAGE) -> RawResponse:
    saved = load_saved_response(FIXTURES / scenario)
    return RawResponse(
        requested_url=url,
        final_url=url,
        status=saved.status,
        headers=saved.headers,
        body=saved.body,
        elapsed_ms=11,
    )


def profile_with(primary: dict, alternatives: list[dict]):
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
                    "routes": {"primary": primary, "alternatives": alternatives},
                    "extractors": [{"kind": "json_ld"}, {"kind": "heuristic"}],
                    "quorum_fields": ["title"],
                    "retry": {"max_attempts": 1, "backoff_seconds": 0},
                }
            },
        }
    )


L1_PRIMARY = {"type": "direct_http", "level": "L1"}
L2_ALT = {"type": "dynamic", "level": "L2"}


class TrackingTransport:
    """Serves a scenario per level and records which levels were attempted."""

    def __init__(self, by_level: dict[str, str], log: list[str], level: str) -> None:
        self._by_level = by_level
        self._log = log
        self._level = level

    def fetch(self, url: str, *, headers: object = None) -> RawResponse:
        self._log.append(self._level)
        return raw(self._by_level[self._level], url)


class GatewayRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.stats = RouteStatsStore(Path(self.tempdir.name) / "routes.sqlite3", now=lambda: 5.0)
        self.calls: list[str] = []

    def gateway(self, profile, by_level: dict[str, str], *, router: AdaptiveRouter | None = None):
        def provider(route, url_class, url):
            return TrackingTransport(by_level, self.calls, route.level.value)

        return FetchGateway(
            profile,
            transport_provider=provider,
            pacer=NoWaitPacer(),
            route_stats=self.stats,
            router=router,
        )

    def key(self, route_id: str, level: str) -> RouteKey:
        return RouteKey(DOMAIN, "article", route_id, level)

    def test_attempts_are_recorded_against_the_route(self) -> None:
        profile = profile_with(L1_PRIMARY, [])
        self.gateway(profile, {"L1": "success"}).fetch_url(PAGE)
        stats = self.stats.get(self.key("direct_http", "L1"))
        self.assertEqual(stats.attempts, 1)
        self.assertEqual(stats.validated_successes, 1)
        self.assertGreater(stats.latency_ms, 0)

    def test_a_soft_block_is_recorded_as_a_failure_not_a_success(self) -> None:
        profile = profile_with(L1_PRIMARY, [L2_ALT])
        self.gateway(profile, {"L1": "soft-block", "L2": "success"}).fetch_url(PAGE)
        l1 = self.stats.get(self.key("direct_http", "L1"))
        l2 = self.stats.get(self.key("dynamic", "L2"))
        self.assertEqual(l1.validated_successes, 0)
        self.assertEqual(l1.soft_blocks, 1)
        self.assertEqual(l2.validated_successes, 1)

    def test_an_origin_outage_is_recorded_but_does_not_score_the_route(self) -> None:
        profile = profile_with(L1_PRIMARY, [])
        self.gateway(profile, {"L1": "origin-down"}).fetch_url(PAGE)
        stats = self.stats.get(self.key("direct_http", "L1"))
        self.assertEqual(stats.attempts, 1)
        self.assertEqual(stats.scored_attempts, 0)  # says nothing about the route

    def test_history_reorders_the_plan_and_skips_the_proven_dead_route(self) -> None:
        profile = profile_with(L1_PRIMARY, [L2_ALT])
        # Teach the store that L1 is reliably blocked and L2 reliably works.
        for _ in range(20):
            self.stats.record(self.key("direct_http", "L1"), verdict=Verdict.BLOCKED)
            self.stats.record(self.key("dynamic", "L2"), verdict=Verdict.OK)

        router = AdaptiveRouter(self.stats, rng=lambda: 1.0)  # no shadow probe
        outcome = self.gateway(
            profile, {"L1": "blocked", "L2": "success"}, router=router
        ).fetch_url(PAGE)
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        # L2 went first; the known-dead L1 was not spent at all.
        self.assertEqual(self.calls, ["L2"])

    def test_without_history_the_declared_primary_is_still_first(self) -> None:
        profile = profile_with(L1_PRIMARY, [L2_ALT])
        router = AdaptiveRouter(self.stats, rng=lambda: 1.0)
        self.gateway(profile, {"L1": "success", "L2": "success"}, router=router).fetch_url(PAGE)
        self.assertEqual(self.calls, ["L1"])

    def test_router_never_promotes_a_paid_route(self) -> None:
        profile = profile_with(
            L1_PRIMARY, [{"type": "provider", "level": "L3", "provider": "scrape.do"}]
        )
        for _ in range(20):
            self.stats.record(self.key("direct_http", "L1"), verdict=Verdict.BLOCKED)
            self.stats.record(self.key("provider", "L3"), verdict=Verdict.OK)

        router = AdaptiveRouter(self.stats, rng=lambda: 1.0)
        outcome = self.gateway(profile, {"L1": "blocked"}, router=router).fetch_url(PAGE)
        self.assertEqual(self.calls, ["L1"])  # the paid route was never attempted
        self.assertTrue(outcome.paid_escalation_candidate)  # reported, not taken
        paid_skips = [s for s in outcome.skipped_routes if s["route"]["level"] == "L3"]
        self.assertEqual(len(paid_skips), 1)

    def test_shadow_probe_re_tests_a_route_history_calls_dead(self) -> None:
        profile = profile_with(L1_PRIMARY, [L2_ALT])
        for _ in range(20):
            self.stats.record(self.key("direct_http", "L1"), verdict=Verdict.BLOCKED)
            self.stats.record(self.key("dynamic", "L2"), verdict=Verdict.OK)

        always = AdaptiveRouter(self.stats, rng=lambda: 0.0, shadow_probe_rate=1.0)
        # The site has quietly relaxed: L1 works again, and only a probe finds out.
        outcome = self.gateway(
            profile, {"L1": "success", "L2": "success"}, router=always
        ).fetch_url(PAGE)
        self.assertEqual(self.calls, ["L1"])
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertGreater(self.stats.get(self.key("direct_http", "L1")).validated_successes, 0)


class RunnerRoutingTests(unittest.TestCase):
    def test_a_run_persists_route_memory_for_the_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            profile = profile_with(L1_PRIMARY, [])
            calls: list[str] = []

            def provider(route, url_class, url):
                return TrackingTransport({"L1": "success"}, calls, route.level.value)

            config = RunConfig(profile_path=state / "p.json", state_dir=state, seed_urls=(PAGE,))
            runner = Runner(
                config,
                profile=profile,
                gateway=FetchGateway(
                    profile,
                    transport_provider=provider,
                    pacer=NoWaitPacer(),
                    route_stats=RouteStatsStore(config.route_stats_path, now=lambda: 5.0),
                ),
                wall_clock=lambda: 1000.0,
            )
            runner.run()

            # A separate store reading the same file sees the run's history.
            reopened = RouteStatsStore(config.route_stats_path)
            stats = reopened.get(RouteKey(DOMAIN, "article", "direct_http", "L1"))
            self.assertIsNotNone(stats)
            self.assertEqual(stats.validated_successes, 1)

    def test_adaptive_routing_can_be_switched_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = RunConfig(
                profile_path=Path(tmp) / "p.json",
                state_dir=Path(tmp),
                seed_urls=(),
                adaptive_routing=False,
            )
            runner = Runner(config, profile=profile_with(L1_PRIMARY, []), wall_clock=lambda: 1.0)
            self.assertIsNone(runner._gateway._router)


if __name__ == "__main__":
    unittest.main()
