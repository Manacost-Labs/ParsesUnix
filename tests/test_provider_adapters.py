"""Firecrawl and Bright Data adapters, exercised without a network.

No API keys exist for either vendor, so none of this measures live behaviour.
What it does pin down is the part that is ours: the request we build, and the way
a vendor's answer is translated into the core's terms. Where the vendors'
documentation is silent — per-mode pricing, cost headers — the assertions below
require the adapter to say "unknown" rather than invent a number.
"""

from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.providers.base import ProviderError, ProviderErrorKind, ProviderRequest
from web_scraper.providers.bright_data import BrightDataProvider
from web_scraper.providers.firecrawl import (
    DOCUMENTED_DEFAULT_MAX_AGE_MS,
    FirecrawlProvider,
)

URL = "https://example.com/article"


class FakeHTTP:
    """Captures the outgoing request and returns a scripted answer."""

    def __init__(self, status: int = 200, body: bytes = b"{}", headers: dict | None = None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.requests: list = []

    def urlopen(self, request, timeout=None):
        self.requests.append(request)
        outer = self

        class Response:
            status = outer.status
            headers = outer.headers

            def read(self):
                return outer.body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return Response()

    @property
    def payload(self) -> dict:
        return json.loads(self.requests[-1].data.decode())


def firecrawl_body(html: str = "<html><body><article>hi</article></body></html>", status=200):
    return json.dumps(
        {
            "success": True,
            "data": {
                "rawHtml": html,
                "metadata": {"statusCode": status, "url": URL, "contentType": "text/html"},
            },
        }
    ).encode()


class FirecrawlRequestTests(unittest.TestCase):
    def provider(self, http: FakeHTTP, **kw) -> FirecrawlProvider:
        return FirecrawlProvider(api_key="k", opener=http, **kw)

    def test_it_asks_for_raw_html_not_markdown(self) -> None:
        # Triage and the extraction chain reason about the document. Markdown has
        # already discarded the canaries, JSON-LD and challenge markup.
        http = FakeHTTP(body=firecrawl_body())
        self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertEqual(http.payload["formats"], ["rawHtml"])
        self.assertFalse(http.payload["onlyMainContent"])

    def test_the_two_day_cache_default_is_overridden(self) -> None:
        # Firecrawl defaults maxAge to two days. Accepting that silently would
        # publish two-day-old content as current.
        http = FakeHTTP(body=firecrawl_body())
        self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertEqual(http.payload["maxAge"], 0)
        self.assertGreater(DOCUMENTED_DEFAULT_MAX_AGE_MS, 0, "their default is non-zero")

    def test_each_strategy_maps_to_its_documented_proxy_mode(self) -> None:
        for strategy, proxy in [("basic", "basic"), ("auto", "auto"), ("enhanced", "enhanced")]:
            with self.subTest(strategy=strategy):
                http = FakeHTTP(body=firecrawl_body())
                self.provider(http).fetch(ProviderRequest(url=URL, strategy_id=strategy))
                self.assertEqual(http.payload["proxy"], proxy)

    def test_an_unknown_strategy_is_rejected_before_any_call(self) -> None:
        http = FakeHTTP(body=firecrawl_body())
        with self.assertRaises(ProviderError) as caught:
            self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="turbo"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.BAD_REQUEST)
        self.assertEqual(http.requests, [], "nothing was sent")

    def test_a_missing_key_never_reaches_the_network(self) -> None:
        http = FakeHTTP()
        provider = FirecrawlProvider(api_key="", token_env="FIRECRAWL_ABSENT", opener=http)
        with self.assertRaises(ProviderError) as caught:
            provider.fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.AUTH)
        self.assertEqual(http.requests, [])


class FirecrawlResponseTests(unittest.TestCase):
    def provider(self, http: FakeHTTP) -> FirecrawlProvider:
        return FirecrawlProvider(api_key="k", opener=http)

    def test_the_target_status_comes_from_metadata_not_the_envelope(self) -> None:
        # Firecrawl answers 200 while reporting that the site returned 404.
        http = FakeHTTP(status=200, body=firecrawl_body(status=404))
        response = self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertEqual(response.target_status, 404)
        self.assertEqual(response.provider_status, 200)

    def test_an_undocumented_cost_header_yields_unknown_not_zero(self) -> None:
        http = FakeHTTP(body=firecrawl_body())
        response = self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertFalse(response.cost.attributed)
        self.assertEqual(response.cost.credits, Decimal("0"), "the field is zero...")
        self.assertFalse(response.cost.attributed, "...but it is explicitly not attributed")

    def test_a_cost_header_is_used_when_present(self) -> None:
        http = FakeHTTP(body=firecrawl_body(), headers={"x-credits-used": "3"})
        response = self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertTrue(response.cost.attributed)
        self.assertEqual(response.cost.credits, Decimal("3"))

    def test_a_live_fetch_can_prove_freshness_and_a_cached_one_cannot(self) -> None:
        http = FakeHTTP(body=firecrawl_body())
        live = self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertTrue(live.freshness_provable)

        cached = self.provider(FakeHTTP(body=firecrawl_body())).fetch(
            ProviderRequest(url=URL, strategy_id="cached")
        )
        self.assertIsNone(cached.from_cache, "the vendor does not say")
        self.assertFalse(cached.freshness_provable, "so freshness cannot be claimed")

    def test_provider_failures_are_not_verdicts_about_the_site(self) -> None:
        cases = [
            (401, ProviderErrorKind.AUTH),
            (402, ProviderErrorKind.QUOTA),
            (429, ProviderErrorKind.QUOTA),
            (400, ProviderErrorKind.BAD_REQUEST),
            (503, ProviderErrorKind.PROVIDER_FAULT),
        ]
        for status, kind in cases:
            with self.subTest(status=status):
                http = FakeHTTP(status=status, body=b'{"success":false}')
                with self.assertRaises(ProviderError) as caught:
                    self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="basic"))
                self.assertEqual(caught.exception.kind, kind)

    def test_a_non_json_answer_is_a_provider_fault(self) -> None:
        http = FakeHTTP(body=b"<html>gateway timeout</html>")
        with self.assertRaises(ProviderError) as caught:
            self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.MALFORMED_RESPONSE)


class BrightDataTests(unittest.TestCase):
    def provider(self, http: FakeHTTP, **kw) -> BrightDataProvider:
        kw.setdefault("api_key", "k")
        kw.setdefault("zone", "unlocker1")
        return BrightDataProvider(opener=http, **kw)

    def test_custom_headers_are_never_forwarded(self) -> None:
        # Documented: sending manual headers moves the account onto being billed
        # for ALL requests, including the failures.
        http = FakeHTTP(body=b"<html>ok</html>")
        self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="unlocker"))
        self.assertNotIn("headers", http.payload)
        self.assertNotIn("cookies", http.payload)

    def test_rendering_is_requested_only_where_a_strategy_promises_it(self) -> None:
        http = FakeHTTP(body=b"<html>ok</html>")
        self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="unlocker"))
        self.assertNotIn("render", http.payload, "paying browser prices to be refused is loss")

        http2 = FakeHTTP(body=b"<html>ok</html>")
        self.provider(http2).fetch(ProviderRequest(url=URL, strategy_id="unlocker_render"))
        self.assertEqual(http2.payload["render"], "true")

    def test_the_browser_api_is_hidden_without_its_own_zone(self) -> None:
        http = FakeHTTP(body=b"<html>ok</html>")
        ids = {s.id for s in self.provider(http).strategies()}
        self.assertNotIn("browser", ids, "advertising it would guarantee failed calls")

        with_zone = self.provider(http, browser_zone="browser1")
        self.assertIn("browser", {s.id for s in with_zone.strategies()})

    def test_the_browser_api_uses_its_own_zone(self) -> None:
        http = FakeHTTP(body=b"<html>ok</html>")
        provider = self.provider(http, browser_zone="browser1")
        provider.fetch(ProviderRequest(url=URL, strategy_id="browser"))
        self.assertEqual(http.payload["zone"], "browser1")

    def test_the_target_status_is_read_from_the_vendor_header(self) -> None:
        http = FakeHTTP(status=200, body=b"not found", headers={"x-brd-http-status": "404"})
        response = self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="unlocker"))
        self.assertEqual(response.target_status, 404)
        self.assertEqual(response.provider_status, 200)

    def test_cpm_billing_means_a_single_call_reports_no_cost(self) -> None:
        http = FakeHTTP(body=b"<html>ok</html>")
        response = self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="unlocker"))
        self.assertFalse(response.cost.attributed, "unknown, not free")

    def test_it_is_priced_above_the_cheaper_providers(self) -> None:
        # Bright Data is the fallback, not the default. The router ranks by cost,
        # so this ordering is what keeps it from being chosen first.
        from web_scraper.providers.firecrawl import STRATEGIES as FC
        from web_scraper.providers.scrape_do import STRATEGIES as SD

        cheapest_bd = min(s.nominal_cost for s in self.provider(FakeHTTP()).strategies())
        self.assertGreater(cheapest_bd, max(s.nominal_cost for s in SD))
        self.assertGreater(cheapest_bd, max(s.nominal_cost for s in FC))

    def test_a_missing_zone_never_reaches_the_network(self) -> None:
        http = FakeHTTP()
        provider = BrightDataProvider(api_key="k", zone="", zone_env="BD_ABSENT", opener=http)
        with self.assertRaises(ProviderError) as caught:
            provider.fetch(ProviderRequest(url=URL, strategy_id="unlocker"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.BAD_REQUEST)
        self.assertEqual(http.requests, [])

    def test_provider_failures_are_not_verdicts_about_the_site(self) -> None:
        for status, kind in [
            (401, ProviderErrorKind.AUTH),
            (402, ProviderErrorKind.QUOTA),
            (429, ProviderErrorKind.QUOTA),
            (500, ProviderErrorKind.PROVIDER_FAULT),
        ]:
            with self.subTest(status=status):
                http = FakeHTTP(status=status, body=b"nope")
                with self.assertRaises(ProviderError) as caught:
                    self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="unlocker"))
                self.assertEqual(caught.exception.kind, kind)


class StrategyIdentityTests(unittest.TestCase):
    def test_every_strategy_id_is_unique_within_its_provider(self) -> None:
        from web_scraper.providers.bright_data import STRATEGIES as BD
        from web_scraper.providers.firecrawl import STRATEGIES as FC
        from web_scraper.providers.scrape_do import STRATEGIES as SD

        for name, strategies in [("scrape.do", SD), ("firecrawl", FC), ("brightdata", BD)]:
            with self.subTest(provider=name):
                ids = [s.id for s in strategies]
                self.assertEqual(len(ids), len(set(ids)))

    def test_every_hold_covers_its_nominal_cost(self) -> None:
        from web_scraper.providers.bright_data import STRATEGIES as BD
        from web_scraper.providers.firecrawl import STRATEGIES as FC
        from web_scraper.providers.scrape_do import STRATEGIES as SD

        for strategy in (*SD, *FC, *BD):
            with self.subTest(strategy=strategy.id):
                self.assertGreaterEqual(strategy.worst_case_cost, strategy.nominal_cost)


if __name__ == "__main__":
    unittest.main()
