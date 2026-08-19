"""The gate that catches a run which succeeds and publishes wrong data."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.publish.drift import (
    DriftVerdict,
    SchemaSnapshot,
    check_drift,
)


def rows(n: int, *, title="Some title", price=10, source="json_ld", extra=None):
    out = []
    for i in range(n):
        row = {
            "id": f"item-{i}",
            "title": title,
            "price": price,
            "_extractor_source": {"id": source, "title": source, "price": source},
        }
        if extra:
            row.update(extra)
        out.append(row)
    return out


class BaselineTests(unittest.TestCase):
    def test_a_first_run_says_it_was_not_evaluated_rather_than_passed(self) -> None:
        # An operator reading "PASS" is entitled to believe something was checked.
        report = check_drift(SchemaSnapshot.from_rows(rows(50)), None)
        self.assertEqual(report.verdict, DriftVerdict.PASS_WITHOUT_BASELINE)
        self.assertTrue(report.verdict.allows_promotion, "a first run must still publish")
        self.assertFalse(report.verdict.was_evaluated)
        self.assertIn("nothing to drift from", report.explain())

    def test_an_identical_dataset_reports_nothing(self) -> None:
        base = SchemaSnapshot.from_rows(rows(50))
        report = check_drift(SchemaSnapshot.from_rows(rows(50)), base)
        self.assertEqual(report.verdict, DriftVerdict.PASS)
        self.assertEqual(report.findings, ())


class RecordCountTests(unittest.TestCase):
    def test_losing_most_records_blocks_promotion(self) -> None:
        base = SchemaSnapshot.from_rows(rows(1000))
        report = check_drift(SchemaSnapshot.from_rows(rows(100)), base)
        self.assertEqual(report.verdict, DriftVerdict.BLOCK_PROMOTION)
        self.assertEqual(report.blocking[0].kind, "record_count_collapse")

    def test_normal_variation_is_allowed(self) -> None:
        base = SchemaSnapshot.from_rows(rows(1000))
        report = check_drift(SchemaSnapshot.from_rows(rows(950)), base)
        self.assertTrue(report.verdict.allows_promotion)

    def test_growth_is_never_a_problem(self) -> None:
        base = SchemaSnapshot.from_rows(rows(100))
        report = check_drift(SchemaSnapshot.from_rows(rows(5000)), base)
        self.assertEqual(report.verdict, DriftVerdict.PASS)


class FieldShapeTests(unittest.TestCase):
    def test_a_vanished_critical_field_blocks_promotion(self) -> None:
        base = SchemaSnapshot.from_rows(rows(100))
        stripped = [{k: v for k, v in r.items() if k != "price"} for r in rows(100)]
        report = check_drift(SchemaSnapshot.from_rows(stripped), base, critical_fields=["price"])
        self.assertEqual(report.verdict, DriftVerdict.BLOCK_PROMOTION)
        self.assertEqual(report.blocking[0].field_name, "price")

    def test_a_vanished_non_critical_field_only_warns(self) -> None:
        base = SchemaSnapshot.from_rows(rows(100))
        stripped = [{k: v for k, v in r.items() if k != "price"} for r in rows(100)]
        report = check_drift(SchemaSnapshot.from_rows(stripped), base)
        self.assertEqual(report.verdict, DriftVerdict.WARN)
        self.assertTrue(report.verdict.allows_promotion)

    def test_a_new_field_is_reported_but_never_blocks(self) -> None:
        # Sites add things. Refusing to publish would be a self-inflicted outage.
        base = SchemaSnapshot.from_rows(rows(100))
        report = check_drift(SchemaSnapshot.from_rows(rows(100, extra={"rating": 4})), base)
        self.assertEqual(report.verdict, DriftVerdict.PASS)
        self.assertEqual(report.findings[0].kind, "field_added")

    def test_a_type_change_on_a_critical_field_blocks(self) -> None:
        # price: 10 -> "10 USD" is exactly the silent breakage this catches.
        base = SchemaSnapshot.from_rows(rows(100, price=10))
        report = check_drift(
            SchemaSnapshot.from_rows(rows(100, price="10 USD")),
            base,
            critical_fields=["price"],
        )
        self.assertEqual(report.verdict, DriftVerdict.BLOCK_PROMOTION)
        finding = report.blocking[0]
        self.assertEqual(finding.kind, "type_changed")
        self.assertEqual(finding.baseline, "int")
        self.assertEqual(finding.observed, "str")


class NullRateTests(unittest.TestCase):
    def test_a_field_going_empty_blocks_when_it_is_critical(self) -> None:
        base = SchemaSnapshot.from_rows(rows(100, title="Real title"))
        report = check_drift(
            SchemaSnapshot.from_rows(rows(100, title="")), base, critical_fields=["title"]
        )
        self.assertEqual(report.verdict, DriftVerdict.BLOCK_PROMOTION)
        self.assertEqual(report.blocking[0].kind, "null_rate_growth")

    def test_the_row_count_can_be_perfect_while_the_data_is_gone(self) -> None:
        # The failure mode in one test: nothing errored, the count is identical,
        # and the field a consumer depends on is empty.
        base = SchemaSnapshot.from_rows(rows(500, title="Real title"))
        current = SchemaSnapshot.from_rows(rows(500, title=""))
        self.assertEqual(current.record_count, base.record_count)
        report = check_drift(current, base, critical_fields=["title"])
        self.assertFalse(report.verdict.allows_promotion)


class ProvenanceTests(unittest.TestCase):
    def test_falling_back_to_a_heuristic_is_flagged(self) -> None:
        # The values may still look plausible. That is the point.
        base = SchemaSnapshot.from_rows(rows(100, source="json_ld"))
        report = check_drift(
            SchemaSnapshot.from_rows(rows(100, source="heuristic")),
            base,
            critical_fields=["title"],
        )
        self.assertEqual(report.verdict, DriftVerdict.BLOCK_PROMOTION)
        kinds = {f.kind for f in report.blocking}
        self.assertIn("provenance_degraded", kinds)

    def test_improving_provenance_is_not_a_problem(self) -> None:
        base = SchemaSnapshot.from_rows(rows(100, source="heuristic"))
        report = check_drift(
            SchemaSnapshot.from_rows(rows(100, source="json_ld")),
            base,
            critical_fields=["title"],
        )
        self.assertEqual(report.verdict, DriftVerdict.PASS)


class PaginationTests(unittest.TestCase):
    def test_stopping_on_our_own_ceiling_blocks_promotion(self) -> None:
        base = SchemaSnapshot.from_rows(rows(100))
        report = check_drift(SchemaSnapshot.from_rows(rows(100)), base, pagination_complete=False)
        self.assertEqual(report.verdict, DriftVerdict.BLOCK_PROMOTION)
        self.assertEqual(report.blocking[0].kind, "pagination_incomplete")

    def test_a_genuinely_exhausted_listing_is_fine(self) -> None:
        base = SchemaSnapshot.from_rows(rows(100))
        report = check_drift(SchemaSnapshot.from_rows(rows(100)), base, pagination_complete=True)
        self.assertEqual(report.verdict, DriftVerdict.PASS)


class SnapshotTests(unittest.TestCase):
    def test_a_snapshot_carries_shape_not_values(self) -> None:
        # Snapshots are stored beside datasets and compared across runs; keeping
        # values would spread whatever the dataset contains.
        snapshot = SchemaSnapshot.from_rows(rows(10, title="a secret title"))
        serialised = str(snapshot.to_dict())
        self.assertNotIn("a secret title", serialised)
        self.assertIn("title", serialised)

    def test_internal_columns_are_not_part_of_the_schema(self) -> None:
        snapshot = SchemaSnapshot.from_rows(rows(10))
        self.assertNotIn("_extractor_source", snapshot.fields)


if __name__ == "__main__":
    unittest.main()


class NullGrowthThresholdTests(unittest.TestCase):
    """A ratio alone is useless near zero."""

    def rows_with_null_rate(self, n, *, null_fraction):
        nulls = int(n * null_fraction)
        out = []
        for i in range(n):
            out.append({"t": "" if i < nulls else "value", "_extractor_source": {"t": "json_ld"}})
        return out

    def test_a_tenfold_move_on_a_tiny_base_does_not_block(self) -> None:
        # 0.1% -> 1% is 10x and 0.9 percentage points. Blocking here trains
        # operators to ignore the gate.
        base = SchemaSnapshot.from_rows(self.rows_with_null_rate(1000, null_fraction=0.001))
        current = SchemaSnapshot.from_rows(self.rows_with_null_rate(1000, null_fraction=0.01))
        report = check_drift(current, base, critical_fields=["t"])
        self.assertTrue(report.verdict.allows_promotion)

    def test_a_move_that_is_both_large_and_material_blocks(self) -> None:
        base = SchemaSnapshot.from_rows(self.rows_with_null_rate(1000, null_fraction=0.02))
        current = SchemaSnapshot.from_rows(self.rows_with_null_rate(1000, null_fraction=0.40))
        report = check_drift(current, base, critical_fields=["t"])
        self.assertEqual(report.verdict, DriftVerdict.BLOCK_PROMOTION)

    def test_a_large_absolute_move_with_a_small_ratio_does_not_block(self) -> None:
        # 40% -> 50% is +10pp but only 1.25x: a shift, not a collapse.
        base = SchemaSnapshot.from_rows(self.rows_with_null_rate(1000, null_fraction=0.40))
        current = SchemaSnapshot.from_rows(self.rows_with_null_rate(1000, null_fraction=0.50))
        report = check_drift(current, base, critical_fields=["t"])
        self.assertTrue(report.verdict.allows_promotion)


class ConfigurableThresholdTests(unittest.TestCase):
    def test_a_volatile_dataset_can_set_its_own_record_floor(self) -> None:
        # A listing that legitimately halves overnight and an archive that must
        # never shrink cannot share one threshold.
        base = SchemaSnapshot.from_rows(rows(1000))
        current = SchemaSnapshot.from_rows(rows(400))
        strict = check_drift(current, base)
        lenient = check_drift(current, base, min_record_ratio=0.3)
        self.assertEqual(strict.verdict, DriftVerdict.BLOCK_PROMOTION)
        self.assertTrue(lenient.verdict.allows_promotion)

    def test_a_project_can_supply_its_own_provenance_ordering(self) -> None:
        base = SchemaSnapshot.from_rows(rows(100, source="tier_a"))
        current = SchemaSnapshot.from_rows(rows(100, source="tier_b"))
        # Unknown names to the default ranking: no degradation is claimed.
        default = check_drift(current, base, critical_fields=["title"])
        self.assertTrue(default.verdict.allows_promotion)
        # With the project's own ordering, the fall is visible.
        ranked = check_drift(
            current, base, critical_fields=["title"], provenance_rank={"tier_a": 0, "tier_b": 9}
        )
        self.assertEqual(ranked.verdict, DriftVerdict.BLOCK_PROMOTION)

    def test_a_structured_api_outranks_json_ld(self) -> None:
        from web_scraper.publish.drift import DEFAULT_PROVENANCE_RANK

        self.assertLess(
            DEFAULT_PROVENANCE_RANK["structured_api"], DEFAULT_PROVENANCE_RANK["json_ld"]
        )
