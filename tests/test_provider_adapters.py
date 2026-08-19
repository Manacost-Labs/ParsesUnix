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

from web_scraper.contracts import Verdict
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

            def read(self, amount=None):
                # Real socket reads take a limit; the adapters now pass one.
                return outer.body if amount is None else outer.body[:amount]

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
        # MEASURED 2026-08-19: Bright Data ALWAYS answers 200 in the envelope
        # and puts the site's own status in x-brd-status-code. A request for a
        # missing page came back envelope 200 / x-brd-status-code 404.
        #
        # This test previously used `x-brd-http-status`, a name that does not
        # exist — the same wrong assumption the adapter held. A test written
        # from the code's own mistake cannot catch it, and this one did not:
        # it passed while every dead URL was being reported as a successful
        # fetch of a 153-byte page.
        http = FakeHTTP(status=200, body=b"not found", headers={"x-brd-status-code": "404"})
        response = self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="unlocker"))
        self.assertEqual(response.target_status, 404)
        self.assertEqual(response.provider_status, 200)

    def test_a_dead_url_behind_a_200_envelope_is_still_a_dead_url(self) -> None:
        # The consequence the header name protects: without it the URL is never
        # quarantined and is re-fetched every run, billed every time, because
        # CPM counts successful requests.
        from web_scraper.contracts import ContentRules
        from web_scraper.triage import classify_response

        http = FakeHTTP(
            status=200,
            body=b"<html><head><title>404 Not Found</title></head><body>nope</body></html>",
            headers={"x-brd-status-code": "404"},
        )
        response = self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="unlocker"))
        verdict = classify_response(
            status=response.target_status,
            body=response.body,
            headers=response.headers,
            rules=ContentRules(min_body_bytes=500),
        )
        self.assertEqual(verdict.verdict, Verdict.DEAD_URL)

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


class BrightDataErrorHeaderTests(unittest.TestCase):
    """MEASURED: Bright Data reports its own failures with HTTP 200.

    A request naming a zone that does not exist comes back 200, empty body, and
    `x-brd-err-code: client_10002`. An adapter reading only the status would
    call that a successful fetch of an empty page; triage would call it
    THIN_CONTENT; and a fact about OUR configuration would be filed as a fact
    about the SITE. Only a live call surfaced it.
    """

    def provider(self, headers, body=b""):
        return BrightDataProvider(
            api_key="k", zone="z", opener=FakeHTTP(status=200, body=body, headers=headers)
        )

    def request(self):
        return ProviderRequest(url=URL, strategy_id="unlocker")

    def test_a_200_with_an_error_header_is_not_a_successful_fetch(self) -> None:
        provider = self.provider(
            {
                "x-brd-err-code": "client_10002",
                "x-brd-err-msg": "Authentication failed: zone not found",
            }
        )
        with self.assertRaises(ProviderError) as caught:
            provider.fetch(self.request())
        self.assertEqual(caught.exception.kind, ProviderErrorKind.AUTH)

    def test_a_zone_problem_needs_a_human_not_a_retry(self) -> None:
        # AUTH opens the provider breaker and waits for a person: no retry helps
        # and no other strategy fares better when the zone does not exist.
        provider = self.provider({"x-brd-err-code": "client_10002", "x-brd-err-msg": "zone"})
        with self.assertRaises(ProviderError) as caught:
            provider.fetch(self.request())
        self.assertFalse(caught.exception.retryable)

    def test_an_unrecognised_error_code_is_a_provider_fault(self) -> None:
        # Never a target verdict: an error header means the vendor did not
        # reach the target on our terms, whatever else it says.
        provider = self.provider({"x-brd-err-code": "server_9999", "x-brd-err-msg": "boom"})
        with self.assertRaises(ProviderError) as caught:
            provider.fetch(self.request())
        self.assertEqual(caught.exception.kind, ProviderErrorKind.PROVIDER_FAULT)

    def test_the_error_message_reaches_the_operator(self) -> None:
        provider = self.provider(
            {"x-brd-err-code": "client_10002", "x-brd-err-msg": "zone is disabled or deleted"}
        )
        with self.assertRaises(ProviderError) as caught:
            provider.fetch(self.request())
        self.assertIn("disabled or deleted", caught.exception.message)

    def test_a_clean_response_still_succeeds(self) -> None:
        provider = self.provider({"content-type": "text/html"}, body=b"<html>ok</html>")
        response = provider.fetch(self.request())
        self.assertEqual(response.body, b"<html>ok</html>")


class FirecrawlCostLocationTests(unittest.TestCase):
    """MEASURED: Firecrawl reports cost in the BODY, never in a header.

    An earlier version guessed at header names that do not exist, so every call
    settled as unattributed spend.
    """

    def envelope(self, **metadata):
        base = {"statusCode": 200, "url": URL, "contentType": "text/html"}
        base.update(metadata)
        return json.dumps(
            {"success": True, "data": {"rawHtml": "<html>x</html>", "metadata": base}}
        ).encode()

    def test_credits_are_read_from_metadata(self) -> None:
        http = FakeHTTP(body=self.envelope(creditsUsed=1))
        response = FirecrawlProvider(api_key="k", opener=http).fetch(
            ProviderRequest(url=URL, strategy_id="basic")
        )
        self.assertTrue(response.cost.attributed)
        self.assertEqual(response.cost.credits, Decimal("1"))

    def test_a_missing_credits_field_is_still_unknown_not_zero(self) -> None:
        http = FakeHTTP(body=self.envelope())
        response = FirecrawlProvider(api_key="k", opener=http).fetch(
            ProviderRequest(url=URL, strategy_id="basic")
        )
        self.assertFalse(response.cost.attributed)

    def test_the_proxy_actually_used_is_recorded(self) -> None:
        # Relevant for `auto`, which is documented to escalate without saying
        # what the escalation costs.
        http = FakeHTTP(body=self.envelope(creditsUsed=1, proxyUsed="stealth"))
        response = FirecrawlProvider(api_key="k", opener=http).fetch(
            ProviderRequest(url=URL, strategy_id="enhanced")
        )
        self.assertEqual(response.detected_defense, "stealth")

    def test_the_cached_strategy_actually_asks_for_a_cache(self) -> None:
        # An earlier default of 0 made it identical to a live fetch: it named a
        # behaviour it did not have.
        provider = FirecrawlProvider(api_key="k", opener=FakeHTTP(body=self.envelope()))
        cached = provider.build_payload(ProviderRequest(url=URL, strategy_id="cached"))
        live = provider.build_payload(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertGreater(cached["maxAge"], 0)
        self.assertEqual(live["maxAge"], 0)
