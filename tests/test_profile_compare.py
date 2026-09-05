from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.profile_engineering import comparison
from web_scraper.profile_engineering.cli import main
from web_scraper.profile_engineering.comparison import compare_corpus
from web_scraper.profile_engineering.corpus import AcceptanceCorpus, CorpusCase, load_corpus
from web_scraper.profiles.model import load_profile


class ProfileComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "profiles"
        self.package = self.root / "example.test"
        shutil.copytree(ROOT / "site_profiles" / "example.test", self.package)
        self.baseline = self.package / "baseline.yaml"
        shutil.copyfile(self.package / "profile.yaml", self.baseline)

    def test_cli_compares_the_same_offline_corpus_and_emits_case_deltas(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "--root",
                    str(self.root),
                    "compare",
                    "example.test",
                    "--baseline-profile",
                    str(self.baseline),
                ]
            )
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["cases"]), 7)
        self.assertEqual(
            set(report["cases"][0]),
            {"case_id", "url_class", "baseline", "candidate", "delta"},
        )
        self.assertIn("fields_found", report["cases"][0]["delta"])
        self.assertIn("records", report["cases"][0]["delta"])
        self.assertIn("conflicts", report["cases"][0]["delta"])

    def test_candidate_failure_is_nonzero_even_when_baseline_also_fails(self) -> None:
        bad = self.package / "bad.yaml"
        bad.write_text(
            self.baseline.read_text(encoding="utf-8").replace(
                'canary: "<article"', 'canary: "<missing"'
            ),
            encoding="utf-8",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "--root",
                    str(self.root),
                    "compare",
                    "example.test",
                    "--baseline-profile",
                    str(bad),
                    "--candidate-profile",
                    str(bad),
                ]
            )
        report = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertTrue(any(not item["candidate"]["passed"] for item in report["cases"]))

    def test_duplicate_case_ids_fail_closed(self) -> None:
        profile = load_profile(self.baseline)
        corpus = load_corpus(self.package / "corpus.yaml")
        duplicate = CorpusCase(**{**corpus.cases[0].__dict__, "fixture": corpus.cases[1].fixture})
        report = compare_corpus(
            profile,
            profile,
            AcceptanceCorpus(domain=corpus.domain, cases=(*corpus.cases, duplicate)),
            fixtures_root=self.package / "fixtures",
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("duplicate case id" in error for error in report.integrity_errors))

    def test_invalid_candidate_profile_returns_json_failure(self) -> None:
        invalid = self.package / "invalid.yaml"
        invalid.write_text("site: example.test\nurl_classes: not-a-mapping\n", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "--root",
                    str(self.root),
                    "compare",
                    "example.test",
                    "--baseline-profile",
                    str(self.baseline),
                    "--candidate-profile",
                    str(invalid),
                ]
            )
        report = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["cases"], [])

    def test_empty_corpus_fails_closed(self) -> None:
        profile = load_profile(self.baseline)
        report = compare_corpus(
            profile,
            profile,
            AcceptanceCorpus(domain="example.test"),
            fixtures_root=self.package / "fixtures",
        )
        self.assertFalse(report.ok)
        self.assertIn("corpus has no cases", report.integrity_errors)

    def test_missing_profile_class_coverage_fails_closed(self) -> None:
        profile = load_profile(self.baseline)
        corpus = AcceptanceCorpus(
            domain="example.test",
            cases=(
                CorpusCase(
                    id="unknown-class",
                    url_class="unknown",
                    kind=load_corpus(self.package / "corpus.yaml").cases[0].kind,
                    fixture="article-normal",
                ),
            ),
        )
        report = compare_corpus(profile, profile, corpus, fixtures_root=self.package / "fixtures")
        self.assertFalse(report.ok)
        self.assertTrue(
            any("missing corpus url_class" in error for error in report.integrity_errors)
        )

    def test_malformed_fixture_returns_json_failure(self) -> None:
        (self.package / "fixtures" / "article-normal" / "meta.json").write_text(
            "{bad", encoding="utf-8"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "--root",
                    str(self.root),
                    "compare",
                    "example.test",
                    "--baseline-profile",
                    str(self.baseline),
                ]
            )
        report = json.loads(output.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(report["ok"])
        self.assertEqual(report["cases"], [])

    def test_candidate_uses_the_fixture_snapshot_taken_before_baseline_runs(self) -> None:
        profile = load_profile(self.baseline)
        original = comparison.run_corpus
        calls = 0

        def mutate_after_baseline(*args, **kwargs):
            nonlocal calls
            outcome = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                (self.package / "fixtures" / "article-normal" / "body.html").write_text(
                    "<broken>", encoding="utf-8"
                )
            return outcome

        comparison.run_corpus = mutate_after_baseline
        self.addCleanup(lambda: setattr(comparison, "run_corpus", original))
        report = compare_corpus(
            profile,
            profile,
            load_corpus(self.package / "corpus.yaml"),
            fixtures_root=self.package / "fixtures",
        )
        self.assertEqual(calls, 2)
        self.assertTrue(report.ok, "candidate must receive the pre-baseline fixture snapshot")

    def test_missing_fixture_body_returns_json_failure(self) -> None:
        (self.package / "fixtures" / "article-normal" / "body.html").unlink()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "--root",
                    str(self.root),
                    "compare",
                    "example.test",
                    "--baseline-profile",
                    str(self.baseline),
                ]
            )
        report = json.loads(output.getvalue())
        self.assertNotEqual(code, 0)
        self.assertFalse(report["ok"])
        self.assertEqual(report["cases"], [])


if __name__ == "__main__":
    unittest.main()
