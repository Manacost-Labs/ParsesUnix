from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.storage import iter_saved_responses

FIXTURES = ROOT / "tests" / "fixtures"
EXPECTED_SCENARIOS = {
    "success",
    "soft-block",
    "blocked",
    "dead-url",
    "rate-limited",
    "origin-down",
    "csr-shell",
    "redesigned",
}


class FixtureVerdictTests(unittest.TestCase):
    """Every verdict is proven by a saved response, never by a live call."""

    def test_every_scenario_is_present(self) -> None:
        names = {saved.name for saved in iter_saved_responses(FIXTURES)}
        self.assertEqual(names, EXPECTED_SCENARIOS)

    def test_saved_responses_produce_expected_verdicts(self) -> None:
        for saved in iter_saved_responses(FIXTURES):
            with self.subTest(scenario=saved.name):
                result = saved.triage()
                self.assertEqual(result.verdict.value, saved.expected["verdict"])
                self.assertEqual(
                    result.paid_escalation_allowed,
                    saved.expected["paid_escalation_allowed"],
                )

    def test_no_free_verdict_ever_escalates_to_paid(self) -> None:
        for saved in iter_saved_responses(FIXTURES):
            result = saved.triage()
            if result.verdict.value in {
                "DEAD_URL",
                "RATE_LIMITED",
                "ORIGIN_DOWN",
                "PARSE_FAIL",
                "ACCESS_DENIED",
            }:
                self.assertFalse(result.paid_escalation_allowed, saved.name)


if __name__ == "__main__":
    unittest.main()
