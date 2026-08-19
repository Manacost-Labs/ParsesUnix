"""What a response *is*, decided by looking rather than by trusting the header.

Getting this wrong is silent. An HTML extractor pointed at JSON finds no
elements and returns nothing; a JSON parser pointed at HTML raises and is
caught. Either way the run completes with a missing field and no explanation.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import ContentKind
from web_scraper.extract import detect_content_kind

HTML = b"<!DOCTYPE html><html><body><article>hi</article></body></html>"
JSON_BODY = json.dumps({"data": {"name": "Thrall", "score": 93}}).encode()


class HeaderAgreesTests(unittest.TestCase):
    def test_html_declared_and_html_sent(self) -> None:
        self.assertIs(
            detect_content_kind(HTML, {"Content-Type": "text/html; charset=utf-8"}),
            ContentKind.HTML,
        )

    def test_json_declared_and_json_sent(self) -> None:
        self.assertIs(
            detect_content_kind(JSON_BODY, {"Content-Type": "application/json"}),
            ContentKind.JSON,
        )

    def test_ld_json_is_json(self) -> None:
        self.assertIs(
            detect_content_kind(JSON_BODY, {"Content-Type": "application/ld+json"}),
            ContentKind.JSON,
        )

    def test_graphql_response_is_json(self) -> None:
        self.assertIs(
            detect_content_kind(JSON_BODY, {"Content-Type": "application/graphql-response+json"}),
            ContentKind.JSON,
        )


class HeaderLiesTests(unittest.TestCase):
    """The body cannot be wrong about itself; the header frequently is."""

    def test_json_served_as_text_plain_is_json(self) -> None:
        # Extremely common. Trusting the header here loses every field.
        self.assertIs(
            detect_content_kind(JSON_BODY, {"Content-Type": "text/plain"}), ContentKind.JSON
        )

    def test_json_served_with_no_header_at_all_is_json(self) -> None:
        self.assertIs(detect_content_kind(JSON_BODY, {}), ContentKind.JSON)

    def test_html_served_under_a_json_content_type_is_html(self) -> None:
        # An error page from a JSON endpoint. Calling it JSON hands a parser
        # something it cannot read.
        self.assertIs(
            detect_content_kind(HTML, {"Content-Type": "application/json"}), ContentKind.HTML
        )

    def test_html_with_a_leading_comment_is_still_html(self) -> None:
        body = b"<!-- generated -->\n<html><body>x</body></html>"
        self.assertIs(detect_content_kind(body, {}), ContentKind.HTML)

    def test_a_bom_does_not_hide_the_real_opening(self) -> None:
        self.assertIs(detect_content_kind(b"\xef\xbb\xbf" + JSON_BODY, {}), ContentKind.JSON)
        self.assertIs(detect_content_kind(b"\xef\xbb\xbf" + HTML, {}), ContentKind.HTML)

    def test_leading_whitespace_does_not_hide_the_real_opening(self) -> None:
        self.assertIs(detect_content_kind(b"\n\n   " + JSON_BODY, {}), ContentKind.JSON)


class MalformedTests(unittest.TestCase):
    def test_something_that_opens_like_json_but_is_not_is_not_called_json(self) -> None:
        # Truncated JSON is not TEXT and not usable JSON. With no declaration it
        # must not be handed to a parser as if it were.
        self.assertIsNot(detect_content_kind(b'{"a": 1', {}), ContentKind.JSON)

    def test_a_declared_json_body_that_will_not_parse_falls_back_to_text(self) -> None:
        self.assertIs(
            detect_content_kind(b"not json at all", {"Content-Type": "application/json"}),
            ContentKind.TEXT,
        )

    def test_an_empty_body_has_no_kind(self) -> None:
        # Reporting HTML because the header said so would send an extractor
        # looking for elements in nothing.
        self.assertIs(detect_content_kind(b"", {"Content-Type": "text/html"}), ContentKind.UNKNOWN)


class BinaryTests(unittest.TestCase):
    def test_a_png_is_binary_whatever_the_header_says(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        self.assertIs(detect_content_kind(png, {"Content-Type": "text/html"}), ContentKind.BINARY)

    def test_a_pdf_is_binary(self) -> None:
        self.assertIs(detect_content_kind(b"%PDF-1.7\n%..." + b"x" * 50, {}), ContentKind.BINARY)

    def test_a_zip_is_binary(self) -> None:
        self.assertIs(detect_content_kind(b"PK\x03\x04" + b"\x00" * 50, {}), ContentKind.BINARY)

    def test_a_declared_image_is_binary(self) -> None:
        self.assertIs(
            detect_content_kind(b"whatever", {"Content-Type": "image/jpeg"}), ContentKind.BINARY
        )

    def test_a_nul_byte_means_not_text(self) -> None:
        self.assertIs(detect_content_kind(b"abc\x00def" + b"x" * 50, {}), ContentKind.BINARY)

    def test_binary_is_never_extractable(self) -> None:
        self.assertFalse(ContentKind.BINARY.is_extractable)
        self.assertTrue(ContentKind.JSON.is_extractable)
        self.assertTrue(ContentKind.HTML.is_extractable)


class NonAsciiTests(unittest.TestCase):
    def test_heavy_utf8_text_is_not_mistaken_for_binary(self) -> None:
        # Counting "unusual" bytes would misclassify a page of Japanese and skip
        # its extraction entirely.
        body = "日本語のページです。".encode() * 20
        self.assertIsNot(detect_content_kind(body, {}), ContentKind.BINARY)

    def test_utf8_html_is_html(self) -> None:
        body = "<html><body>Привет</body></html>".encode()
        self.assertIs(detect_content_kind(body, {"Content-Type": "text/html"}), ContentKind.HTML)


class BoundsTests(unittest.TestCase):
    def test_a_huge_json_body_is_not_fully_parsed_to_classify_it(self) -> None:
        # Parsing 50MB to decide what it is costs more than the extraction.
        from web_scraper.extract.content_kind import MAX_JSON_VALIDATION_BYTES

        huge = b'{"a": "' + b"x" * (MAX_JSON_VALIDATION_BYTES + 10) + b'"}'
        self.assertIs(
            detect_content_kind(huge, {"Content-Type": "application/json"}), ContentKind.JSON
        )

    def test_only_the_head_is_sniffed(self) -> None:
        from web_scraper.extract.content_kind import SNIFF_BYTES

        body = b"<html>" + b"a" * (SNIFF_BYTES * 4) + b"</html>"
        self.assertIs(detect_content_kind(body, {}), ContentKind.HTML)


if __name__ == "__main__":
    unittest.main()
