from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.storage.redaction import (
    REDACTED,
    redact_body,
    redact_headers,
    redact_url,
)


class RedactUrlTests(unittest.TestCase):
    def test_masks_sensitive_query_values(self) -> None:
        out = redact_url("https://x.example/api?api_key=SECRET123&page=2")
        self.assertIn("api_key=%5BREDACTED%5D", out)  # urlencoded [REDACTED]
        self.assertIn("page=2", out)
        self.assertNotIn("SECRET123", out)

    def test_leaves_url_without_query_untouched(self) -> None:
        self.assertEqual(redact_url("https://x.example/a/b"), "https://x.example/a/b")


class RedactBodyTests(unittest.TestCase):
    def test_masks_bearer_and_keys(self) -> None:
        body = b'{"token":"Bearer abcdef1234567890","aws":"AKIAIOSFODNN7EXAMPLE"}'
        out = redact_body(body)
        self.assertNotIn(b"abcdef1234567890", out)
        self.assertNotIn(b"AKIAIOSFODNN7EXAMPLE", out)
        self.assertIn(REDACTED.encode(), out)

    def test_masks_jwt(self) -> None:
        jwt = b"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
        self.assertNotIn(jwt, redact_body(b"auth=" + jwt))

    def test_leaves_clean_body_untouched(self) -> None:
        body = b"<html><h1>hello world</h1></html>"
        self.assertEqual(redact_body(body), body)


class RedactHeadersTests(unittest.TestCase):
    def test_expanded_sensitive_set(self) -> None:
        out = redact_headers(
            {
                "WWW-Authenticate": "Basic realm=x",
                "X-Goog-Api-Key": "k",
                "Content-Type": "text/html",
            }
        )
        self.assertEqual(out["WWW-Authenticate"], REDACTED)
        self.assertEqual(out["X-Goog-Api-Key"], REDACTED)
        self.assertEqual(out["Content-Type"], "text/html")


if __name__ == "__main__":
    unittest.main()
