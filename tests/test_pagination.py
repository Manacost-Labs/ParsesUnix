"""Pagination: stop for a stated reason, and know whether the listing was whole."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.pagination import (
    PaginationStrategy,
    StopReason,
    TraversalBudget,
    TraversalState,
    detect_strategy,
)


class DetectionTests(unittest.TestCase):
    def test_query_parameters_identify_the_strategy(self) -> None:
        cases = {
            "https://x.example/a?cursor=abc": PaginationStrategy.CURSOR,
            "https://x.example/a?after=abc": PaginationStrategy.CURSOR,
            "https://x.example/a?offset=20&limit=20": PaginationStrategy.OFFSET,
            "https://x.example/a?page=3": PaginationStrategy.PAGE,
            "https://x.example/a": PaginationStrategy.NONE,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_strategy(url=url).strategy, expected)

    def test_a_cursor_outranks_an_offset(self) -> None:
        # A site offering a cursor means it to be used.
        plan = detect_strategy(url="https://x.example/a?offset=20&cursor=abc")
        self.assertEqual(plan.strategy, PaginationStrategy.CURSOR)

    def test_a_payload_cursor_is_found(self) -> None:
        plan = detect_strategy(json_payload={"items": [], "next_cursor": "abc"})
        self.assertEqual(plan.strategy, PaginationStrategy.CURSOR)
        self.assertEqual(plan.parameter, "next_cursor")

    def test_an_empty_payload_cursor_is_not_a_cursor(self) -> None:
        plan = detect_strategy(json_payload={"items": [], "next_cursor": None})
        self.assertEqual(plan.strategy, PaginationStrategy.NONE)

    def test_markup_signals(self) -> None:
        self.assertEqual(
            detect_strategy(body=b'<link rel="next" href="/p2">').strategy, PaginationStrategy.PAGE
        )
        self.assertEqual(
            detect_strategy(body=b"<div data-infinite-scroll></div>").strategy,
            PaginationStrategy.INFINITE_SCROLL,
        )

    def test_the_plan_explains_itself(self) -> None:
        self.assertTrue(detect_strategy(url="https://x.example/a?page=2").evidence)


class TerminationTests(unittest.TestCase):
    def state(self, **kwargs: object) -> TraversalState:
        return TraversalState(budget=TraversalBudget(**kwargs))  # type: ignore[arg-type]

    def test_a_finished_listing_is_complete(self) -> None:
        state = self.state()
        state.observe_page(["a", "b"])
        state.finish()
        self.assertEqual(state.stop_reason, StopReason.COMPLETE)
        self.assertTrue(state.complete)

    def test_pages_adding_nothing_new_end_the_traversal(self) -> None:
        state = self.state(empty_streak=2)
        self.assertIsNone(state.observe_page(["a", "b"]))
        self.assertIsNone(state.observe_page(["a"]))  # first empty page
        self.assertEqual(state.observe_page(["b"]), StopReason.NO_NEW_RECORDS)
        self.assertTrue(state.complete)

    def test_a_repeated_cursor_is_a_bug_not_an_ending(self) -> None:
        # Cursor pagination that loops would otherwise never terminate.
        state = self.state()
        state.observe_page(["a"], cursor="c1")
        reason = state.observe_page(["b"], cursor="c1")
        self.assertEqual(reason, StopReason.REPEATED_CURSOR)
        self.assertFalse(state.stop_reason.is_exhaustive)
        self.assertFalse(state.complete)

    def test_our_own_ceilings_are_not_completion(self) -> None:
        for kwargs, keys, expected in (
            ({"max_pages": 2}, ["a"], StopReason.MAX_PAGES),
            ({"max_records": 2}, ["a", "b", "c"], StopReason.MAX_RECORDS),
        ):
            with self.subTest(expected=expected):
                state = self.state(**kwargs)
                reason = None
                for index in range(5):
                    # distinct keys each page, so only the ceiling can stop us
                    reason = state.observe_page([f"{key}-{index}" for key in keys])
                    if reason:
                        break
                self.assertEqual(reason, expected)
                self.assertFalse(state.complete, "a self-imposed limit is not completeness")

    def test_the_time_budget_stops_the_traversal(self) -> None:
        state = self.state(time_budget_seconds=10.0)
        state.elapsed_seconds = 11.0
        self.assertEqual(state.observe_page(["a"]), StopReason.TIME_BUDGET)
        self.assertFalse(state.complete)

    def test_infinite_scroll_is_bounded(self) -> None:
        state = self.state(max_scrolls=3, empty_streak=99)
        reason = None
        for index in range(10):
            reason = state.observe_scroll([f"item-{index}"])
            if reason:
                break
        self.assertEqual(reason, StopReason.MAX_SCROLLS)
        self.assertEqual(state.scrolls, 3)
        self.assertFalse(state.complete)

    def test_a_declared_count_that_is_not_reached_is_not_complete(self) -> None:
        # "Found 1247 results" and we collected 300: an incident, not a success.
        state = TraversalState(expected_count=1247)
        state.observe_page([f"item-{i}" for i in range(300)])
        state.finish()
        self.assertEqual(state.records, 300)
        self.assertFalse(state.complete)

    def test_reaching_the_declared_count_ends_it(self) -> None:
        state = TraversalState(expected_count=2)
        self.assertEqual(state.observe_page(["a", "b"]), StopReason.EXPECTED_COUNT_REACHED)
        self.assertTrue(state.complete)

    def test_duplicates_across_pages_are_not_counted_twice(self) -> None:
        state = self.state(empty_streak=99)
        state.observe_page(["a", "b"])
        state.observe_page(["b", "c"])
        self.assertEqual(state.records, 3)

    def test_a_traversal_always_reports_why_it_stopped(self) -> None:
        state = self.state()
        state.observe_page(["a"])
        payload = state.finish() and state.to_dict()
        self.assertIsNotNone(payload["stop_reason"])

    def test_invalid_budgets_are_rejected(self) -> None:
        for kwargs in ({"max_pages": 0}, {"max_records": 0}, {"time_budget_seconds": 0}):
            with self.assertRaises(ValueError):
                TraversalBudget(**kwargs)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()


class RealWorldShapeTests(unittest.TestCase):
    """Shapes taken from the acceptance corpus, which a query-only detector missed."""

    def test_a_page_number_in_the_path_is_pagination(self) -> None:
        # The most common shape on the web; /page/2/ carries no query at all.
        for url in (
            "https://quotes.toscrape.com/page/2/",
            "https://books.toscrape.com/catalogue/page-2.html",
            "https://blog.example/p/7",
        ):
            with self.subTest(url=url):
                plan = detect_strategy(url=url)
                self.assertEqual(plan.strategy, PaginationStrategy.PAGE)
                self.assertIn("path", plan.evidence[0])

    def test_a_next_control_without_rel_next_is_pagination(self) -> None:
        # books.toscrape.com uses <li class="next"> and never sets rel="next".
        body = b'<ul class="pager"><li class="next"><a href="page-2.html">next</a></li></ul>'
        plan = detect_strategy(body=body)
        self.assertEqual(plan.strategy, PaginationStrategy.PAGE)

    def test_a_scroll_handler_counts_as_infinite_scroll(self) -> None:
        body = b"<script>$(window).scroll(function(){ loadQuotes(); });</script>"
        self.assertEqual(detect_strategy(body=body).strategy, PaginationStrategy.INFINITE_SCROLL)

    def test_an_ordinary_page_is_still_not_paginated(self) -> None:
        body = b"<html><body><article><h1>One page</h1><p>text</p></article></body></html>"
        plan = detect_strategy(url="https://x.example/articles/one", body=body)
        self.assertEqual(plan.strategy, PaginationStrategy.NONE)
