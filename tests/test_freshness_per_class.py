"""Freshness is a per-class question, not a global one.

A site with hourly news and monthly guides has two different definitions of
"current". Judging both by the tighter window reports perfectly good guides as
stale; judging both by the looser one reports day-old news as current. The first
error makes a consumer re-fetch data that was fine; the second makes them act on
data that is not. Neither is acceptable, and one global number cannot avoid both.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.publish.availability import (
    DataStatus,
    build_availability,
    summarize_availability,
    summarize_by_url_class,
)

NOW = 1_000_000.0
HOUR = 3600.0

#: news: current within an hour. guides: current within a month.
WINDOWS = {"news": 1 * HOUR, "guide": 720 * HOUR}


def row(key, *, age_hours, url_class):
    return {
        "natural_key": key,
        "url": f"https://s/{key}",
        "updated_at": NOW - age_hours * HOUR,
        "data": {"title": key},
        "url_class": url_class,
    }


class PerClassTests(unittest.TestCase):
    def build(self, rows, **kw):
        kw.setdefault("max_age_by_url_class", WINDOWS)
        return build_availability(rows, now=NOW, max_age_seconds=1 * HOUR, verdicts_by_key={}, **kw)

    def test_a_day_old_guide_is_fresh_and_a_day_old_article_is_not(self) -> None:
        # The whole bug in one assertion: identical age, different verdicts,
        # because they are different kinds of thing.
        records = self.build(
            [
                row("a1", age_hours=24, url_class="news"),
                row("g1", age_hours=24, url_class="guide"),
            ]
        )
        by_key = {r.natural_key: r for r in records}
        self.assertEqual(by_key["g1"].status, DataStatus.FRESH)
        self.assertEqual(by_key["a1"].status, DataStatus.STALE_LKG)

    def test_the_old_global_window_would_have_called_the_guide_stale(self) -> None:
        # Reproduces the previous behaviour: min() across classes = 1 hour.
        records = build_availability(
            [row("g1", age_hours=24, url_class="guide")],
            now=NOW,
            max_age_seconds=1 * HOUR,
            verdicts_by_key={},
        )
        self.assertEqual(records[0].status, DataStatus.STALE_LKG, "this is what we were reporting")

    def test_an_unknown_class_falls_back_rather_than_going_unclassified(self) -> None:
        records = self.build([row("x", age_hours=0.5, url_class="mystery")])
        self.assertEqual(records[0].status, DataStatus.FRESH, "0.5h under the 1h fallback")

    def test_the_record_reports_which_window_judged_it(self) -> None:
        # An operator asking "why is this stale?" needs the class, not just the
        # verdict.
        records = self.build([row("a1", age_hours=24, url_class="news")])
        self.assertEqual(records[0].url_class, "news")
        self.assertEqual(records[0].to_dict()["url_class"], "news")


class ReportingTests(unittest.TestCase):
    def test_a_healthy_global_number_can_hide_a_dead_class(self) -> None:
        # 95% fresh overall, and the class that matters is 0% fresh.
        rows = [row(f"g{i}", age_hours=1, url_class="guide") for i in range(95)]
        rows += [row(f"a{i}", age_hours=48, url_class="news") for i in range(5)]
        records = build_availability(
            rows, now=NOW, max_age_seconds=1 * HOUR, max_age_by_url_class=WINDOWS
        )

        overall = summarize_availability(records)
        self.assertGreaterEqual(overall.fresh / overall.total, 0.95, "looks healthy")

        per_class = summarize_by_url_class(records)
        self.assertEqual(per_class["news"]["fresh"], 0, "and the news class is entirely stale")
        self.assertEqual(per_class["guide"]["fresh"], 95)

    def test_every_class_appears_in_the_breakdown(self) -> None:
        records = build_availability(
            [
                row("g", age_hours=1, url_class="guide"),
                row("a", age_hours=1, url_class="news"),
            ],
            now=NOW,
            max_age_seconds=1 * HOUR,
            max_age_by_url_class=WINDOWS,
        )
        self.assertEqual(set(summarize_by_url_class(records)), {"guide", "news"})

    def test_records_without_a_class_are_grouped_honestly(self) -> None:
        records = build_availability(
            [{"natural_key": "k", "url": "u", "updated_at": NOW, "data": {}}],
            now=NOW,
            max_age_seconds=HOUR,
        )
        self.assertIn("unknown", summarize_by_url_class(records))


class EvidenceTests(unittest.TestCase):
    def test_a_record_whose_last_attempt_failed_is_not_fresh(self) -> None:
        # Existing behaviour, pinned here because per-class windows must not
        # weaken it: age alone never makes a record current.
        records = build_availability(
            [row("k", age_hours=0, url_class="news")],
            now=NOW,
            max_age_seconds=HOUR,
            verdicts_by_key={"k": "BLOCKED"},
            max_age_by_url_class=WINDOWS,
        )
        self.assertEqual(records[0].status, DataStatus.STALE_LKG)
        self.assertEqual(records[0].fresh_failure_verdict, "BLOCKED")


if __name__ == "__main__":
    unittest.main()
