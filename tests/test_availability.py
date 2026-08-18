"""LKG must never be served as if it were fresh."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.publish import (
    DatasetStore,
    DataStatus,
    build_availability,
    summarize_availability,
)
from web_scraper.publish.availability import classify_record

HOUR = 3_600.0


def row(key: str, updated_at: float | None, data: dict | None = None) -> dict:
    return {
        "natural_key": key,
        "url": f"https://x.example/{key}",
        "updated_at": updated_at,
        "data": {"title": key} if data is None else data,
    }


class ClassifyTests(unittest.TestCase):
    def test_recent_success_is_fresh(self) -> None:
        status, age = classify_record(
            updated_at=900.0, now=1000.0, max_age_seconds=HOUR, last_verdict="OK", has_data=True
        )
        self.assertIs(status, DataStatus.FRESH)
        self.assertEqual(age, 100.0)

    def test_a_confirmed_304_is_its_own_status(self) -> None:
        status, _ = classify_record(
            updated_at=900.0,
            now=1000.0,
            max_age_seconds=HOUR,
            last_verdict="NOT_MODIFIED",
            has_data=True,
        )
        self.assertIs(status, DataStatus.NOT_MODIFIED)

    def test_a_recent_row_whose_refresh_failed_is_stale_not_fresh(self) -> None:
        # The row is young, but the latest attempt did not confirm it. Calling
        # this FRESH is exactly the silent corruption this module exists to stop.
        status, _ = classify_record(
            updated_at=900.0,
            now=1000.0,
            max_age_seconds=HOUR,
            last_verdict="ORIGIN_DOWN",
            has_data=True,
        )
        self.assertIs(status, DataStatus.STALE_LKG)

    def test_beyond_the_window_an_old_success_is_still_old(self) -> None:
        status, age = classify_record(
            updated_at=0.0, now=10 * HOUR, max_age_seconds=HOUR, last_verdict="OK", has_data=True
        )
        self.assertIs(status, DataStatus.STALE_LKG)
        self.assertEqual(age, 10 * HOUR)

    def test_no_record_is_unavailable(self) -> None:
        status, age = classify_record(
            updated_at=None, now=1000.0, max_age_seconds=HOUR, last_verdict=None, has_data=False
        )
        self.assertIs(status, DataStatus.UNAVAILABLE)
        self.assertIsNone(age)


class AvailabilityViewTests(unittest.TestCase):
    def build(self):
        rows = [
            row("fresh", 900.0),
            row("unconfirmed", 900.0),
            row("old", 1000.0 - 5 * HOUR),  # comfortably outside the one-hour window
            {"natural_key": "gone", "url": None, "updated_at": None, "data": None},
        ]
        return build_availability(
            rows,
            now=1000.0,
            max_age_seconds=HOUR,
            verdicts_by_key={"fresh": "OK", "unconfirmed": "BLOCKED", "old": "OK"},
        )

    def test_stale_rows_carry_the_verdict_that_explains_them(self) -> None:
        records = {r.natural_key: r for r in self.build()}
        self.assertEqual(records["unconfirmed"].fresh_failure_verdict, "BLOCKED")
        self.assertIsNone(records["fresh"].fresh_failure_verdict)

    def test_every_record_is_serializable_with_its_status(self) -> None:
        for record in self.build():
            payload = record.to_dict()
            self.assertIn("data_status", payload)
            self.assertIn("data_age_seconds", payload)

    def test_slo_separates_fresh_from_merely_available(self) -> None:
        slo = summarize_availability(self.build())
        self.assertEqual(slo.total, 4)
        self.assertEqual(slo.fresh, 1)
        self.assertEqual(slo.stale, 2)
        self.assertEqual(slo.unavailable, 1)
        self.assertAlmostEqual(slo.fresh_availability, 0.25)
        self.assertAlmostEqual(slo.fresh_plus_lkg_availability, 0.75)
        self.assertEqual(slo.oldest_age_seconds, 5 * HOUR)

    def test_an_empty_dataset_does_not_divide_by_zero(self) -> None:
        slo = summarize_availability([])
        self.assertEqual(slo.fresh_availability, 0.0)
        self.assertEqual(slo.fresh_plus_lkg_availability, 0.0)


class DatasetMetadataTests(unittest.TestCase):
    def test_clean_rows_with_meta_exposes_the_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DatasetStore(Path(tmp) / "d.sqlite3", now=lambda: 4_242.0)
            store.stage("k1", url="https://x.example/1", data={"title": "t"})
            decision = store.promote(required_fields=["title"], expected_count=1)
            self.assertTrue(decision.ok)

            rows = store.clean_rows_with_meta()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["updated_at"], 4_242.0)
            self.assertEqual(rows[0]["data"], {"title": "t"})

            records = build_availability(rows, now=4_300.0, max_age_seconds=HOUR)
            self.assertIs(records[0].status, DataStatus.FRESH)


if __name__ == "__main__":
    unittest.main()
