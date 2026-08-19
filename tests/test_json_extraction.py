"""JSON as a first-class body, not as HTML that happens to parse."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import ContentKind
from web_scraper.extract import extract_fields, extract_response
from web_scraper.extract.json_path import JsonPathError, validate_path, walk_many

DOCUMENT = {
    "data": {
        "character": {"name": "Thrall", "score": 93, "active": True, "rank": None},
        "rows": [{"name": "A", "n": 1}, {"name": "B", "n": 2}],
        "players": [{"id": 1, "name": "X"}, {"id": 2, "name": "Y"}, {"id": 3, "name": "Z"}],
        "items": ["a", "b"],
        "metadata": {"total": 1234, "ratio": 0.75},
    }
}
BODY = json.dumps(DOCUMENT).encode()


class PathTests(unittest.TestCase):
    def test_nested_objects(self) -> None:
        self.assertEqual(walk_many(DOCUMENT, "data.character.name"), "Thrall")

    def test_array_index(self) -> None:
        self.assertEqual(walk_many(DOCUMENT, "data.rows.0.name"), "A")
        self.assertEqual(walk_many(DOCUMENT, "data.rows.1.n"), 2)

    def test_a_whole_list_comes_back_as_a_list(self) -> None:
        self.assertEqual(walk_many(DOCUMENT, "data.items"), ["a", "b"])

    def test_a_wildcard_returns_every_match_not_the_first(self) -> None:
        # Asking for every player's name and getting one name would be wrong in
        # a way nobody notices until the counts are compared.
        self.assertEqual(walk_many(DOCUMENT, "data.players[*].name"), ["X", "Y", "Z"])

    def test_a_bare_wildcard_returns_the_elements(self) -> None:
        self.assertEqual(len(walk_many(DOCUMENT, "data.players[*]")), 3)

    def test_types_are_preserved(self) -> None:
        # Stringifying here is how a numeric field is compared as text later.
        self.assertIsInstance(walk_many(DOCUMENT, "data.character.score"), int)
        self.assertIs(walk_many(DOCUMENT, "data.character.active"), True)
        self.assertIsInstance(walk_many(DOCUMENT, "data.metadata.ratio"), float)
        self.assertIsInstance(walk_many(DOCUMENT, "data.rows"), list)
        self.assertIsInstance(walk_many(DOCUMENT, "data.character"), dict)

    def test_a_missing_path_is_none_not_an_error(self) -> None:
        # A field absent from one record among thousands is ordinary.
        self.assertIsNone(walk_many(DOCUMENT, "data.character.nope"))
        self.assertIsNone(walk_many(DOCUMENT, "nothing.here.at.all"))

    def test_an_out_of_range_index_is_none(self) -> None:
        self.assertIsNone(walk_many(DOCUMENT, "data.rows.99.name"))

    def test_an_explicit_null_stays_none(self) -> None:
        self.assertIsNone(walk_many(DOCUMENT, "data.character.rank"))

    def test_a_wildcard_over_a_non_list_yields_nothing(self) -> None:
        self.assertEqual(walk_many(DOCUMENT, "data.character[*].x"), [])

    def test_wildcards_are_bounded(self) -> None:
        # A page of ten thousand rows should not silently become ten thousand
        # extracted values because a profile said [*].
        big = {"rows": [{"n": i} for i in range(5000)]}
        self.assertEqual(len(walk_many(big, "rows[*].n", max_wildcard=100)), 100)


class PathValidationTests(unittest.TestCase):
    """A malformed path is a profile bug and should surface at validation."""

    def test_a_supported_path_validates(self) -> None:
        for path in ("a.b", "a.0.b", "a[*].b", "a[*]"):
            with self.subTest(path=path):
                validate_path(path)

    def test_full_jsonpath_syntax_is_rejected_clearly(self) -> None:
        for path in ("a[0]", "a[?(@.x)]", "a[1:2]"):
            with self.subTest(path=path), self.assertRaises(JsonPathError):
                validate_path(path)

    def test_an_empty_path_is_rejected(self) -> None:
        with self.assertRaises(JsonPathError):
            validate_path("")

    def test_too_many_wildcards_are_rejected(self) -> None:
        with self.assertRaises(JsonPathError):
            validate_path("a[*].b[*].c[*].d")


class ExtractorTests(unittest.TestCase):
    def extract(self, body, headers=None, extractors=None, fields=None):
        return extract_response(
            body,
            headers=headers or {"Content-Type": "application/json"},
            extractors=extractors
            or [
                {
                    "kind": "json",
                    "fields": {
                        "name": "data.character.name",
                        "score": "data.character.score",
                        "total": "data.metadata.total",
                    },
                }
            ],
            fields=fields or ["name", "score", "total"],
        )

    def test_a_pure_json_response_extracts(self) -> None:
        result, kind = self.extract(BODY)
        self.assertIs(kind, ContentKind.JSON)
        self.assertEqual(result.data["name"], "Thrall")
        self.assertEqual(result.data["total"], 1234)

    def test_provenance_names_the_path_not_just_the_format(self) -> None:
        # The drift gate ranks sources; it must see json_path -> heuristic as
        # the degradation it is.
        result, _ = self.extract(BODY)
        self.assertEqual(result.sources["name"], "json_path")

    def test_json_served_as_text_plain_still_extracts(self) -> None:
        result, kind = self.extract(BODY, headers={"Content-Type": "text/plain"})
        self.assertIs(kind, ContentKind.JSON)
        self.assertEqual(result.data["name"], "Thrall")

    def test_invalid_json_extracts_nothing_rather_than_raising(self) -> None:
        result, _ = self.extract(b'{"broken": ', headers={"Content-Type": "application/json"})
        self.assertEqual(result.data, {})

    def test_a_binary_body_is_never_handed_to_an_extractor(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        result, kind = self.extract(png, headers={"Content-Type": "image/png"})
        self.assertIs(kind, ContentKind.BINARY)
        self.assertEqual(result.data, {})

    def test_no_dom_is_built_for_a_json_response(self) -> None:
        # The practical point of first-class JSON: parsing a large API response
        # into an HTML tree to find a field a dotted path already names is work
        # with no purpose.
        import web_scraper.extract.dom as dom_module

        calls: list[str] = []
        original = dom_module.parse_html

        def counting(text):
            calls.append("parsed")
            return original(text)

        dom_module.parse_html = counting  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(dom_module, "parse_html", original))

        extract_fields(
            BODY,
            extractors=[{"kind": "json", "fields": {"name": "data.character.name"}}],
            fields=["name"],
            content_kind=ContentKind.JSON,
        )
        self.assertEqual(calls, [], "a DOM was built for a JSON response")


class HtmlStillWorksTests(unittest.TestCase):
    """The existing chain must be untouched by all of this."""

    HTML = b"""<!DOCTYPE html><html><head>
    <meta property="og:title" content="From OpenGraph">
    <script type="application/ld+json">{"@type":"Article","headline":"From JSON-LD"}</script>
    </head><body><h1>From the DOM</h1></body></html>"""

    def test_json_ld_still_wins_over_meta(self) -> None:
        result, kind = extract_response(
            self.HTML,
            headers={"Content-Type": "text/html"},
            extractors=[{"kind": "json_ld", "schema_type": "Article"}, {"kind": "meta"}],
            fields=["title"],
        )
        self.assertIs(kind, ContentKind.HTML)
        self.assertEqual(result.data["title"], "From JSON-LD")
        self.assertEqual(result.sources["title"], "json_ld")

    def test_meta_still_works_when_json_ld_is_absent(self) -> None:
        result, _ = extract_response(
            self.HTML,
            headers={"Content-Type": "text/html"},
            extractors=[{"kind": "meta"}],
            fields=["title"],
        )
        self.assertEqual(result.data["title"], "From OpenGraph")

    def test_heuristic_still_works(self) -> None:
        result, _ = extract_response(
            self.HTML,
            headers={"Content-Type": "text/html"},
            extractors=[{"kind": "heuristic"}],
            fields=["title"],
        )
        self.assertEqual(result.data["title"], "From the DOM")

    def test_html_containing_json_ld_is_still_an_html_response(self) -> None:
        # Embedded JSON does not make the response JSON. Switching the whole
        # response would abandon the DOM the other extractors need.
        _, kind = extract_response(
            self.HTML,
            headers={"Content-Type": "text/html"},
            extractors=[{"kind": "json_ld"}],
            fields=["title"],
        )
        self.assertIs(kind, ContentKind.HTML)

    def test_a_next_data_page_is_still_html(self) -> None:
        body = (
            b'<html><body><script id="__NEXT_DATA__" type="application/json">'
            b'{"props":{"pageProps":{"title":"From Next"}}}</script></body></html>'
        )
        result, kind = extract_response(
            body,
            headers={"Content-Type": "text/html"},
            extractors=[{"kind": "app_state", "fields": {"title": "props.pageProps.title"}}],
            fields=["title"],
        )
        self.assertIs(kind, ContentKind.HTML)
        self.assertEqual(result.data["title"], "From Next")


if __name__ == "__main__":
    unittest.main()
