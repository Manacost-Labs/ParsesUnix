from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.regression import compare_bodies, json_paths  # noqa: E402
from web_scraper.regression.cli import main as regress_main  # noqa: E402
from web_scraper.regression.detect import SEVERITY_CRITICAL, SEVERITY_NONE, SEVERITY_WARNING  # noqa: E402

HTML_HEADERS = {"Content-Type": "text/html"}
JSON_HEADERS = {"Content-Type": "application/json"}

EXTRACTORS = [
    {"kind": "json_ld", "schema_type": "Article"},
    {"kind": "css", "fields": {"title": "h1.t::text"}},
    {"kind": "heuristic"},
]

FILLER = b"<p>" + b"word " * 200 + b"</p>"
BASELINE = (
    b'<html><head><title>Page</title>'
    b'<script type="application/ld+json">{"@type":"Article","headline":"Real Title"}</script>'
    b'<link rel="canonical" href="https://x.example/a">'
    b"</head><body>" + b'<h1 class="t">Real Title</h1>' + FILLER + b"</body></html>"
)


def compare(current: bytes, *, fields=("title",), baseline: bytes = BASELINE):
    return compare_bodies(
        url="https://x.example/a",
        baseline_body=baseline,
        current_body=current,
        baseline_headers=HTML_HEADERS,
        current_headers=HTML_HEADERS,
        extractors=EXTRACTORS,
        fields=list(fields),
    )


class JsonPathTests(unittest.TestCase):
    def test_leaf_paths_use_bracket_notation_for_lists(self) -> None:
        paths = json_paths({"data": {"players": [{"name": "a", "rank": 1}]}})
        self.assertEqual(paths, {"data.players[].name", "data.players[].rank"})

    def test_scalar_and_empty_documents(self) -> None:
        self.assertEqual(json_paths({}), set())
        self.assertEqual(json_paths({"a": 1}), {"a"})


class NoChangeTests(unittest.TestCase):
    def test_identical_bodies_report_no_regression(self) -> None:
        report = compare(BASELINE)
        self.assertEqual(report.severity, SEVERITY_NONE)
        self.assertFalse(report.regressed)
        self.assertEqual(report.summary, "no regression detected")

    def test_report_is_json_serializable(self) -> None:
        json.dumps(compare(BASELINE).to_dict())


class FieldRegressionTests(unittest.TestCase):
    def test_lost_field_is_critical(self) -> None:
        # Both the JSON-LD and the CSS anchor are gone: nothing can supply title.
        current = BASELINE.replace(
            b'<script type="application/ld+json">{"@type":"Article","headline":"Real Title"}</script>', b""
        ).replace(b'<h1 class="t">Real Title</h1>', b'<div class="new">Real Title</div>').replace(
            b"<title>Page</title>", b""
        )
        report = compare(current)
        self.assertEqual(report.severity, SEVERITY_CRITICAL)
        lost = [c for c in report.field_changes if c.kind == "lost"]
        self.assertEqual([c.field for c in lost], ["title"])
        self.assertIn("lost field(s): title", report.summary)

    def test_source_drift_is_a_warning_not_a_loss(self) -> None:
        # JSON-LD disappeared but the CSS selector still yields the value.
        current = BASELINE.replace(
            b'<script type="application/ld+json">{"@type":"Article","headline":"Real Title"}</script>', b""
        )
        report = compare(current)
        self.assertEqual(report.severity, SEVERITY_WARNING)
        drift = [c for c in report.field_changes if c.kind == "source_drift"]
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0].before_source, "json_ld")
        self.assertEqual(drift[0].after_source, "css")

    def test_value_change_is_reported(self) -> None:
        current = BASELINE.replace(b"Real Title", b"Different Title")
        report = compare(current)
        changed = [c for c in report.field_changes if c.kind == "value_changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].after, "Different Title")


class StructureRegressionTests(unittest.TestCase):
    def test_ssr_to_csr_is_critical_with_a_browser_hint(self) -> None:
        current = b'<html><head></head><body><div id="root"></div><script src="/a.js"></script></body></html>'
        report = compare(current)
        self.assertEqual(report.severity, SEVERITY_CRITICAL)
        change = next(c for c in report.structure_changes if c.kind == "rendering_changed")
        self.assertIn("ssr -> csr", change.detail)
        self.assertIn("browser recon", change.replacement_hint)

    def test_verdict_regression_is_flagged(self) -> None:
        blocked = b"<html><title>Just a moment...</title><body>checking your browser</body></html>"
        report = compare(blocked)
        kinds = {c.kind for c in report.structure_changes}
        self.assertIn("verdict_regressed", kinds)
        self.assertEqual(report.current_verdict, "SOFT_BLOCK")

    def test_canonical_change_is_reported(self) -> None:
        current = BASELINE.replace(b"https://x.example/a", b"https://x.example/a-new")
        report = compare(current)
        self.assertIn("canonical_changed", {c.kind for c in report.structure_changes})


class JsonApiRegressionTests(unittest.TestCase):
    def compare_json(self, baseline: dict, current: dict):
        return compare_bodies(
            url="https://x.example/api",
            baseline_body=json.dumps(baseline).encode(),
            current_body=json.dumps(current).encode(),
            baseline_headers=JSON_HEADERS,
            current_headers=JSON_HEADERS,
        )

    def test_moved_path_is_critical_and_suggests_the_replacement(self) -> None:
        report = self.compare_json(
            {"data": {"players": [{"name": "a"}]}},
            {"pageProps": {"players": [{"name": "a"}]}},
        )
        self.assertEqual(report.severity, SEVERITY_CRITICAL)
        lost = [c for c in report.structure_changes if c.kind == "json_path_lost"]
        self.assertTrue(lost)
        self.assertEqual(lost[0].replacement_hint, "$pageProps.players[].name")
        self.assertIn("possible replacement", report.summary)

    def test_added_paths_alone_are_not_a_regression(self) -> None:
        report = self.compare_json({"a": 1}, {"a": 1, "b": 2})
        self.assertEqual(report.severity, SEVERITY_NONE)


class RegressCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.dir = Path(self.tempdir.name)

    def run_cli(self, argv) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = regress_main(argv)
        return code, buffer.getvalue()

    def test_offline_mode_exits_nonzero_on_critical(self) -> None:
        baseline = self.dir / "base.json"
        current = self.dir / "curr.json"
        baseline.write_text(json.dumps({"data": {"players": [{"name": "a"}]}}))
        current.write_text(json.dumps({"pageProps": {"players": [{"name": "a"}]}}))
        code, out = self.run_cli([
            "--baseline", str(baseline), "--current", str(current),
            "--url", "https://x.example/api", "--content-type", "application/json", "--json",
        ])
        self.assertEqual(code, 1)  # gates CI
        self.assertEqual(json.loads(out)["critical"], 1)

    def test_offline_mode_exits_zero_when_unchanged(self) -> None:
        body = self.dir / "b.html"
        body.write_bytes(BASELINE)
        code, _out = self.run_cli([
            "--baseline", str(body), "--current", str(body), "--url", "https://x.example/a", "--json",
        ])
        self.assertEqual(code, 0)

    def test_text_output_names_the_replacement_hint(self) -> None:
        baseline = self.dir / "base.json"
        current = self.dir / "curr.json"
        baseline.write_text(json.dumps({"data": {"players": [{"name": "a"}]}}))
        current.write_text(json.dumps({"pageProps": {"players": [{"name": "a"}]}}))
        _code, out = self.run_cli([
            "--baseline", str(baseline), "--current", str(current),
            "--url", "https://x.example/api", "--content-type", "application/json",
        ])
        self.assertIn("$pageProps.players[].name", out)

    def test_missing_current_is_a_usage_error(self) -> None:
        body = self.dir / "b.html"
        body.write_bytes(BASELINE)
        with self.assertRaises(SystemExit):
            regress_main(["--baseline", str(body), "--url", "https://x.example/a"])


if __name__ == "__main__":
    unittest.main()
