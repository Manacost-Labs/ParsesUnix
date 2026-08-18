from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Level, Route, RouteType, Verdict
from web_scraper.routing import RouteKey, RouteStatsStore, wilson_lower_bound
from web_scraper.routing.router import AdaptiveRouter

DOMAIN = "x.example"
URL_CLASS = "article"

L0 = Route(type=RouteType.JSON_API, level=Level.L0, url="https://x.example/api/1")
L1 = Route(type=RouteType.DIRECT_HTTP, level=Level.L1)
L2 = Route(type=RouteType.DYNAMIC, level=Level.L2)
PAID = Route(type=RouteType.PROVIDER, level=Level.L3, provider="scrape.do")


class WilsonTests(unittest.TestCase):
    def test_small_samples_are_penalised(self) -> None:
        self.assertLess(wilson_lower_bound(1, 1), wilson_lower_bound(200, 205))

    def test_bounds(self) -> None:
        self.assertEqual(wilson_lower_bound(0, 0), 0.0)
        self.assertEqual(wilson_lower_bound(0, 10), 0.0)
        self.assertLessEqual(wilson_lower_bound(10, 10), 1.0)


class StatsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = RouteStatsStore(Path(self.tempdir.name) / "r.sqlite3", now=lambda: 100.0)
        self.key = RouteKey(DOMAIN, URL_CLASS, "direct_http", "L1")

    def test_validated_success_is_what_counts(self) -> None:
        stats = self.store.record(self.key, verdict=Verdict.OK, latency_ms=100)
        self.assertEqual(stats.validated_successes, 1)
        self.assertEqual(stats.last_success, 100.0)

    def test_soft_block_is_a_failure_not_a_success(self) -> None:
        # A 200 carrying a challenge must never look like a working route.
        stats = self.store.record(self.key, verdict=Verdict.SOFT_BLOCK)
        self.assertEqual(stats.validated_successes, 0)
        self.assertEqual(stats.soft_blocks, 1)
        self.assertEqual(stats.ewma_success, 0.0)

    def test_origin_outage_does_not_depress_the_route(self) -> None:
        for _ in range(10):
            self.store.record(self.key, verdict=Verdict.OK)
        healthy = self.store.get(self.key)
        for _ in range(20):
            self.store.record(self.key, verdict=Verdict.ORIGIN_DOWN)
        after = self.store.get(self.key)
        self.assertEqual(after.ewma_success, healthy.ewma_success)
        self.assertEqual(after.validated_successes, healthy.validated_successes)
        self.assertEqual(after.scored_attempts, healthy.scored_attempts)
        self.assertEqual(after.attempts, 30)  # still visible for observability

    def test_dead_url_and_rate_limit_are_neutral_too(self) -> None:
        for verdict in (Verdict.DEAD_URL, Verdict.RATE_LIMITED, Verdict.AUTH_REQUIRED):
            self.store.record(self.key, verdict=verdict)
        stats = self.store.get(self.key)
        self.assertEqual(stats.scored_attempts, 0)

    def test_parse_fail_is_scored_against_the_route(self) -> None:
        # The door opened but gave nothing usable: that is a route problem.
        self.store.record(self.key, verdict=Verdict.PARSE_FAIL)
        self.assertEqual(self.store.get(self.key).scored_attempts, 1)

    def test_cost_accumulates(self) -> None:
        self.store.record(self.key, verdict=Verdict.OK, cost_credits="1.5")
        stats = self.store.record(self.key, verdict=Verdict.OK, cost_credits="2")
        self.assertEqual(stats.cost_credits, Decimal("3.5"))

    def test_state_survives_reopening(self) -> None:
        self.store.record(self.key, verdict=Verdict.OK)
        reopened = RouteStatsStore(self.store.path)
        self.assertEqual(reopened.get(self.key).validated_successes, 1)

    def test_for_class_lists_every_route(self) -> None:
        self.store.record(self.key, verdict=Verdict.OK)
        self.store.record(RouteKey(DOMAIN, URL_CLASS, "dynamic", "L2"), verdict=Verdict.OK)
        self.assertEqual(len(self.store.for_class(DOMAIN, URL_CLASS)), 2)


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = RouteStatsStore(Path(self.tempdir.name) / "r.sqlite3", now=lambda: 1.0)

    def router(self, **kwargs) -> AdaptiveRouter:
        kwargs.setdefault("rng", lambda: 1.0)  # no shadow probes unless asked
        return AdaptiveRouter(self.store, **kwargs)

    def feed(self, route: Route, verdict: Verdict, times: int, **kwargs) -> None:
        key = RouteKey.for_route(route, domain=DOMAIN, url_class=URL_CLASS)
        for _ in range(times):
            self.store.record(key, verdict=verdict, **kwargs)

    def order(self, routes, **kwargs):
        return self.router(**kwargs).order(routes, domain=DOMAIN, url_class=URL_CLASS)

    def test_without_history_the_declared_ladder_is_preserved(self) -> None:
        self.assertEqual(self.order([L1, L0, L2]), [L1, L0, L2])

    def test_paid_routes_are_never_selected(self) -> None:
        self.assertNotIn(PAID, self.order([L1, PAID]))

    def test_one_lucky_success_does_not_promote_a_route(self) -> None:
        self.feed(L2, Verdict.OK, 1)
        self.assertEqual(self.order([L1, L2])[0], L1)

    def test_a_proven_route_outranks_a_failing_declared_primary(self) -> None:
        self.feed(L1, Verdict.BLOCKED, 20)
        self.feed(L2, Verdict.OK, 20)
        self.assertEqual(self.order([L1, L2])[0], L2)

    def test_hysteresis_keeps_a_near_tie_stable(self) -> None:
        # Both routes look the same; the declared primary must not be displaced.
        self.feed(L1, Verdict.OK, 20)
        self.feed(L2, Verdict.OK, 20)
        self.assertEqual(self.order([L1, L2])[0], L1)

    def test_hysteresis_can_be_overcome_by_a_clear_winner(self) -> None:
        self.feed(L1, Verdict.SOFT_BLOCK, 20)
        self.feed(L2, Verdict.OK, 20)
        ranked = self.router().rank([L1, L2], domain=DOMAIN, url_class=URL_CLASS)
        self.assertEqual(ranked[0].route, L2)
        self.assertIn("confidence", ranked[0].reason)

    def test_cheaper_level_wins_when_reliability_is_equal(self) -> None:
        self.feed(L0, Verdict.OK, 20)
        self.feed(L2, Verdict.OK, 20)
        self.assertEqual(self.order([L2, L0])[0], L0)

    def test_latency_penalises_an_otherwise_equal_route(self) -> None:
        self.feed(L1, Verdict.OK, 20, latency_ms=100)
        self.feed(L2, Verdict.OK, 20, latency_ms=30_000)
        ranked = self.router().rank([L2, L1], domain=DOMAIN, url_class=URL_CLASS)
        self.assertEqual(ranked[0].route, L1)

    def test_shadow_probe_retests_a_cheaper_failing_route(self) -> None:
        self.feed(L0, Verdict.BLOCKED, 20)  # cheap route currently failing
        self.feed(L2, Verdict.OK, 20)
        always_probe = self.router(rng=lambda: 0.0, shadow_probe_rate=1.0)
        ranked = always_probe.rank([L2, L0], domain=DOMAIN, url_class=URL_CLASS)
        self.assertEqual(ranked[0].route, L0)
        self.assertTrue(ranked[0].shadow_probe)
        self.assertIn("shadow probe", ranked[0].reason)

    def test_no_shadow_probe_when_the_dice_say_no(self) -> None:
        self.feed(L0, Verdict.BLOCKED, 20)
        self.feed(L2, Verdict.OK, 20)
        ranked = self.router(rng=lambda: 1.0).rank([L2, L0], domain=DOMAIN, url_class=URL_CLASS)
        self.assertFalse(any(item.shadow_probe for item in ranked))

    def test_an_origin_outage_does_not_reorder_routes(self) -> None:
        self.feed(L1, Verdict.OK, 20)
        baseline = self.order([L1, L2])
        self.feed(L1, Verdict.ORIGIN_DOWN, 50)
        self.assertEqual(self.order([L1, L2]), baseline)

    def test_ranking_explains_itself(self) -> None:
        self.feed(L1, Verdict.OK, 20)
        ranked = self.router().rank([L1, L2], domain=DOMAIN, url_class=URL_CLASS)
        self.assertTrue(all(item.reason for item in ranked))
        self.assertIn("declared order kept", ranked[-1].reason)  # L2 has no history

    def test_router_without_a_store_falls_back_to_the_ladder(self) -> None:
        plain = AdaptiveRouter(None, rng=lambda: 1.0)
        self.assertEqual(plain.order([L1, L0], domain=DOMAIN, url_class=URL_CLASS), [L1, L0])


if __name__ == "__main__":
    unittest.main()


class RouteIdentityTests(unittest.TestCase):
    """Two routes of the same type and level are two routes, not one."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = RouteStatsStore(Path(self.tempdir.name) / "r.sqlite3", now=lambda: 1.0)

    def test_two_apis_at_the_same_level_do_not_share_history(self) -> None:
        v1 = Route(type=RouteType.JSON_API, level=Level.L0, url="https://x.example/api/v1")
        v2 = Route(type=RouteType.JSON_API, level=Level.L0, url="https://x.example/api/v2")
        self.assertNotEqual(v1.route_id, v2.route_id)

        for _ in range(10):
            self.store.record(
                RouteKey.for_route(v1, domain=DOMAIN, url_class=URL_CLASS), verdict=Verdict.OK
            )
            self.store.record(
                RouteKey.for_route(v2, domain=DOMAIN, url_class=URL_CLASS), verdict=Verdict.BLOCKED
            )
        good = self.store.get(RouteKey.for_route(v1, domain=DOMAIN, url_class=URL_CLASS))
        dead = self.store.get(RouteKey.for_route(v2, domain=DOMAIN, url_class=URL_CLASS))
        self.assertEqual(good.success_rate, 1.0)
        self.assertEqual(dead.success_rate, 0.0)

    def test_a_declared_id_survives_a_url_change(self) -> None:
        before = Route(type=RouteType.JSON_API, level=Level.L0, url="https://x/api/v1", id="main")
        after = Route(type=RouteType.JSON_API, level=Level.L0, url="https://x/api/v2", id="main")
        self.assertEqual(before.route_id, after.route_id)
        self.store.record(
            RouteKey.for_route(before, domain=DOMAIN, url_class=URL_CLASS), verdict=Verdict.OK
        )
        carried = self.store.get(RouteKey.for_route(after, domain=DOMAIN, url_class=URL_CLASS))
        self.assertEqual(carried.validated_successes, 1)

    def test_a_urlless_route_keeps_its_pre_identity_key(self) -> None:
        # Backward compatibility: statistics recorded before identities existed
        # were keyed on the bare type, which is what these still derive to.
        self.assertEqual(Route(type=RouteType.DIRECT_HTTP, level=Level.L1).route_id, "direct_http")
        self.assertEqual(Route(type=RouteType.DYNAMIC, level=Level.L2).route_id, "dynamic")

    def test_legacy_database_is_migrated_not_orphaned(self) -> None:
        import sqlite3

        legacy = Path(self.tempdir.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy) as conn:
            conn.execute(
                """
                CREATE TABLE route_stats (
                    domain TEXT NOT NULL, url_class TEXT NOT NULL, route_type TEXT NOT NULL,
                    level TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    scored_attempts INTEGER NOT NULL DEFAULT 0,
                    validated_successes INTEGER NOT NULL DEFAULT 0,
                    blocks INTEGER NOT NULL DEFAULT 0, soft_blocks INTEGER NOT NULL DEFAULT 0,
                    ewma_success REAL NOT NULL DEFAULT 0, latency_ms REAL NOT NULL DEFAULT 0,
                    cost_credits TEXT NOT NULL DEFAULT '0', last_success REAL, last_failure REAL,
                    PRIMARY KEY (domain, url_class, route_type, level)
                )
                """
            )
            conn.execute(
                "INSERT INTO route_stats(domain, url_class, route_type, level, attempts,"
                " scored_attempts, validated_successes) VALUES (?, ?, ?, ?, 5, 5, 5)",
                (DOMAIN, URL_CLASS, "direct_http", "L1"),
            )

        migrated = RouteStatsStore(legacy)
        stats = migrated.get(RouteKey(DOMAIN, URL_CLASS, "direct_http", "L1"))
        self.assertIsNotNone(stats, "pre-identity history must survive the migration")
        self.assertEqual(stats.validated_successes, 5)
