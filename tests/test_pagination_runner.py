import asyncio
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from web_scraper.pagination.runner import Cursor, Limits, Page, collect_pages


class PaginationRunnerTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    async def collect(self, pages, **kwargs):
        return await collect_pages(
            scope_id="listing",
            first_token="first",
            fetch_page=lambda token: asyncio.sleep(0, result=pages[token]),
            record_key=str,
            **kwargs,
        )

    def test_three_pages_complete_with_expected_count(self):
        result = self.run_async(
            self.collect(
                {
                    "first": Page(("a",), "opaque:2", expected_count=3),
                    "opaque:2": Page(("b",), "opaque:9", expected_count=3),
                    "opaque:9": Page(("c",), exhausted=True, expected_count=3),
                }
            )
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "complete")
        self.assertEqual(result.cursor.records, ("a", "b", "c"))
        self.assertEqual(result.cursor.visited_tokens, ("first", "opaque:2", "opaque:9"))

    def test_stable_first_seen_deduplication(self):
        result = self.run_async(
            self.collect(
                {"first": Page(("a", "b"), "next"), "next": Page(("b", "c"), exhausted=True)}
            )
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.cursor.records, ("a", "b", "c"))

    def test_repeat_cursor_is_incomplete(self):
        result = self.run_async(self.collect({"first": Page(("a",), "first")}))
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "loop")

    def test_empty_continuation_and_missing_next_are_incomplete(self):
        empty = self.run_async(self.collect({"first": Page((), "next")}))
        missing = self.run_async(self.collect({"first": Page(("a",))}))
        self.assertEqual(empty.stop_reason, "no_new_records")
        self.assertEqual(missing.stop_reason, "missing_next")

    def test_expected_count_change_or_smaller_count_rejects_page(self):
        changed = self.run_async(
            self.collect(
                {
                    "first": Page(("a",), "next", expected_count=2),
                    "next": Page(("b",), exhausted=True, expected_count=3),
                }
            )
        )
        smaller = self.run_async(
            self.collect({"first": Page(("a", "b"), exhausted=True, expected_count=1)})
        )
        self.assertEqual(changed.stop_reason, "invalid_page")
        self.assertEqual(smaller.stop_reason, "invalid_page")

    def test_exhausted_count_mismatch_never_completes(self):
        result = self.run_async(
            self.collect({"first": Page(("a",), exhausted=True, expected_count=2)})
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "count_mismatch")

    def test_malformed_page_token_records_and_record_key_are_invalid(self):
        bad_records = self.run_async(self.collect({"first": Page(["a"], exhausted=True)}))
        bad_token = self.run_async(self.collect({"first": Page(("a",), next_token="")}))
        key_exception = self.run_async(
            collect_pages(
                scope_id="listing",
                first_token="first",
                fetch_page=lambda _: asyncio.sleep(0, result=Page(("a",), exhausted=True)),
                record_key=lambda _: (_ for _ in ()).throw(RuntimeError("secret")),
            )
        )
        self.assertEqual(bad_records.stop_reason, "invalid_page")
        self.assertEqual(bad_token.stop_reason, "invalid_page")
        self.assertEqual(key_exception.stop_reason, "invalid_page")

    def test_max_requests_and_records_do_not_commit_half_page(self):
        capped_requests = self.run_async(
            self.collect(
                {"first": Page(("a",), "next"), "next": Page(("b",), exhausted=True)},
                limits=Limits(max_requests=1),
            )
        )
        capped_records = self.run_async(
            self.collect({"first": Page(("a", "b"), exhausted=True)}, limits=Limits(max_records=1))
        )
        self.assertEqual(capped_requests.stop_reason, "max_requests")
        self.assertEqual(capped_records.stop_reason, "max_records")
        self.assertEqual(capped_records.cursor.records, ())

    def test_fetch_failure_retains_pending_token_for_resume(self):
        async def fetch(token):
            if token == "next":
                raise OSError("provider detail")
            return Page(("a",), "next")

        result = self.run_async(
            collect_pages(scope_id="listing", first_token="first", fetch_page=fetch, record_key=str)
        )
        self.assertEqual(result.stop_reason, "fetch_error")
        self.assertEqual(result.requests_this_run, 2)
        self.assertEqual(result.cursor.next_token, "next")

        resumed = self.run_async(
            collect_pages(
                scope_id="listing",
                first_token="ignored",
                fetch_page=lambda token: asyncio.sleep(0, result=Page(("b",), exhausted=True)),
                record_key=str,
                resume=result.cursor,
            )
        )
        self.assertTrue(resumed.complete)
        self.assertEqual(resumed.cursor.records, ("a", "b"))

    def test_timeout_counts_logical_attempt(self):
        async def slow(_):
            await asyncio.sleep(0.01)
            return Page(("a",), exhausted=True)

        result = self.run_async(
            collect_pages(
                scope_id="listing",
                first_token="first",
                fetch_page=slow,
                record_key=str,
                limits=Limits(max_seconds=0.0001),
            )
        )
        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(result.requests_this_run, 1)

    def test_checkpoint_initial_accepted_and_cancellation_propagates(self):
        checkpoints = []

        async def checkpoint(cursor):
            checkpoints.append(cursor)

        completed = self.run_async(
            self.collect({"first": Page(("a",), exhausted=True)}, checkpoint=checkpoint)
        )
        self.assertTrue(completed.complete)
        self.assertEqual(len(checkpoints), 2)

        async def cancelled(_):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            self.run_async(
                collect_pages(
                    scope_id="listing", first_token="first", fetch_page=cancelled, record_key=str
                )
            )

    def test_checkpoint_failure_propagates(self):
        async def broken(_):
            raise OSError("disk full")

        with self.assertRaises(OSError):
            self.run_async(self.collect({"first": Page(("a",), exhausted=True)}, checkpoint=broken))

    def test_checkpoint_callbacks_share_the_end_to_end_time_budget(self):
        fetched = []

        async def fetch(token):
            fetched.append(token)
            return Page(("a",), exhausted=True)

        async def slow_checkpoint(_):
            await asyncio.sleep(0.01)

        initial_timeout = self.run_async(
            collect_pages(
                scope_id="listing",
                first_token="first",
                fetch_page=fetch,
                record_key=str,
                checkpoint=slow_checkpoint,
                limits=Limits(max_seconds=0.0001),
            )
        )
        self.assertEqual(initial_timeout.stop_reason, "checkpoint_timeout")
        self.assertEqual(fetched, [])

        calls = 0

        async def first_fast_then_slow(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                await asyncio.sleep(0.01)

        post_page_timeout = self.run_async(
            self.collect(
                {"first": Page(("a",), exhausted=True)},
                checkpoint=first_fast_then_slow,
                limits=Limits(max_seconds=0.0001),
            )
        )
        self.assertEqual(post_page_timeout.stop_reason, "checkpoint_timeout")
        self.assertFalse(post_page_timeout.complete)

    def test_elapsed_terminal_checkpoint_cannot_report_complete(self):
        now = 0.0
        checkpoints = 0

        def clock():
            return now

        async def checkpoint(_):
            nonlocal now, checkpoints
            checkpoints += 1
            if checkpoints == 2:
                now = 2.0

        result = self.run_async(
            self.collect(
                {"first": Page(("a",), exhausted=True)},
                checkpoint=checkpoint,
                clock=clock,
                limits=Limits(max_seconds=1),
            )
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "max_seconds")

    def test_page_processing_cannot_bypass_terminal_time_budget_without_checkpoint(self):
        now = 0.0

        def clock():
            return now

        def slow_key(value):
            nonlocal now
            now = 2.0
            return value

        result = self.run_async(
            collect_pages(
                scope_id="listing",
                first_token="first",
                fetch_page=lambda _: asyncio.sleep(0, result=Page(("a",), exhausted=True)),
                record_key=slow_key,
                clock=clock,
                limits=Limits(max_seconds=1),
            )
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "max_seconds")

    def test_checkpoint_storage_timeout_propagates_without_fetching(self):
        fetched = []

        async def fetch(token):
            fetched.append(token)
            return Page(("a",), exhausted=True)

        async def storage_timeout(_):
            raise TimeoutError("storage")

        with self.assertRaisesRegex(TimeoutError, "storage"):
            self.run_async(
                collect_pages(
                    scope_id="listing",
                    first_token="first",
                    fetch_page=fetch,
                    record_key=str,
                    checkpoint=storage_timeout,
                )
            )
        self.assertEqual(fetched, [])

    def test_malformed_resume_and_limits_are_rejected(self):
        with self.assertRaises(ValueError):
            self.run_async(
                self.collect(
                    {"first": Page(("a",), exhausted=True)},
                    resume=Cursor(
                        "listing", "first", visited_tokens=("first",), pages_completed=True
                    ),
                )
            )
        with self.assertRaises(ValueError):
            self.run_async(
                self.collect(
                    {"first": Page(("a",), exhausted=True)},
                    resume=Cursor("listing", "first", records=("a", "b"), expected_count=1),
                )
            )
        with self.assertRaises(ValueError):
            self.run_async(
                self.collect(
                    {"first": Page(("a",), exhausted=True)}, resume=Cursor("listing", None)
                )
            )
        for seconds in (True, math.inf, math.nan):
            with self.assertRaises(ValueError):
                Limits(max_seconds=seconds)
        for value in (True, 0, -1):
            with self.assertRaises(ValueError):
                Limits(max_requests=value)

    def test_invalid_input_and_page_booleans_are_never_accepted(self):
        invalid_page = self.run_async(self.collect({"first": Page(("a",), exhausted=1)}))
        invalid_count = self.run_async(
            self.collect({"first": Page(("a",), exhausted=True, expected_count=True)})
        )
        self.assertEqual(invalid_page.stop_reason, "invalid_page")
        self.assertEqual(invalid_count.stop_reason, "invalid_page")
        with self.assertRaises(ValueError):
            self.run_async(
                collect_pages(
                    scope_id="",
                    first_token="first",
                    fetch_page=lambda _: asyncio.sleep(0, result=Page((), exhausted=True)),
                    record_key=str,
                )
            )
        with self.assertRaises(ValueError):
            self.run_async(
                collect_pages(
                    scope_id="listing",
                    first_token="x" * 4097,
                    fetch_page=lambda _: asyncio.sleep(0, result=Page((), exhausted=True)),
                    record_key=str,
                )
            )
