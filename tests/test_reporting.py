from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Result, Verdict
from web_scraper.reporting import summarize


class SummaryTests(unittest.TestCase):
    def test_summary_counts_and_lists_unresolved(self) -> None:
        results = [
            Result(url="https://x.example/1", verdict=Verdict.OK),
            Result(url="https://x.example/2", verdict=Verdict.DEAD_URL),
            Result(url="https://x.example/3", verdict=Verdict.SOFT_BLOCK),
        ]
        summary = summarize(results)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(summary["by_verdict"]["DEAD_URL"], 1)
        self.assertEqual(summary["unresolved_urls"], ["https://x.example/2", "https://x.example/3"])
        self.assertEqual(summary["paid_escalation_candidates"], ["https://x.example/3"])


if __name__ == "__main__":
    unittest.main()
