from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.fetchers import RawResponse  # noqa: E402
from web_scraper.storage.redaction import redact_headers  # noqa: E402
from web_scraper.storage.snapshots import SnapshotStore  # noqa: E402


class RedactionTests(unittest.TestCase):
    def test_sensitive_headers_are_masked_but_recorded(self) -> None:
        cleaned = redact_headers(
            {"Set-Cookie": "sid=secret", "AUTHORIZATION": "Bearer x", "Content-Type": "text/html"}
        )
        self.assertEqual(cleaned["Set-Cookie"], "[REDACTED]")
        self.assertEqual(cleaned["AUTHORIZATION"], "[REDACTED]")
        self.assertEqual(cleaned["Content-Type"], "text/html")


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SnapshotStore(self.tempdir.name, now=lambda: 1_755_000_000.0)

    def response(self) -> RawResponse:
        return RawResponse(
            requested_url="https://demo-news.example/a",
            final_url="https://demo-news.example/a",
            status=200,
            headers={"Set-Cookie": "sid=secret-value", "Content-Type": "text/html"},
            body=b"<html>page</html>",
            elapsed_ms=12,
        )

    def secret_body_response(self) -> RawResponse:
        return RawResponse(
            requested_url="https://demo-news.example/a?api_key=SEEKRIT9999",
            final_url="https://demo-news.example/a?api_key=SEEKRIT9999",
            status=200,
            headers={"Set-Cookie": "sid=secret-value", "Content-Type": "text/html"},
            body=b'<html><script>var token="Bearer abcdef1234567890";</script></html>',
            truncated=True,
            elapsed_ms=12,
        )

    def test_snapshot_persists_body_and_redacts_secrets(self) -> None:
        meta_path = self.store.save(
            url="https://demo-news.example/a", attempt_index=1, response=self.response(), verdict="OK"
        )
        meta = json.loads(meta_path.read_text())
        self.assertEqual(meta["headers"]["Set-Cookie"], "[REDACTED]")
        self.assertNotIn("secret-value", meta_path.read_text())
        body = (meta_path.parent / meta["body_file"]).read_bytes()
        self.assertEqual(body, b"<html>page</html>")
        self.assertEqual(meta["body_sha256"], hashlib.sha256(body).hexdigest())

    def test_snapshot_redacts_body_secrets_and_url_query_and_records_truncated(self) -> None:
        meta_path = self.store.save(
            url="https://demo-news.example/a?api_key=SEEKRIT9999",
            attempt_index=1,
            response=self.secret_body_response(),
            verdict="OK",
        )
        raw_meta = meta_path.read_text()
        self.assertNotIn("SEEKRIT9999", raw_meta)  # query value masked in meta
        meta = json.loads(raw_meta)
        self.assertTrue(meta["truncated"])
        body = (meta_path.parent / meta["body_file"]).read_bytes()
        self.assertNotIn(b"abcdef1234567890", body)  # bearer token masked in body
        self.assertEqual(meta["body_sha256"], hashlib.sha256(body).hexdigest())

    def test_snapshots_are_grouped_by_url_and_indexed(self) -> None:
        self.store.save(
            url="https://demo-news.example/a", attempt_index=1, response=self.response(), verdict="OK"
        )
        self.store.save(
            url="https://demo-news.example/a", attempt_index=2, response=self.response(), verdict="SOFT_BLOCK"
        )
        key = hashlib.sha256(b"https://demo-news.example/a").hexdigest()[:16]
        directory = Path(self.tempdir.name) / key
        self.assertEqual(len(list(directory.glob("*.meta.json"))), 2)
        index_lines = (Path(self.tempdir.name) / "index.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(index_lines), 2)
        self.assertEqual(json.loads(index_lines[0])["url"], "https://demo-news.example/a")

    def test_prune_by_total_bytes_keeps_newest(self) -> None:
        times = iter([1_000.0, 2_000.0, 3_000.0])
        store = SnapshotStore(self.tempdir.name, now=lambda: next(times, 3_000.0))
        big = RawResponse(
            requested_url="https://x.example/a",
            final_url="https://x.example/a",
            status=200,
            headers={},
            body=b"x" * 5_000,
        )
        for i in range(3):
            store.save(url=f"https://x.example/{i}", attempt_index=1, response=big, verdict="OK")
        removed = store.prune(max_total_bytes=8_000)
        self.assertTrue(removed)  # at least the oldest was pruned
        remaining = list(Path(self.tempdir.name).glob("*/*.meta.json"))
        self.assertLess(len(remaining), 3)


class GatewaySnapshotIntegrationTests(unittest.TestCase):
    def test_gateway_writes_a_snapshot_per_attempt(self) -> None:
        from test_gateway import PAGE_URL, FakeTransport, RecordingPacer, fixture_response, make_profile
        from web_scraper.fetchers import FetchGateway

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = SnapshotStore(tempdir.name, now=lambda: 1_755_000_000.0)
        profile = make_profile(
            {"type": "direct_http", "level": "L1"}, [{"type": "dynamic", "level": "L2"}]
        )
        l1 = FakeTransport({PAGE_URL: [fixture_response("soft-block")]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("success")]})

        def provider(route, url_class, url):
            return {"L1": l1, "L2": l2}[route.level.value]

        gateway = FetchGateway(
            profile, transport_provider=provider, pacer=RecordingPacer(), snapshots=store
        )
        outcome = gateway.fetch_url(PAGE_URL)
        self.assertEqual(len(outcome.snapshot_paths), 2)
        for path in outcome.snapshot_paths:
            self.assertTrue(Path(path).exists())


if __name__ == "__main__":
    unittest.main()
