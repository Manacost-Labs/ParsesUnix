"""Failure fingerprints: recognising a defense we have already defeated."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Verdict
from web_scraper.fingerprints import FingerprintStore, body_size_bucket, fingerprint_attempt
from web_scraper.fingerprints.model import redirect_pattern, transport_error_category

CHALLENGE_BODY = b"<html><title>Just a moment...</title>checking your browser</html>" + b" " * 900
CF_HEADERS = {
    "Server": "cloudflare",
    "cf-mitigated": "challenge",
    "Content-Type": "text/html; charset=utf-8",
}


def challenge(domain: str = "a.example", body: bytes = CHALLENGE_BODY):
    return fingerprint_attempt(
        verdict=Verdict.BLOCKED,
        status=403,
        body=body,
        headers=CF_HEADERS,
        domain=domain,
        url_class="article",
    )


class NormalizationTests(unittest.TestCase):
    def test_size_buckets_are_coarse(self) -> None:
        self.assertEqual(body_size_bucket(0), "empty")
        self.assertEqual(body_size_bucket(500), "tiny")
        self.assertEqual(body_size_bucket(5_000), "small")
        self.assertEqual(body_size_bucket(5_000_000), "large")
        # A few bytes of jitter must not change the bucket.
        self.assertEqual(body_size_bucket(5_000), body_size_bucket(5_050))

    def test_the_same_defense_on_two_sites_has_one_digest(self) -> None:
        # This is the whole point: knowledge must transfer between domains.
        a = challenge("a.example")
        b = challenge("b.example", CHALLENGE_BODY + b"<!-- ray 9f3 -->")
        self.assertEqual(a.digest, b.digest)

    def test_a_different_defense_has_a_different_digest(self) -> None:
        other = fingerprint_attempt(
            verdict=Verdict.BLOCKED,
            status=403,
            body=b"<html>datadome</html>" + b" " * 900,
            headers={"Server": "nginx", "Content-Type": "text/html"},
        )
        self.assertNotEqual(challenge().digest, other.digest)

    def test_the_label_is_readable(self) -> None:
        self.assertEqual(challenge().label, "BLOCKED/403/cf-challenge")

    def test_no_raw_page_text_or_header_values_are_stored(self) -> None:
        shape = fingerprint_attempt(
            verdict=Verdict.BLOCKED,
            status=403,
            body=b"secret-article-body just a moment " + b"x" * 900,
            headers={
                "Server": "cloudflare",
                "Set-Cookie": "sid=SUPERSECRET",
                "X-Served-By": "edge-9f3a",
            },
        )
        serialized = json.dumps(shape.to_dict())
        self.assertNotIn("SUPERSECRET", serialized)
        self.assertNotIn("secret-article-body", serialized)
        self.assertNotIn("edge-9f3a", serialized)  # per-request value, presence only
        self.assertIn("x-served-by=present", serialized)

    def test_transport_errors_are_bucketed(self) -> None:
        self.assertEqual(transport_error_category("The read operation timed out"), "timeout")
        self.assertEqual(transport_error_category("[Errno 8] nodename nor servname"), "dns")
        self.assertEqual(transport_error_category("Connection refused"), "connection_refused")
        self.assertIsNone(transport_error_category(None))

    def test_redirect_pattern_records_shape_not_urls(self) -> None:
        pattern = redirect_pattern(
            (
                {"from": "https://a.example/x", "to": "https://a.example/y", "status": 301},
                {"from": "https://a.example/y", "to": "https://b.example/z", "status": 302},
            )
        )
        self.assertEqual(pattern, "301s->302x")  # same-host, then cross-host
        self.assertNotIn("a.example", pattern)
        self.assertEqual(redirect_pattern(()), "none")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.clock = [1_000.0]
        self.store = FingerprintStore(
            Path(self.tempdir.name) / "f.sqlite3", now=lambda: self.clock[0]
        )

    def test_repeated_failures_accumulate(self) -> None:
        shape = challenge()
        self.store.record_failure(shape, route_id="direct_http")
        record = self.store.record_failure(shape, route_id="json_api:abc")
        self.assertEqual(record.count, 2)
        self.assertEqual(record.routes_seen, ("direct_http", "json_api:abc"))

    def test_no_hint_before_any_recovery(self) -> None:
        self.store.record_failure(challenge(), route_id="direct_http")
        self.assertIsNone(self.store.recovery_hint(challenge()))

    def test_knowledge_transfers_to_a_site_never_seen(self) -> None:
        learned_here = challenge("known.example")
        self.store.record_failure(learned_here, route_id="direct_http")
        self.store.record_recovery(learned_here.digest, route_id="dynamic")

        # A different domain, same defense: the hint applies.
        elsewhere = challenge("brand-new.example")
        hint = self.store.recovery_hint(elsewhere)
        self.assertIsNotNone(hint)
        self.assertEqual(hint.route_id, "dynamic")

    def test_the_most_successful_recovery_wins(self) -> None:
        shape = challenge()
        self.store.record_failure(shape, route_id="direct_http")
        self.store.record_recovery(shape.digest, route_id="dynamic")
        self.store.record_recovery(shape.digest, route_id="dynamic")
        self.store.record_recovery(shape.digest, route_id="stealthy")
        self.assertEqual(self.store.recovery_hint(shape).route_id, "dynamic")

    def test_recovery_for_an_unknown_digest_is_ignored(self) -> None:
        self.assertIsNone(self.store.record_recovery("nope", route_id="dynamic"))

    def test_state_survives_reopening(self) -> None:
        shape = challenge()
        self.store.record_failure(shape, route_id="direct_http")
        self.store.record_recovery(shape.digest, route_id="dynamic")
        reopened = FingerprintStore(self.store.path)
        self.assertEqual(reopened.recovery_hint(shape).route_id, "dynamic")

    def test_pruning_drops_stale_entries_but_keeps_learned_recoveries(self) -> None:
        stale = fingerprint_attempt(verdict=Verdict.PARSE_FAIL, status=200, body=b"x" * 900)
        valuable = challenge()
        self.store.record_failure(stale, route_id="direct_http")
        self.store.record_failure(valuable, route_id="direct_http")
        self.store.record_recovery(valuable.digest, route_id="dynamic")

        self.clock[0] += 200 * 86_400
        removed = self.store.prune(max_age_days=90)
        self.assertEqual(removed, 1)
        self.assertIsNone(self.store.get(stale.digest))
        self.assertIsNotNone(self.store.get(valuable.digest))  # recovery evidence kept

    def test_records_are_serializable_for_reporting(self) -> None:
        shape = challenge()
        self.store.record_failure(shape, route_id="direct_http")
        json.dumps([record.to_dict() for record in self.store.all_records()])


if __name__ == "__main__":
    unittest.main()
