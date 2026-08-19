"""The record that lets someone explain a dataset six months later."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.observability.manifest import build_manifest, stable_hash
from web_scraper.providers.scrape_do import ScrapeDoProvider


class HashTests(unittest.TestCase):
    def test_the_same_config_hashes_the_same_regardless_of_key_order(self) -> None:
        self.assertEqual(
            stable_hash({"a": 1, "b": 2}),
            stable_hash({"b": 2, "a": 1}),
            "otherwise two identical runs would look different",
        )

    def test_a_changed_value_changes_the_hash(self) -> None:
        self.assertNotEqual(stable_hash({"limit": 100}), stable_hash({"limit": 200}))


class ManifestTests(unittest.TestCase):
    def manifest(self, **kw):
        kw.setdefault("run_id", "run-1")
        kw.setdefault("started_at", "2026-08-19T10:00:00Z")
        return build_manifest(**kw)

    def test_it_records_the_commit_it_ran_from(self) -> None:
        manifest = self.manifest(repo=ROOT)
        self.assertIsNotNone(manifest.git_commit)
        assert manifest.git_commit is not None
        self.assertEqual(len(manifest.git_commit), 40)

    def test_a_missing_repository_does_not_stop_a_run(self) -> None:
        # A manifest without a commit beats a run that refuses to start.
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self.manifest(repo=Path(tmp))
            self.assertIsNone(manifest.git_commit)
            self.assertIn("unknown", manifest.explain())

    def test_it_records_when_each_provider_contract_was_last_verified(self) -> None:
        # A vendor changing its response format is invisible at runtime; this is
        # what turns "the costs stopped adding up" into a starting point.
        manifest = self.manifest(providers=[ScrapeDoProvider(token="x")])
        fingerprint = manifest.providers[0]
        self.assertEqual(fingerprint.provider, "scrape.do")
        self.assertEqual(fingerprint.docs_verified_at, "2026-08-19")
        self.assertGreater(len(fingerprint.strategies), 0)

    def test_it_carries_no_credentials(self) -> None:
        manifest = self.manifest(
            providers=[ScrapeDoProvider(token="SUPERSECRETTOKEN")],
            config={"budget": {"daily_credit_limit": "100"}},
        )
        serialised = json.dumps(manifest.to_dict())
        self.assertNotIn("SUPERSECRETTOKEN", serialised)

    def test_profiles_are_hashed_not_embedded(self) -> None:
        manifest = self.manifest(profiles={"site.example": {"site": "site.example"}})
        self.assertIn("site.example", manifest.profile_hashes)
        self.assertTrue(manifest.profile_hashes["site.example"].startswith("sha256:"))

    def test_it_round_trips_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = self.manifest(input_url_count=9000, daily_credit_limit=Decimal("500"))
            path = manifest.write(Path(tmp) / "manifest.json")
            loaded = json.loads(path.read_text())
            self.assertEqual(loaded["input_url_count"], 9000)
            self.assertEqual(loaded["daily_credit_limit"], "500")

    def test_a_free_run_says_so_rather_than_showing_zero(self) -> None:
        self.assertIn("free run", self.manifest().explain())


if __name__ == "__main__":
    unittest.main()
