from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.queue import QueueStore, UrlStatus, normalize_url  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_case_port_dot_segments_fragment_and_tracking(self) -> None:
        self.assertEqual(
            normalize_url("HTTPS://Example.COM:443/a/../b/?utm_source=x&z=1&a=2#frag"),
            "https://example.com/b/?a=2&z=1",
        )

    def test_non_default_port_is_kept(self) -> None:
        self.assertEqual(normalize_url("http://x.example:8080/p"), "http://x.example:8080/p")

    def test_distinct_resources_are_not_merged(self) -> None:
        self.assertNotEqual(normalize_url("https://x.example/a"), normalize_url("https://x.example/b"))
        self.assertNotEqual(
            normalize_url("https://x.example/p?id=1"), normalize_url("https://x.example/p?id=2")
        )


class QueueStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.clock = [1000.0]
        self.q = QueueStore(Path(self.tempdir.name) / "q.sqlite3", now=lambda: self.clock[0])

    def test_add_dedups_on_normalized_url(self) -> None:
        self.assertTrue(self.q.add("https://x.example/a"))
        self.assertFalse(self.q.add("https://x.example/a"))
        self.assertFalse(self.q.add("https://x.example/a?utm_source=ad"))  # same after normalize
        self.assertEqual(self.q.counts_by_status(), {"PENDING": 1})

    def test_claim_moves_to_in_progress_and_is_not_reclaimed(self) -> None:
        self.q.add_many(["https://x.example/a", "https://x.example/b"])
        first = self.q.claim_batch(10)
        self.assertEqual(len(first), 2)
        self.assertEqual(self.q.claim_batch(10), [])  # nothing left to claim

    def test_crash_resume_creates_no_duplicates(self) -> None:
        self.q.add_many(["https://x.example/a", "https://x.example/b"])
        self.q.claim_batch(10)  # simulate a run that then crashes mid-flight
        reset = self.q.reset_stale_in_progress()
        self.assertEqual(reset, 2)
        # Re-run picks the same rows back up; still exactly two URLs, no dupes.
        again = self.q.claim_batch(10)
        self.assertEqual({u.url for u in again}, {"https://x.example/a", "https://x.example/b"})
        self.assertEqual(sum(self.q.counts_by_status().values()), 2)

    def test_quarantine_and_dead_zone_are_recorded(self) -> None:
        self.q.add_many(["https://x.example/gone", "https://x.example/hard"])
        self.q.claim_batch(10)
        self.q.quarantine_url("https://x.example/gone", status_code=410)
        self.q.mark_dead_zone("https://x.example/hard", verdict_history=["BLOCKED", "BLOCKED", "BLOCKED"])
        self.assertEqual(self.q.get("https://x.example/gone").status, UrlStatus.QUARANTINED)
        self.assertEqual(self.q.get("https://x.example/hard").status, UrlStatus.DEAD_ZONE)
        self.assertEqual(self.q.quarantined()[0]["last_status"], 410)
        self.assertEqual(self.q.dead_zones()[0]["verdict_history"], ["BLOCKED", "BLOCKED", "BLOCKED"])

    def test_retry_is_not_claimable_until_not_before(self) -> None:
        self.q.add("https://x.example/a")
        self.q.claim_batch(10)
        self.q.schedule_retry("https://x.example/a", verdict="ORIGIN_DOWN", delay_seconds=100)
        self.assertEqual(self.q.pending_count(), 0)  # still embargoed
        self.clock[0] += 101
        self.assertEqual(self.q.pending_count(), 1)
        self.assertEqual(len(self.q.claim_batch(10)), 1)

    def test_no_silent_skips_every_url_has_a_status(self) -> None:
        urls = [f"https://x.example/{i}" for i in range(5)]
        self.q.add_many(urls)
        self.q.claim_batch(10)
        self.q.mark_done(urls[0], verdict="OK")
        self.q.mark_failed(urls[1], verdict="ACCESS_DENIED")
        self.q.quarantine_url(urls[2], status_code=404)
        self.q.mark_dead_zone(urls[3], verdict_history=["BLOCKED"])
        self.q.schedule_retry(urls[4], verdict="RATE_LIMITED", delay_seconds=0)
        statuses = {row.url: row.status for row in self.q.all_rows()}
        self.assertEqual(len(statuses), 5)
        self.assertNotIn(UrlStatus.IN_PROGRESS, set(statuses.values()))


    def test_reactivate_moves_done_back_to_pending(self) -> None:
        self.q.add("https://x.example/a")
        self.q.claim_batch(10)
        self.q.mark_done("https://x.example/a", verdict="OK")
        self.assertEqual(self.q.done_urls(), ["https://x.example/a"])
        moved = self.q.reactivate(["https://x.example/a"])
        self.assertEqual(moved, 1)
        self.assertEqual(self.q.get("https://x.example/a").status, UrlStatus.PENDING)

if __name__ == "__main__":
    unittest.main()
