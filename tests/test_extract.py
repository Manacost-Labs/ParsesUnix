from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.extract import extract_fields, normalize_value, run_quorum  # noqa: E402
from web_scraper.extract import dom  # noqa: E402

JSON_LD = (
    '<html><head><script type="application/ld+json">'
    '{"@type":"Article","headline":"Real Title","datePublished":"2026-08-12T09:30:00Z",'
    '"author":{"@type":"Person","name":"Jane Doe"}}'
    '</script>'
    '<meta property="og:title" content="OG Title">'
    '</head><body>'
    '<h1 class="t">CSS Title</h1><time datetime="2026-08-12T09:30:00Z">Aug 12</time>'
    '<span class="price">1 234,50 ₽</span>'
    '</body></html>'
)

NEXT_DATA = (
    '<html><body><div id="root"></div>'
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"post":{"title":"Next Title"}}}}'
    '</script></body></html>'
)


class NormalizeTests(unittest.TestCase):
    def test_number_with_currency_and_thousands(self) -> None:
        self.assertEqual(normalize_value("1 234,50 ₽", kind="number"), 1234.5)
        self.assertEqual(normalize_value("$1,999.00", kind="number"), 1999.0)

    def test_text_strips_nbsp_and_entities(self) -> None:
        self.assertEqual(normalize_value("a &amp; b", kind="text"), "a & b")

    def test_relative_url_is_absolutized(self) -> None:
        self.assertEqual(
            normalize_value("/x", kind="url", base_url="https://s.example/a/"),
            "https://s.example/x",
        )


class DomTests(unittest.TestCase):
    def test_text_and_attr_selectors(self) -> None:
        root = dom.parse_html('<div><h1 class="t">Hi</h1><time datetime="2026-01-01">x</time></div>')
        self.assertEqual(dom.query_value(root, "h1::text"), "Hi")
        self.assertEqual(dom.query_value(root, "h1.t::text"), "Hi")
        self.assertEqual(dom.query_value(root, "time::attr(datetime)"), "2026-01-01")

    def test_descendant_combinator(self) -> None:
        root = dom.parse_html('<article><div class="body"><p>hello</p></div></article>')
        self.assertEqual(dom.query_value(root, "article .body p::text"), "hello")

    def test_script_text_is_not_leaked_into_text(self) -> None:
        root = dom.parse_html("<div>keep<script>var x=1;</script></div>")
        self.assertEqual(dom.query_value(root, "div::text"), "keep")


class ChainTests(unittest.TestCase):
    EXTRACTORS = [
        {"kind": "json_ld", "schema_type": "Article"},
        {"kind": "meta", "fields": {"title": "og:title"}},
        {"kind": "css", "fields": {"title": "h1.t::text", "price": ".price::text"}},
        {"kind": "heuristic"},
    ]

    def test_json_ld_wins_and_records_source(self) -> None:
        res = extract_fields(JSON_LD, extractors=self.EXTRACTORS, fields=["title", "author"])
        self.assertEqual(res.data["title"], "Real Title")
        self.assertEqual(res.sources["title"], "json_ld")
        self.assertEqual(res.data["author"], "Jane Doe")

    def test_css_fills_field_absent_from_json_ld(self) -> None:
        res = extract_fields(
            JSON_LD, extractors=self.EXTRACTORS, fields=["price"], field_kinds={"price": "number"}
        )
        self.assertEqual(res.data["price"], 1234.5)
        self.assertEqual(res.sources["price"], "css")

    def test_app_state_extractor(self) -> None:
        res = extract_fields(
            NEXT_DATA,
            extractors=[{"kind": "app_state", "source": "next_data",
                         "fields": {"title": "props.pageProps.post.title"}}],
            fields=["title"],
        )
        self.assertEqual(res.data["title"], "Next Title")
        self.assertEqual(res.sources["title"], "app_state")


class QuorumTests(unittest.TestCase):
    def test_agreement_is_high(self) -> None:
        body = (
            '<script type="application/ld+json">{"@type":"Article","headline":"Same"}</script>'
            '<h1 class="t">Same</h1>'
        )
        extractors = [{"kind": "json_ld", "schema_type": "Article"}, {"kind": "css", "fields": {"title": "h1.t::text"}}]
        res = run_quorum(body, extractors=extractors, quorum_fields=["title"])
        self.assertEqual(res.quorum["title"], "high")
        self.assertEqual(res.conflicts, ())

    def test_disagreement_is_conflict(self) -> None:
        body = (
            '<script type="application/ld+json">{"@type":"Article","headline":"A"}</script>'
            '<h1 class="t">B</h1>'
        )
        extractors = [{"kind": "json_ld", "schema_type": "Article"}, {"kind": "css", "fields": {"title": "h1.t::text"}}]
        res = run_quorum(body, extractors=extractors, quorum_fields=["title"])
        self.assertEqual(res.quorum["title"], "conflict")
        self.assertIn("title", res.conflicts)

    def test_single_source_is_medium(self) -> None:
        body = '<script type="application/ld+json">{"@type":"Article","headline":"Only"}</script>'
        extractors = [{"kind": "json_ld", "schema_type": "Article"}, {"kind": "css", "fields": {"title": "h1.t::text"}}]
        res = run_quorum(body, extractors=extractors, quorum_fields=["title"])
        self.assertEqual(res.quorum["title"], "medium")


if __name__ == "__main__":
    unittest.main()
