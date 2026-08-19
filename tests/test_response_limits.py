"""Body ceilings on the paid path.

The free transports have always capped what they read. The provider path did
not: `.read()` with no argument. A provider is not more trustworthy than an
origin — it is just better paid — and one malfunctioning response could pull
unbounded bytes into memory.

The second half of this is subtler than the cap itself. A truncated body is a
PREFIX, not a document. If that distinction is lost, a cut-off page reads as
thin content and a canary missing from the prefix reads as proof the page is
wrong.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.providers._transport import DEFAULT_MAX_BODY_BYTES, get, post_json
from web_scraper.providers.base import ProviderError, ProviderErrorKind, ProviderRequest
from web_scraper.providers.bright_data import BrightDataProvider
from web_scraper.providers.firecrawl import FirecrawlProvider
from web_scraper.providers.scrape_do import ScrapeDoProvider

URL = "https://example.com/a"


class SizedHTTP:
    """Returns a body of a chosen size, and records how much was requested."""

    def __init__(self, size: int, *, status: int = 200, headers: dict | None = None):
        self._size, self.status = size, status
        self.headers = headers or {}
        self.read_limits: list[int | None] = []

    def urlopen(self, request, timeout=None):
        outer = self

        class Response:
            status = outer.status
            headers = outer.headers

            def read(self, amount=None):
                outer.read_limits.append(amount)
                # Honour the limit the way a real socket read does.
                return b"x" * (outer._size if amount is None else min(amount, outer._size))

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return Response()


class TransportCeilingTests(unittest.TestCase):
    def test_a_body_is_never_read_without_a_limit(self) -> None:
        http = SizedHTTP(1000)
        post_json(
            "https://api.example/x",
            {},
            headers={},
            provider="p",
            timeout_seconds=5,
            opener=http,
        )
        self.assertNotIn(None, http.read_limits, "an unbounded read is the bug this fixes")

    def test_an_oversized_body_is_truncated_not_buffered(self) -> None:
        http = SizedHTTP(DEFAULT_MAX_BODY_BYTES * 3)
        result = post_json(
            "https://api.example/x",
            {},
            headers={},
            provider="p",
            timeout_seconds=5,
            opener=http,
            max_body_bytes=1000,
        )
        self.assertEqual(len(result.body), 1000)
        self.assertTrue(result.truncated)

    def test_a_body_at_exactly_the_limit_is_not_called_truncated(self) -> None:
        http = SizedHTTP(1000)
        result = post_json(
            "https://api.example/x",
            {},
            headers={},
            provider="p",
            timeout_seconds=5,
            opener=http,
            max_body_bytes=1000,
        )
        self.assertEqual(len(result.body), 1000)
        self.assertFalse(result.truncated, "exactly at the ceiling is complete, not cut off")

    def test_the_get_helper_is_bounded_too(self) -> None:
        http = SizedHTTP(5000)
        result = get(
            "https://api.example/x",
            provider="p",
            timeout_seconds=5,
            opener=http,
            max_body_bytes=100,
        )
        self.assertEqual(len(result.body), 100)
        self.assertTrue(result.truncated)

    def test_an_unsolicited_encoding_is_visible(self) -> None:
        # We never send Accept-Encoding, so a compressed body is unsolicited.
        # We do not decompress, so a bomb cannot expand — but the caller can see
        # that the bytes are not what they appear to be.
        http = SizedHTTP(50, headers={"content-encoding": "gzip"})
        result = get("https://api.example/x", provider="p", timeout_seconds=5, opener=http)
        self.assertEqual(result.content_encoding, "gzip")

    def test_a_truncated_json_envelope_is_a_provider_fault(self) -> None:
        # Half a JSON document is the provider's problem, never the site's.
        http = SizedHTTP(10_000)
        result = get(
            "https://api.example/x",
            provider="p",
            timeout_seconds=5,
            opener=http,
            max_body_bytes=50,
        )
        with self.assertRaises(ProviderError) as caught:
            result.json(provider="p")
        self.assertEqual(caught.exception.kind, ProviderErrorKind.MALFORMED_RESPONSE)


class AdapterCeilingTests(unittest.TestCase):
    """Every adapter must carry truncation into the contract, not swallow it."""

    def test_scrape_do_reports_a_truncated_body(self) -> None:
        http = SizedHTTP(10_000, headers={"scrape.do-request-cost": "1"})
        provider = ScrapeDoProvider(token="t", opener=http, max_body_bytes=100)
        response = provider.fetch(ProviderRequest(url=URL, strategy_id="normal"))
        self.assertTrue(response.truncated)
        self.assertEqual(len(response.body), 100)

    def test_scrape_do_does_not_read_without_a_limit(self) -> None:
        http = SizedHTTP(500, headers={"scrape.do-request-cost": "1"})
        ScrapeDoProvider(token="t", opener=http).fetch(
            ProviderRequest(url=URL, strategy_id="normal")
        )
        self.assertNotIn(None, http.read_limits)

    def test_firecrawl_reports_a_truncated_body(self) -> None:
        envelope = json.dumps(
            {
                "success": True,
                "data": {
                    "rawHtml": "<html>" + "y" * 5000 + "</html>",
                    "metadata": {"statusCode": 200},
                },
            }
        ).encode()

        class JsonHTTP(SizedHTTP):
            def urlopen(self, request, timeout=None):
                outer = self

                class Response:
                    status = 200
                    headers: dict = {}

                    def read(self, amount=None):
                        outer.read_limits.append(amount)
                        return envelope if amount is None else envelope[:amount]

                    def __enter__(self):
                        return self

                    def __exit__(self, *_):
                        return False

                return Response()

        http = JsonHTTP(0)
        provider = FirecrawlProvider(api_key="k", opener=http)
        response = provider.fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertFalse(response.truncated, "this envelope fits")
        self.assertNotIn(None, http.read_limits)

    def test_bright_data_reports_a_truncated_body(self) -> None:
        http = SizedHTTP(10_000_000)
        provider = BrightDataProvider(api_key="k", zone="z", opener=http)
        response = provider.fetch(ProviderRequest(url=URL, strategy_id="unlocker"))
        self.assertTrue(response.truncated)
        self.assertLessEqual(len(response.body), DEFAULT_MAX_BODY_BYTES)


class TruncationSemanticsTests(unittest.TestCase):
    def test_a_truncated_body_reaches_the_gateway_as_truncated(self) -> None:
        # Otherwise a cut-off page is judged as thin content, and a canary
        # absent from the prefix reads as proof the page is wrong.
        from web_scraper.fetchers.gateway import _raw_from_provider
        from web_scraper.providers.base import ProviderResponse

        provider_response = ProviderResponse(
            provider="p",
            strategy_id="s",
            target_status=200,
            provider_status=200,
            body=b"<html>partial",
            truncated=True,
        )
        raw = _raw_from_provider(URL, provider_response)
        self.assertTrue(raw.truncated)

    def test_truncation_appears_in_the_serialised_response(self) -> None:
        from web_scraper.providers.base import ProviderResponse

        payload = ProviderResponse(
            provider="p",
            strategy_id="s",
            target_status=200,
            provider_status=200,
            body=b"x",
            truncated=True,
        ).to_dict()
        self.assertIs(payload["truncated"], True)


if __name__ == "__main__":
    unittest.main()
