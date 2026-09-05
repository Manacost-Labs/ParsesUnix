"""Acceptance must measure extracted records, not columns or truthiness."""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web_scraper.profile_engineering.acceptance import Fixture, run_case
from web_scraper.profile_engineering.corpus import CaseKind, CorpusCase
from web_scraper.profiles.model import parse_profile


def url_class():
    return parse_profile(
        {
            "site": "example.test",
            "authorization": {"public_data_only": True},
            "url_classes": {
                "records": {
                    "match": r"^https://example\.test/records",
                    "expected_content_type": "json",
                    "validation": {
                        "min_body_bytes": 2,
                        "required_json_paths": ["fixture"],
                        "fields": {
                            "count": {"importance": "optional"},
                            "enabled": {"importance": "optional"},
                            "items": {"importance": "optional"},
                        },
                    },
                    "routes": {"primary": {"type": "direct_http", "level": "L1"}},
                    "extractors": [
                        {
                            "kind": "json",
                            "fields": {"count": "count", "enabled": "enabled", "items": "items"},
                        }
                    ],
                }
            },
        }
    ).url_classes["records"]


def fixture(data):
    return Fixture(
        "records",
        200,
        {"Content-Type": "application/json"},
        json.dumps({"fixture": "records", **data}).encode(),
    )


class AcceptanceRecordQualityTests(unittest.TestCase):
    def case(self, **kwargs):
        return CorpusCase("records", "records", CaseKind.NORMAL, **kwargs)

    def test_zero_and_false_are_present_values(self):
        result = run_case(
            self.case(expect_fields=("count", "enabled"), expect_min_records=1),
            fixture({"count": 0, "enabled": False}),
            url_class(),
        )
        self.assertTrue(result.passed, result.detail)
        self.assertEqual(result.records, 1)

    def test_columns_are_not_multiple_records(self):
        result = run_case(
            self.case(expect_min_records=2), fixture({"count": 1, "enabled": True}), url_class()
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.records, 1)
        self.assertIn("record", result.detail)

    def test_no_extraction_is_zero_records(self):
        result = run_case(self.case(expect_min_records=1), fixture({"unrelated": 1}), url_class())
        self.assertFalse(result.passed)
        self.assertEqual(result.records, 0)

    def test_explicit_collection_counts_distinct_objects(self):
        case = self.case(expect_min_records=2, records_field="items")
        data = {"items": [{"id": 1, "name": "a"}, {"name": "a", "id": 1}, {"id": 2}]}
        result = run_case(case, fixture(data), url_class())
        self.assertTrue(result.passed, result.detail)
        self.assertEqual(result.records, 2)
        insufficient = run_case(replace(case, expect_min_records=3), fixture(data), url_class())
        self.assertFalse(insufficient.passed)

    def test_empty_collection_and_absent_field_have_no_records(self):
        for data in ({"items": []}, {"count": 99}):
            with self.subTest(data=data):
                case = self.case(expect_min_records=0, records_field="items")
                result = run_case(case, fixture(data), url_class())
                self.assertTrue(result.passed, result.detail)
                self.assertEqual(result.records, 0)
                self.assertFalse(
                    run_case(replace(case, expect_min_records=1), fixture(data), url_class()).passed
                )

    def test_strings_scalar_arrays_and_objects_are_not_collection_counts(self):
        for items in ("123", 3, {"id": 1}, [1, 2, 3], [{"id": 1}, "bad"], [{}]):
            with self.subTest(items=items):
                result = run_case(
                    self.case(expect_min_records=1, records_field="items"),
                    fixture({"items": items}),
                    url_class(),
                )
                self.assertFalse(result.passed)
                self.assertIsNone(result.records)

    def test_record_selection_round_trips_in_corpus(self):
        case = self.case(
            expect_min_records=2,
            records_field="items",
            record_identity_field="id",
            expect_values={"count": 0},
        )
        self.assertEqual(CorpusCase.from_dict(case.to_dict()), case)

    def test_unique_identity_count_and_threshold_boundaries(self):
        case = self.case(expect_min_records=1000, records_field="items", record_identity_field="id")
        for count in (999, 1000, 1001):
            result = run_case(
                case, fixture({"items": [{"id": i} for i in range(count)]}), url_class()
            )
            self.assertEqual(result.passed, count >= 1000, result.detail)
            self.assertEqual(result.records, count)
        duplicates = [{"id": 1, "description": str(i)} for i in range(1000)]
        result = run_case(case, fixture({"items": duplicates}), url_class())
        self.assertFalse(result.passed)
        self.assertEqual(result.records, 1)

    def test_missing_identity_is_not_a_verified_record(self):
        for identity in (None, "", False, [], {}):
            result = run_case(
                self.case(expect_min_records=1, records_field="items", record_identity_field="id"),
                fixture({"items": [{"id": identity, "title": "a"}]}),
                url_class(),
            )
            self.assertFalse(result.passed)
            self.assertIsNone(result.records)

    def test_expected_values_are_type_sensitive_and_check_ids(self):
        case = self.case(expect_values={"count": 0, "items": [{"id": "correct"}]})
        self.assertTrue(
            run_case(case, fixture({"count": 0, "items": [{"id": "correct"}]}), url_class()).passed
        )
        for data in (
            {"count": False, "items": [{"id": "correct"}]},
            {"count": 0, "items": [{"id": "wrong"}]},
        ):
            result = run_case(case, fixture(data), url_class())
            self.assertFalse(result.passed)
            self.assertIn("value", result.detail)

    def test_critical_conflict_fails_positive_acceptance(self):
        cls = replace(
            url_class(),
            required_fields=("count",),
            quorum_fields=("count",),
            extractors=(
                {"kind": "json", "fields": {"count": "count"}},
                {"kind": "json", "fields": {"count": "other_count"}},
            ),
        )
        result = run_case(
            self.case(expect_fields=("count",)), fixture({"count": 0, "other_count": 1}), cls
        )
        self.assertFalse(result.passed)
        self.assertIn("conflict", result.detail)

    def test_optional_field_absence_is_still_absence(self):
        result = run_case(
            self.case(expect_absent_fields=("count",)), fixture({"count": 0}), url_class()
        )
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
