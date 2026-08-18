from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.run.sweep import sweep_dead_urls  # noqa: E402


class SweepTests(unittest.TestCase):
    def test_quarantines_dead_and_leaves_others(self) -> None:
        statuses = {"a": 200, "b": 404, "c": 410, "d": 405, "e": None}
        quarantined = []
        result = sweep_dead_urls(
            list(statuses),
            head=lambda u: statuses[u],
            quarantine=lambda u, s: quarantined.append((u, s)),
        )
        self.assertEqual(set(result.quarantined), {"b", "c"})
        self.assertEqual(result.inconclusive, 2)  # 405 + network error
        self.assertEqual(sorted(quarantined), [("b", 404), ("c", 410)])


if __name__ == "__main__":
    unittest.main()
