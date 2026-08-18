from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.publish import DatasetStore, validate_staging  # noqa: E402


class ValidateStagingTests(unittest.TestCase):
    def rows(self, n, missing=0):
        out = [{"title": f"t{i}", "price": i} for i in range(n - missing)]
        out += [{"title": None, "price": i} for i in range(missing)]
        return out

    def test_complete_dataset_passes(self) -> None:
        d = validate_staging(self.rows(10), required_fields=["title", "price"],
                             expected_count=10, min_completeness=0.95)
        self.assertTrue(d.ok)

    def test_volume_shortfall_rejects(self) -> None:
        d = validate_staging(self.rows(5), required_fields=["title"],
                             expected_count=100, min_completeness=0.95)
        self.assertFalse(d.ok)
        self.assertIn("volume", d.reason)

    def test_completeness_shortfall_rejects(self) -> None:
        d = validate_staging(self.rows(10, missing=3), required_fields=["title"],
                             expected_count=10, min_completeness=0.95)
        self.assertFalse(d.ok)
        self.assertIn("completeness", d.reason)

    def test_null_rate_growth_rejects(self) -> None:
        d = validate_staging(self.rows(10, missing=2), required_fields=["title"],
                             expected_count=10, min_completeness=0.5,
                             baseline_null_rate={"title": 0.02}, max_null_rate_growth=2.0)
        self.assertFalse(d.ok)
        self.assertIn("null-rate", d.reason)


class DatasetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = DatasetStore(Path(self.tempdir.name) / "data.sqlite3", now=lambda: 1.0)

    def stage_good(self, n=10):
        self.store.reset_staging()
        for i in range(n):
            self.store.stage(f"k{i}", url=f"https://x.example/{i}", data={"title": f"t{i}"})

    def test_atomic_promote_updates_clean(self) -> None:
        self.stage_good(10)
        decision = self.store.promote(required_fields=["title"], expected_count=10)
        self.assertTrue(decision.ok)
        self.assertEqual(self.store.clean_count(), 10)
        self.assertEqual(self.store.staged_rows(), [])  # staging cleared on success

    def test_rejected_run_leaves_clean_untouched_and_keeps_staging(self) -> None:
        # First good promote establishes a clean dataset.
        self.stage_good(10)
        self.store.promote(required_fields=["title"], expected_count=10)
        before = self.store.clean_rows()
        # A broken partial run: only 2 of an expected 10.
        self.store.reset_staging()
        self.store.stage("k0", url="https://x.example/0", data={"title": "changed"})
        self.store.stage("k1", url="https://x.example/1", data={"title": "changed"})
        decision = self.store.promote(required_fields=["title"], expected_count=10, min_completeness=0.95)
        self.assertFalse(decision.ok)
        self.assertEqual(self.store.clean_rows(), before)  # unchanged: no half-update
        self.assertEqual(len(self.store.staged_rows()), 2)  # kept for review

    def test_conflicts_are_counted(self) -> None:
        self.store.reset_staging()
        self.store.stage("k0", url="u", data={"title": "a"}, conflict=True)
        decision = self.store.promote(required_fields=["title"], expected_count=1)
        self.assertEqual(decision.conflicts, 1)


if __name__ == "__main__":
    unittest.main()
