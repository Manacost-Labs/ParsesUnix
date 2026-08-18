from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import (
    PAID_ESCALATION_VERDICTS,
    Attempt,
    Level,
    Result,
    Route,
    RouteType,
    Verdict,
)


class LevelTests(unittest.TestCase):
    def test_levels_are_ordered_and_paid_flags_are_correct(self) -> None:
        self.assertEqual([level.rank for level in Level], [0, 1, 2, 3, 4])
        self.assertEqual([level.is_paid for level in Level], [False, False, False, True, True])


class RouteTests(unittest.TestCase):
    def test_route_type_must_match_level(self) -> None:
        with self.assertRaises(ValueError):
            Route(type=RouteType.JSON_API, level=Level.L2)
        with self.assertRaises(ValueError):
            Route(type=RouteType.DIRECT_HTTP, level=Level.L3)

    def test_provider_routes_require_provider_name(self) -> None:
        with self.assertRaises(ValueError):
            Route(type=RouteType.PROVIDER, level=Level.L3)
        route = Route(type=RouteType.PROVIDER, level=Level.L4, provider="brightdata")
        self.assertEqual(route.provider, "brightdata")

    def test_non_provider_routes_reject_provider_name(self) -> None:
        with self.assertRaises(ValueError):
            Route(type=RouteType.RSS, level=Level.L0, provider="scrape.do")

    def test_round_trip(self) -> None:
        route = Route(type=RouteType.RSS, level=Level.L0, url="https://x.example/feed", mode="bulk")
        self.assertEqual(Route.from_dict(route.to_dict()), route)


class ResultTests(unittest.TestCase):
    def test_attempt_and_result_round_trip(self) -> None:
        attempt = Attempt(
            url="https://x.example/a",
            level=Level.L1,
            verdict=Verdict.SOFT_BLOCK,
            reason="challenge",
            route=Route(type=RouteType.DIRECT_HTTP, level=Level.L1),
            status=200,
            body_bytes=812,
        )
        result = Result(
            url="https://x.example/a",
            verdict=Verdict.OK,
            attempts=(attempt,),
            data={"title": "t"},
            extractor_source="json_ld",
        )
        restored = Result.from_dict(result.to_dict())
        self.assertEqual(restored.verdict, Verdict.OK)
        self.assertTrue(restored.resolved)
        self.assertEqual(restored.attempts[0], attempt)

    def test_only_block_verdicts_allow_paid_escalation(self) -> None:
        self.assertEqual(PAID_ESCALATION_VERDICTS, {Verdict.BLOCKED, Verdict.SOFT_BLOCK})


if __name__ == "__main__":
    unittest.main()
