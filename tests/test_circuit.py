from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Verdict
from web_scraper.fetchers.circuit import CircuitBreaker


class CircuitBreakerTests(unittest.TestCase):
    def test_opens_after_threshold_of_same_hard_verdict(self) -> None:
        cb = CircuitBreaker(threshold=3)
        for _ in range(2):
            self.assertFalse(cb.record("d", Verdict.BLOCKED).open)
        self.assertTrue(cb.record("d", Verdict.BLOCKED).open)

    def test_ok_resets_the_streak(self) -> None:
        cb = CircuitBreaker(threshold=2)
        cb.record("d", Verdict.BLOCKED)
        cb.record("d", Verdict.OK)
        self.assertFalse(cb.record("d", Verdict.BLOCKED).open)

    def test_different_hard_verdict_restarts_count(self) -> None:
        cb = CircuitBreaker(threshold=2)
        cb.record("d", Verdict.BLOCKED)
        state = cb.record("d", Verdict.ORIGIN_DOWN)  # different -> count resets to 1
        self.assertFalse(state.open)
        self.assertEqual(state.consecutive, 1)

    def test_non_hard_verdict_does_not_open(self) -> None:
        cb = CircuitBreaker(threshold=2)
        cb.record("d", Verdict.PARSE_FAIL)
        cb.record("d", Verdict.PARSE_FAIL)
        self.assertFalse(cb.is_open("d"))

    def test_domains_are_independent(self) -> None:
        cb = CircuitBreaker(threshold=1)
        cb.record("a", Verdict.BLOCKED)
        self.assertTrue(cb.is_open("a"))
        self.assertFalse(cb.is_open("b"))


if __name__ == "__main__":
    unittest.main()
