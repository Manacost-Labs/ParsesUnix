from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Result, Verdict
from web_scraper.extract import run_quorum
from web_scraper.fetchers import RawResponse
from web_scraper.profiles import parse_profile
from web_scraper.publish import DatasetStore, validate_staging
from web_scraper.run import RunConfig, Runner


class RequiredFieldsByClassTests(unittest.TestCase):
    def test_accepted_quorum_only_profile_still_promotes(self) -> None:
        profile = parse_profile(
            {
                "site": "example.test",
                "authorization": {"public_data_only": True},
                "url_classes": {
                    "article": {
                        "match": r"^https://example\.test/articles/",
                        "validation": {"canary": "<article"},
                        "routes": {"primary": {"type": "direct_http", "level": "L1"}},
                        "extractors": [{"kind": "css", "fields": {"title": "h1::text"}}],
                        "quorum_fields": ["title"],
                    }
                },
            }
        )
        self.assertEqual(profile.url_classes["article"].required_fields, ())
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runner = Runner(
                RunConfig(profile_path=state / "profile.json", state_dir=state),
                profile=profile,
                gateway=object(),
            )
            runner.dataset.stage(
                "key",
                url="https://example.test/articles/1",
                data={"title": "A"},
                url_class="article",
            )
            decision = runner._promote()
            self.assertIsNotNone(decision)
            self.assertTrue(decision["ok"])
            self.assertEqual(runner.dataset.clean_rows()[0]["title"], "A")

    def test_completeness_is_not_diluted_by_other_classes(self) -> None:
        rows = [{"_url_class": "article", "title": "a"} for _ in range(99)]
        rows.append({"_url_class": "product", "title": "p"})
        decision = validate_staging(
            rows,
            required_fields=(),
            required_fields_by_class={"article": ("title",), "product": ("title", "price")},
            min_completeness_by_class={"article": 0.9, "product": 1.0},
            expected_count=None,
            min_completeness=0.9,
        )
        self.assertFalse(decision.ok)
        self.assertIn("product", decision.reason)

    def test_declared_noncritical_conflict_is_preserved_but_not_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DatasetStore(Path(directory) / "data.sqlite3")
            store.stage(
                "key",
                url="https://example.test/a",
                data={"title": "A", "note": "first"},
                conflict=True,
                conflict_fields=("note",),
            )
            decision = store.promote(required_fields=("title",), expected_count=1)
            self.assertTrue(decision.ok, decision.reason)
            self.assertEqual(store.clean_rows()[0]["_extractor_conflicts"], ["note"])

    def test_conflict_field_cannot_be_hidden_by_false_flag(self) -> None:
        decision = validate_staging(
            [{"title": "A", "_conflict": False, "_conflict_fields": ("title",)}],
            required_fields=("title",),
            expected_count=1,
            min_completeness=1.0,
        )
        self.assertFalse(decision.ok)

    def test_runner_stages_required_fields_beyond_quorum(self) -> None:
        profile = parse_profile(
            {
                "site": "example.test",
                "authorization": {"public_data_only": True},
                "url_classes": {
                    "article": {
                        "match": "^https://example\\.test/articles/",
                        "validation": {
                            "canary": "<h1",
                            "required_fields": ["title", "price"],
                        },
                        "routes": {"primary": {"type": "direct_http", "level": "L1"}},
                        "extractors": [
                            {
                                "kind": "css",
                                "fields": {"title": "h1::text", "price": ".price::text"},
                            }
                        ],
                        "quorum_fields": ["title"],
                    }
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runner = Runner(
                RunConfig(profile_path=state / "profile.json", state_dir=state),
                profile=profile,
                gateway=object(),
            )
            url = "https://example.test/articles/1"
            runner.queue.add(url, url_class="article")
            queued = runner.queue.get(url)
            assert queued is not None
            runner._handle_ok(
                queued,
                profile.url_classes["article"],
                Result(url=url, verdict=Verdict.OK),
                RawResponse(
                    url,
                    url,
                    200,
                    {"Content-Type": "text/html"},
                    b'<h1>A</h1><div class="price">5</div>',
                ),
            )
            self.assertEqual(runner.dataset.staged_rows()[0]["price"], "5")

    def test_legacy_staging_schema_is_upgraded_without_losing_clean_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE clean (natural_key TEXT PRIMARY KEY, url TEXT, data TEXT NOT NULL,
                        content_hash TEXT, updated_at REAL NOT NULL);
                    CREATE TABLE staging (natural_key TEXT PRIMARY KEY, url TEXT, data TEXT NOT NULL,
                        content_hash TEXT, conflict INTEGER NOT NULL DEFAULT 0, staged_at REAL NOT NULL);
                    CREATE TABLE lkg (natural_key TEXT PRIMARY KEY, url TEXT, data TEXT NOT NULL,
                        content_hash TEXT, saved_at REAL NOT NULL);
                    INSERT INTO clean VALUES ('old', 'https://example.test/old', '{"title":"old"}', NULL, 1);
                    """
                )
            store = DatasetStore(path)
            store.stage("new", url="https://example.test/new", data={"title": "new"})
            decision = store.promote(required_fields=("title",), expected_count=None)
            self.assertTrue(decision.ok)
            self.assertEqual({row["natural_key"] for row in store.clean_rows()}, {"old", "new"})

    def test_each_row_is_checked_against_its_own_class_requirements(self) -> None:
        decision = validate_staging(
            [
                {"_url_class": "article", "title": "article"},
                {"_url_class": "product", "title": "product"},
            ],
            required_fields=(),
            required_fields_by_class={"article": ("title",), "product": ("title", "price")},
            expected_count=None,
            min_completeness=1.0,
        )
        self.assertFalse(decision.ok)
        self.assertIn("completeness", decision.reason)

    def test_unknown_class_fails_closed(self) -> None:
        decision = validate_staging(
            [{"_url_class": "unknown", "title": "not enough"}],
            required_fields=(),
            required_fields_by_class={"article": ("title",)},
            expected_count=None,
            min_completeness=1.0,
        )
        self.assertFalse(decision.ok)
        self.assertIn("unknown url class", decision.reason)

    def test_conflict_blocks_first_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DatasetStore(Path(directory) / "data.sqlite3")
            store.stage(
                "key",
                url="https://example.test/a",
                data={"title": "A"},
                conflict=True,
                url_class="article",
                conflict_fields=("title",),
            )
            decision = store.promote(
                required_fields=(),
                required_fields_by_class={"article": ("title",)},
                expected_count=1,
                min_completeness=1.0,
            )
        self.assertFalse(decision.ok)
        self.assertIn("conflict", decision.reason)


class QuorumObservationIdentityTests(unittest.TestCase):
    def test_duplicate_spec_is_not_independent_agreement(self) -> None:
        body = b'{"score": 0}'
        result = run_quorum(
            body,
            headers={"Content-Type": "application/json"},
            extractors=[
                {"kind": "json", "fields": {"score": "score"}},
                {"kind": "json", "fields": {"score": "score"}},
            ],
            quorum_fields=["score"],
        )
        self.assertEqual(result.data["score"], 0)
        self.assertEqual(result.quorum["score"], "medium")

    def test_independent_equivalent_values_are_high(self) -> None:
        body = (
            '<meta property="a" content="2026-08-12T09:30:00Z">'
            '<meta property="b" content="Wed, 12 Aug 2026 09:30:00 GMT">'
        )
        result = run_quorum(
            body,
            extractors=[
                {"kind": "meta", "fields": {"published_at": "a"}},
                {"kind": "meta", "fields": {"published_at": "b"}},
            ],
            quorum_fields=["published_at"],
            field_kinds={"published_at": "date"},
        )
        self.assertEqual(result.quorum["published_at"], "high")

    def test_independent_disagreement_is_a_conflict_and_preserves_false(self) -> None:
        body = b'{"left": false, "right": 0}'
        result = run_quorum(
            body,
            headers={"Content-Type": "application/json"},
            extractors=[
                {"kind": "json", "fields": {"flag": "left"}},
                {"kind": "json", "fields": {"flag": "right"}},
            ],
            quorum_fields=["flag"],
        )
        self.assertEqual(result.data["flag"], False)
        self.assertEqual(result.quorum["flag"], "conflict")

    def test_typed_json_object_agreement_is_high(self) -> None:
        body = b'{"left": {"id": 1}, "right": {"id": 1}}'
        result = run_quorum(
            body,
            headers={"Content-Type": "application/json"},
            extractors=[
                {"kind": "json", "fields": {"item": "left"}},
                {"kind": "json", "fields": {"item": "right"}},
            ],
            quorum_fields=["item"],
        )
        self.assertEqual(result.data["item"], {"id": 1})
        self.assertEqual(result.quorum["item"], "high")


if __name__ == "__main__":
    unittest.main()
