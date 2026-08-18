from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.freshness import FreshnessStore, content_hash  # noqa: E402


class ContentHashTests(unittest.TestCase):
    def test_whitespace_insensitive(self) -> None:
        self.assertEqual(content_hash(b"<h1>Hi</h1>"), content_hash(b"<h1>Hi</h1>   \n  "))
        self.assertNotEqual(content_hash(b"<h1>Hi</h1>"), content_hash(b"<h1>Bye</h1>"))


class FreshnessStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.clock = [1000.0]
        self.fs = FreshnessStore(Path(self.tempdir.name) / "f.sqlite3", now=lambda: self.clock[0])

    def test_unknown_url_is_due(self) -> None:
        self.assertTrue(self.fs.is_due("https://x.example/a"))

    def test_conditional_headers_after_first_record(self) -> None:
        self.fs.record_result(
            "https://x.example/a",
            headers={"ETag": '"abc"', "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"},
            body=b"<html>1</html>",
        )
        headers = self.fs.conditional_headers("https://x.example/a")
        self.assertEqual(headers["If-None-Match"], '"abc"')
        self.assertIn("If-Modified-Since", headers)

    def test_interval_widens_when_unchanged_and_resets_on_change(self) -> None:
        url = "https://x.example/a"
        changed1, _ = self.fs.record_result(url, body=b"<html>1</html>")
        self.assertTrue(changed1)  # first fetch counts as changed (new data)
        base = self.fs.get(url).interval_seconds
        self.clock[0] += 10_000
        changed2, _ = self.fs.record_result(url, body=b"<html>1</html>")  # same content
        self.assertFalse(changed2)
        self.assertGreater(self.fs.get(url).interval_seconds, base)  # widened
        self.clock[0] += 10_000_000
        changed3, _ = self.fs.record_result(url, body=b"<html>2</html>")  # different
        self.assertTrue(changed3)
        self.assertEqual(self.fs.get(url).interval_seconds, FreshnessStore.MIN_INTERVAL)  # reset

    def test_not_modified_keeps_hash_and_is_not_changed(self) -> None:
        url = "https://x.example/a"
        self.fs.record_result(url, body=b"<html>1</html>")
        h = self.fs.get(url).content_hash
        changed, new_hash = self.fs.record_result(url, not_modified=True)
        self.assertFalse(changed)
        self.assertEqual(new_hash, h)

    def test_due_respects_interval(self) -> None:
        url = "https://x.example/a"
        self.fs.record_result(url, body=b"<html>1</html>")
        self.assertFalse(self.fs.is_due(url))  # just checked
        self.clock[0] += FreshnessStore.MIN_INTERVAL + 1
        self.assertTrue(self.fs.is_due(url))
        self.assertTrue(self.fs.is_due(url, full_review=True))


if __name__ == "__main__":
    unittest.main()
