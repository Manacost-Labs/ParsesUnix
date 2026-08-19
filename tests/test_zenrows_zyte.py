"""ZenRows and Zyte, tested against what their documentation actually says.

Neither has a live key here, so nothing below measures vendor behaviour. What it
pins down is the part that is ours: the request built, the way an answer is
translated, and — most of all — the separations that five live-found defects in
the previous provider round were all violations of.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import ContentRules, CostCertainty, Verdict
from web_scraper.providers.base import ProviderError, ProviderErrorKind, ProviderRequest
from web_scraper.providers.pricing import PricingBook, zenrows_snapshot, zyte_snapshot
from web_scraper.providers.zenrows import ZenRowsProvider
from web_scraper.providers.zyte import ZyteProvider
from web_scraper.triage import classify_response

URL = "https://example.com/page"


class FakeHTTP:
    """Captures the outgoing request and returns a scripted answer."""

    def __init__(self, status=200, body=b"", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.requests: list = []

    def urlopen(self, request, timeout=None):
        self.requests.append(request)
        outer = self

        class Response:
            status = outer.status
            headers = outer.headers

            def read(self, amount=None):
                return outer.body if amount is None else outer.body[:amount]

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return Response()

    @property
    def url(self) -> str:
        return self.requests[-1].full_url

    @property
    def payload(self) -> dict:
        return json.loads(self.requests[-1].data.decode())


# ---------------------------------------------------------------------------
# ZenRows
# ---------------------------------------------------------------------------
class ZenRowsRequestTests(unittest.TestCase):
    def provider(self, http, **kw):
        return ZenRowsProvider(api_key="SECRETKEY", opener=http, **kw)

    def query(self, strategy_id, **kw):
        return self.provider(FakeHTTP(), **kw).build_query(
            ProviderRequest(url=URL, strategy_id=strategy_id)
        )

    def test_each_strategy_sends_its_documented_parameters(self) -> None:
        self.assertEqual(self.query("basic").get("js_render"), None)
        self.assertEqual(self.query("js")["js_render"], "true")
        self.assertEqual(self.query("premium")["premium_proxy"], "true")
        self.assertEqual(self.query("js_premium")["js_render"], "true")
        self.assertEqual(self.query("js_premium")["premium_proxy"], "true")
        self.assertEqual(self.query("auto")["mode"], "auto")

    def test_the_original_target_status_is_always_requested(self) -> None:
        # Without it the API answers 200 for everything and triage would judge a
        # 404 page as thin content instead of a dead URL.
        for strategy in ("basic", "js", "premium", "js_premium", "auto"):
            with self.subTest(strategy=strategy):
                self.assertEqual(self.query(strategy)["original_status"], "true")

    def test_a_country_is_only_sent_where_premium_is_on(self) -> None:
        # Documented as requiring premium_proxy.
        provider = self.provider(FakeHTTP())
        with_premium = provider.build_query(
            ProviderRequest(url=URL, strategy_id="premium", geo_code="DE")
        )
        without = provider.build_query(ProviderRequest(url=URL, strategy_id="basic", geo_code="DE"))
        self.assertEqual(with_premium["proxy_country"], "de")
        self.assertNotIn("proxy_country", without)

    def test_network_capture_is_opt_in_and_only_where_a_browser_runs(self) -> None:
        default = self.query("js")
        self.assertNotIn("json_response", default)
        capturing = self.query("js", capture_network=True)
        self.assertEqual(capturing["json_response"], "true")
        self.assertNotIn("json_response", self.query("basic", capture_network=True))

    def test_an_unknown_strategy_never_reaches_the_network(self) -> None:
        http = FakeHTTP()
        with self.assertRaises(ProviderError) as caught:
            self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="turbo"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.BAD_REQUEST)
        self.assertEqual(http.requests, [])

    def test_a_missing_key_never_reaches_the_network(self) -> None:
        http = FakeHTTP()
        provider = ZenRowsProvider(api_key="", token_env="ZENROWS_ABSENT", opener=http)
        with self.assertRaises(ProviderError) as caught:
            provider.fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.AUTH)
        self.assertEqual(http.requests, [])


class ZenRowsSecrecyTests(unittest.TestCase):
    """The API key travels in the query string, so the URL is a secret."""

    def test_the_key_never_appears_in_a_query_helper(self) -> None:
        # A helper that returned the full URL would invite logging it.
        provider = ZenRowsProvider(api_key="SECRETKEY", opener=FakeHTTP())
        query = provider.build_query(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertNotIn("apikey", query)
        self.assertNotIn("SECRETKEY", json.dumps(query))

    def test_the_key_never_appears_in_an_exception(self) -> None:
        http = FakeHTTP(
            status=500,
            body=json.dumps({"code": "SRV001", "title": "boom"}).encode(),
            headers={"content-type": "application/problem+json"},
        )
        provider = ZenRowsProvider(api_key="SECRETKEY", opener=http)
        with self.assertRaises(ProviderError) as caught:
            provider.fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertNotIn("SECRETKEY", str(caught.exception))
        self.assertNotIn("SECRETKEY", caught.exception.message)

    def test_the_key_never_appears_in_the_response_record(self) -> None:
        http = FakeHTTP(
            body=b"<html>ok</html>", headers={"x-request-id": "r1", "x-request-cost": "1"}
        )
        response = ZenRowsProvider(api_key="SECRETKEY", opener=http).fetch(
            ProviderRequest(url=URL, strategy_id="basic")
        )
        self.assertNotIn("SECRETKEY", json.dumps(response.to_dict()))


class ZenRowsStatusTests(unittest.TestCase):
    """Provider failure vs target answer, decided by the error CODE prefix.

    MEASURED 2026-08-19, after two wrong rules that live calls killed:

    1. "X-Request-Id is set only on processed requests" — false, ZenRows sets it
       on its own errors too.
    2. "application/problem+json means a provider error" — also false, ZenRows
       describes the TARGET's 404 in that same format.

    The code prefix is the real signal, and the billing agrees: REQS* is
    ZenRows refusing our request and costs 0 credits, RESP* is the target
    answering and costs 1.
    """

    def problem(self, status, code, title="something"):
        body = json.dumps(
            {"code": code, "title": title, "detail": title, "status": status}
        ).encode()
        return FakeHTTP(
            status=status,
            body=body,
            headers={
                "content-type": "application/problem+json",
                "x-request-id": "r1",
                "x-request-cost": "0",
            },
        )

    def fetch(self, http):
        return ZenRowsProvider(api_key="k", opener=http).fetch(
            ProviderRequest(url=URL, strategy_id="basic")
        )

    def test_a_plain_200_is_the_sites_answer(self) -> None:
        http = FakeHTTP(
            status=200,
            body=b"<html>ok</html>",
            headers={"x-request-id": "r1", "x-request-credits": "1"},
        )
        response = self.fetch(http)
        self.assertEqual(response.target_status, 200)
        self.assertEqual(response.provider_status, 200)

    def test_a_target_404_reaches_triage_as_a_dead_url(self) -> None:
        # The defect this prevents: raising RESP002 as a provider error would
        # leave the URL unquarantined, re-fetched and re-billed every run.
        http = self.problem(404, "RESP002", "Page not found (RESP002)")
        response = self.fetch(http)
        self.assertEqual(response.target_status, 404)
        verdict = classify_response(
            status=response.target_status,
            body=response.body,
            headers=response.headers,
            rules=ContentRules(min_body_bytes=500),
        ).verdict
        self.assertEqual(verdict, Verdict.DEAD_URL)

    def test_a_target_410_is_also_passed_through(self) -> None:
        response = self.fetch(self.problem(410, "RESP003", "Gone"))
        self.assertEqual(response.target_status, 410)

    def test_a_forbidden_domain_is_our_request_not_the_site(self) -> None:
        # REQS001, measured live: ZenRows refuses some domains outright, and
        # that is emphatically not a verdict about the target.
        with self.assertRaises(ProviderError) as caught:
            self.fetch(self.problem(400, "REQS001", "Requests to this domain are forbidden"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.BAD_REQUEST)
        self.assertIn("REQS001", caught.exception.message)

    def test_bad_credentials_are_an_auth_failure(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            self.fetch(self.problem(401, "AUTH001", "Invalid API key"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.AUTH)

    def test_exhausted_credits_are_a_quota_failure(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            self.fetch(self.problem(402, "BILL001", "Out of credits"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.QUOTA)

    def test_a_rate_limit_is_retryable(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            self.fetch(self.problem(429, "RATE001", "Too many requests"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.QUOTA)
        self.assertTrue(caught.exception.retryable)

    def test_a_provider_5xx_is_a_provider_fault(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            self.fetch(self.problem(503, "SRV001", "Service unavailable"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.PROVIDER_FAULT)

    def test_a_non_problem_body_is_never_treated_as_an_error(self) -> None:
        # A site can legitimately answer 403 with HTML. That is its answer.
        http = FakeHTTP(status=403, body=b"<html>forbidden</html>", headers={"x-request-id": "r1"})
        response = self.fetch(http)
        self.assertEqual(response.target_status, 403)


class ZenRowsCostTests(unittest.TestCase):
    def fetch(self, headers):
        http = FakeHTTP(body=b"<html>ok</html>", headers=headers)
        return ZenRowsProvider(api_key="k", opener=http).fetch(
            ProviderRequest(url=URL, strategy_id="basic")
        )

    def test_credits_are_taken_from_the_credits_header(self) -> None:
        # MEASURED: X-Request-Credits is the credit count and X-Request-Cost is
        # dollars — a basic call reported 1 and 0.001, a js call 5 and 0.005.
        response = self.fetch(
            {"x-request-id": "r", "x-request-credits": "5", "x-request-cost": "0.005"}
        )
        self.assertTrue(response.cost.attributed)
        self.assertEqual(response.cost.credits, Decimal("5"))

    def test_the_dollar_figure_is_used_when_credits_are_absent(self) -> None:
        response = self.fetch({"x-request-id": "r", "x-request-cost": "0.001"})
        self.assertTrue(response.cost.attributed)

    def test_a_missing_cost_header_is_unknown_not_zero(self) -> None:
        response = self.fetch({"x-request-id": "r"})
        self.assertFalse(response.cost.attributed)

    def test_a_billed_zero_is_a_measured_zero(self) -> None:
        # A refused request reports 0 credits, and that is true rather than
        # unknown: the vendor told us it did not charge.
        response = self.fetch({"x-request-id": "r", "x-request-credits": "0"})
        self.assertTrue(response.cost.attributed)
        self.assertEqual(response.cost.credits, Decimal("0"))

    def test_the_request_id_is_carried(self) -> None:
        self.assertEqual(self.fetch({"x-request-id": "abc123"}).request_id, "abc123")

    def test_the_final_url_is_carried(self) -> None:
        response = self.fetch({"x-request-id": "r", "zr-final-url": "https://example.com/final"})
        self.assertEqual(response.final_url, "https://example.com/final")

    def test_a_truncated_body_is_reported(self) -> None:
        # The ceiling is a constructor argument, as it is for every other
        # adapter: patching a module constant would not reach the default bound
        # at call time, and an inconsistent surface is its own defect.
        http = FakeHTTP(body=b"x" * 5000, headers={"x-request-id": "r"})
        provider = ZenRowsProvider(api_key="k", opener=http, max_body_bytes=100)
        response = provider.fetch(ProviderRequest(url=URL, strategy_id="basic"))
        self.assertTrue(response.truncated)
        self.assertEqual(len(response.body), 100)


class ZenRowsDiscoveryTests(unittest.TestCase):
    """Captured traffic goes through the EXISTING collector, protections intact."""

    def capture(self, entries):
        envelope = json.dumps({"html": "<html>page</html>", "xhr": entries}).encode()
        http = FakeHTTP(body=envelope, headers={"x-request-id": "r", "x-request-cost": "5"})
        provider = ZenRowsProvider(api_key="k", opener=http, capture_network=True)
        response = provider.fetch(ProviderRequest(url=URL, strategy_id="js"))
        return provider, response, entries

    def observed(self, entries):
        provider, response, _ = self.capture(entries)
        return provider.observed_requests(response, entries)

    def test_the_page_html_is_separated_from_the_capture(self) -> None:
        _, response, _ = self.capture([])
        self.assertEqual(response.body, b"<html>page</html>", "the caller sees the page")

    def test_a_captured_endpoint_becomes_an_observation(self) -> None:
        mapped = self.observed(
            [
                {
                    "url": "https://example.com/api/stats",
                    "method": "GET",
                    "status": 200,
                    "headers": {"content-type": "application/json"},
                    "body": '{"a": 1}',
                }
            ]
        )
        self.assertEqual(len(mapped), 1)
        self.assertEqual(mapped[0]["url"], "https://example.com/api/stats")
        self.assertEqual(mapped[0]["resource_type"], "xhr")

    def test_header_values_are_never_carried_out(self) -> None:
        # Names decide the verdict; values are how a token reaches a report.
        mapped = self.observed(
            [
                {
                    "url": "https://example.com/api/me",
                    "headers": {"Authorization": "Bearer SECRETTOKEN"},
                    "body": "{}",
                }
            ]
        )
        self.assertIn("Authorization", mapped[0]["request_header_names"])
        self.assertNotIn("SECRETTOKEN", json.dumps(mapped[0], default=str))

    def test_an_authorised_capture_is_rejected_by_the_existing_collector(self) -> None:
        from web_scraper.discovery import (
            CandidateVerdict,
            DiscoveryCollector,
            observed_from_mapping,
        )

        mapped = self.observed(
            [
                {
                    "url": "https://example.com/api/me",
                    "headers": {"Cookie": "session=abc", "content-type": "application/json"},
                    "body": "{}",
                }
            ]
        )
        collector = DiscoveryCollector(min_pages=1)
        collector.observe(observed_from_mapping(mapped[0]))
        self.assertIs(collector.candidates()[0].verdict, CandidateVerdict.REJECTED_AUTH)

    def test_a_private_capture_is_rejected_by_the_existing_validator(self) -> None:
        from web_scraper.discovery import (
            CandidateVerdict,
            DiscoveryCollector,
            observed_from_mapping,
        )

        mapped = self.observed(
            [
                {
                    "url": "http://169.254.169.254/latest/meta-data/",
                    "headers": {"content-type": "application/json"},
                    "body": "{}",
                }
            ]
        )
        collector = DiscoveryCollector(min_pages=1)
        collector.observe(observed_from_mapping(mapped[0]))
        self.assertIs(collector.candidates()[0].verdict, CandidateVerdict.REJECTED_PRIVATE)


# ---------------------------------------------------------------------------
# Zyte
# ---------------------------------------------------------------------------
def zyte_body(**kw):
    base = {"url": URL, "statusCode": 200}
    base.update(kw)
    return json.dumps(base).encode()


class ZyteRequestTests(unittest.TestCase):
    def provider(self, http, **kw):
        return ZyteProvider(api_key="APIKEY", opener=http, **kw)

    def test_authentication_is_basic_with_an_empty_password(self) -> None:
        http = FakeHTTP(body=zyte_body(httpResponseBody=base64.b64encode(b"x").decode()))
        self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="http"))
        header = http.requests[-1].get_header("Authorization")
        decoded = base64.b64decode(header.split(" ")[1]).decode()
        self.assertEqual(decoded, "APIKEY:")

    def test_http_mode_asks_for_the_raw_body_and_headers(self) -> None:
        payload = self.provider(FakeHTTP()).build_payload(
            ProviderRequest(url=URL, strategy_id="http")
        )
        self.assertTrue(payload["httpResponseBody"])
        self.assertTrue(payload["httpResponseHeaders"])
        self.assertNotIn("browserHtml", payload)

    def test_browser_mode_asks_for_rendered_html_not_vendor_extraction(self) -> None:
        # A vendor changing its parser must not be able to change our dataset.
        payload = self.provider(FakeHTTP()).build_payload(
            ProviderRequest(url=URL, strategy_id="browser")
        )
        self.assertTrue(payload["browserHtml"])
        for vendor_extraction in ("product", "article", "productList", "jobPosting"):
            self.assertNotIn(vendor_extraction, payload)

    def test_capture_mode_sends_documented_filters(self) -> None:
        payload = self.provider(FakeHTTP()).build_payload(
            ProviderRequest(url=URL, strategy_id="browser_capture")
        )
        filters = payload["networkCapture"]
        self.assertGreater(len(filters), 0)
        for entry in filters:
            self.assertEqual(entry["filterType"], "url")
            self.assertIn("matchType", entry)
            self.assertTrue(entry["httpResponseBody"])

    def test_tags_carry_no_target_data(self) -> None:
        # Tags are echoed on the vendor's billing system; a URL in one would put
        # target data there.
        payload = self.provider(FakeHTTP(), run_id="run-7").build_payload(
            ProviderRequest(url="https://secret.example/private/path", strategy_id="http")
        )
        self.assertEqual(payload["tags"], {"strategy": "http", "run": "run-7"})
        self.assertNotIn("secret.example", json.dumps(payload["tags"]))

    def test_an_unknown_strategy_never_reaches_the_network(self) -> None:
        http = FakeHTTP()
        with self.assertRaises(ProviderError):
            self.provider(http).fetch(ProviderRequest(url=URL, strategy_id="magic"))
        self.assertEqual(http.requests, [])


class ZyteResponseTests(unittest.TestCase):
    def fetch(self, strategy_id, envelope, status=200):
        http = FakeHTTP(status=status, body=envelope)
        return ZyteProvider(api_key="k", opener=http).fetch(
            ProviderRequest(url=URL, strategy_id=strategy_id)
        )

    def test_the_target_status_comes_from_its_own_field(self) -> None:
        # Zyte separates them cleanly: its 200 is about Zyte, statusCode is the
        # site's. No heuristic needed, so none is used.
        response = self.fetch(
            "http", zyte_body(statusCode=404, httpResponseBody=base64.b64encode(b"gone").decode())
        )
        self.assertEqual(response.target_status, 404)
        self.assertEqual(response.provider_status, 200)

    def test_the_http_body_is_base64_decoded(self) -> None:
        encoded = base64.b64encode(b"<html>real</html>").decode()
        response = self.fetch("http", zyte_body(httpResponseBody=encoded))
        self.assertEqual(response.body, b"<html>real</html>")

    def test_a_malformed_base64_body_is_a_provider_fault(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            self.fetch("http", zyte_body(httpResponseBody="!!!not base64!!!"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.MALFORMED_RESPONSE)

    def test_browser_html_comes_back_as_text(self) -> None:
        response = self.fetch("browser", zyte_body(browserHtml="<html>rendered</html>"))
        self.assertEqual(response.body, b"<html>rendered</html>")
        self.assertEqual(response.headers.get("Content-Type"), "text/html")

    def test_a_non_json_envelope_is_a_provider_fault(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            self.fetch("http", b"<html>gateway timeout</html>")
        self.assertEqual(caught.exception.kind, ProviderErrorKind.MALFORMED_RESPONSE)


class ZyteErrorTests(unittest.TestCase):
    def fetch(self, status, error_type):
        body = json.dumps({"status": status, "type": error_type}).encode()
        http = FakeHTTP(status=status, body=body)
        return ZyteProvider(api_key="k", opener=http).fetch(
            ProviderRequest(url=URL, strategy_id="http")
        )

    def test_documented_error_types_map_correctly(self) -> None:
        cases = [
            (401, "/auth/key-not-found", ProviderErrorKind.AUTH),
            (401, "/auth/not-valid", ProviderErrorKind.AUTH),
            (429, "/limits/over-user-limit", ProviderErrorKind.QUOTA),
            (429, "/limits/over-domain-limit", ProviderErrorKind.QUOTA),
            (400, "/request/invalid", ProviderErrorKind.BAD_REQUEST),
            (422, "/request/unprocessable", ProviderErrorKind.BAD_REQUEST),
        ]
        for status, error_type, expected in cases:
            with self.subTest(type=error_type):
                with self.assertRaises(ProviderError) as caught:
                    self.fetch(status, error_type)
                self.assertEqual(caught.exception.kind, expected)

    def test_the_error_type_reaches_the_operator(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            self.fetch(401, "/auth/key-not-found")
        self.assertIn("/auth/key-not-found", caught.exception.message)

    def test_an_unrecognised_failure_is_a_provider_fault(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            self.fetch(503, "/unknown/thing")
        self.assertEqual(caught.exception.kind, ProviderErrorKind.PROVIDER_FAULT)


class ZyteBoundsTests(unittest.TestCase):
    def test_the_body_ceiling_is_configurable_like_every_other_adapter(self) -> None:
        http = FakeHTTP(body=b"y" * 9000)
        provider = ZyteProvider(api_key="k", opener=http, max_body_bytes=200)
        with self.assertRaises(ProviderError) as caught:
            provider.fetch(ProviderRequest(url=URL, strategy_id="http"))
        # A truncated JSON envelope cannot be parsed, which is the honest
        # outcome: half a document is not a document.
        self.assertEqual(caught.exception.kind, ProviderErrorKind.MALFORMED_RESPONSE)


class ZyteCaptureTests(unittest.TestCase):
    def observed(self, entries, page_url=URL):
        provider = ZyteProvider(api_key="k", opener=FakeHTTP())
        return provider.observed_requests({"networkCapture": entries}, page_url=page_url)

    def test_a_captured_response_becomes_an_observation(self) -> None:
        mapped = self.observed(
            [
                {
                    "url": "https://example.com/api/rankings",
                    "statusCode": 200,
                    "httpResponseBody": base64.b64encode(b'{"a":1}').decode(),
                    "headers": [{"name": "content-type", "value": "application/json"}],
                    "request": {"method": "GET", "headers": [{"name": "Accept"}]},
                }
            ]
        )
        self.assertEqual(mapped[0]["url"], "https://example.com/api/rankings")
        self.assertEqual(mapped[0]["content_type"], "application/json")
        self.assertEqual(mapped[0]["body"], b'{"a":1}')

    def test_header_values_are_never_carried_out(self) -> None:
        mapped = self.observed(
            [
                {
                    "url": "https://example.com/api/me",
                    "request": {"headers": [{"name": "Authorization", "value": "Bearer SECRET"}]},
                }
            ]
        )
        self.assertIn("Authorization", mapped[0]["request_header_names"])
        self.assertNotIn("SECRET", json.dumps(mapped[0], default=str))

    def test_an_undecodable_capture_is_skipped_rather_than_guessed(self) -> None:
        mapped = self.observed(
            [{"url": "https://example.com/api/x", "httpResponseBody": "!!!bad!!!"}]
        )
        self.assertEqual(mapped[0]["body"], b"", "no schema is invented from a bad body")


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------
class ZenRowsPricingTests(unittest.TestCase):
    def book(self, cpm):
        return PricingBook((zenrows_snapshot(cpm),))

    def test_documented_multipliers_are_applied(self) -> None:
        book = self.book("1.00")
        self.assertEqual(book.settle("zenrows", "basic", None).credits, Decimal("1"))
        self.assertEqual(book.settle("zenrows", "js", None).credits, Decimal("5"))
        self.assertEqual(book.settle("zenrows", "premium", None).credits, Decimal("10"))
        self.assertEqual(book.settle("zenrows", "js_premium", None).credits, Decimal("25"))

    def test_without_a_plan_rate_nothing_is_bounded(self) -> None:
        book = self.book(None)
        for strategy in ("basic", "js", "premium", "js_premium", "auto"):
            with self.subTest(strategy=strategy):
                self.assertIs(
                    book.settle("zenrows", strategy, None).certainty, CostCertainty.UNKNOWN
                )

    def test_auto_is_never_bounded_optimistically(self) -> None:
        # The vendor picks the mode, so an unreported cost cannot be assumed to
        # be the cheapest one it might have picked.
        self.assertIs(
            self.book("1.00").settle("zenrows", "auto", None).certainty, CostCertainty.UNKNOWN
        )

    def test_a_reported_cost_overrides_the_planning_multiplier(self) -> None:
        # The multiplier is what we expected; the header is what happened.
        cost = self.book("1.00").settle("zenrows", "js", Decimal("25"))
        self.assertIs(cost.certainty, CostCertainty.EXACT)
        self.assertEqual(cost.credits, Decimal("25"))

    def test_a_cost_above_the_documented_ceiling_is_drift(self) -> None:
        drift = self.book("1.00").detect_drift("zenrows", "basic", Decimal("9"))
        self.assertIsNotNone(drift)


class ZytePricingTests(unittest.TestCase):
    def test_without_a_ceiling_every_mode_is_unknown(self) -> None:
        # Zyte prices by website tier; a figure safe on an easy domain
        # understates a hard one, which is where the paid layer gets used.
        book = PricingBook((zyte_snapshot(None),))
        for strategy in ("http", "browser", "browser_capture"):
            with self.subTest(strategy=strategy):
                self.assertIs(book.settle("zyte", strategy, None).certainty, CostCertainty.UNKNOWN)

    def test_operator_ceilings_make_each_mode_boundable(self) -> None:
        book = PricingBook((zyte_snapshot("0.002", "0.01", "0.015"),))
        for strategy, expected in (
            ("http", Decimal("0.002000")),
            ("browser", Decimal("0.010000")),
            ("browser_capture", Decimal("0.015000")),
        ):
            with self.subTest(strategy=strategy):
                cost = book.settle("zyte", strategy, None)
                self.assertIs(cost.certainty, CostCertainty.PROVISIONAL)
                self.assertEqual(cost.estimated_usd, expected)

    def test_a_nonsense_ceiling_is_refused_rather_than_used(self) -> None:
        for bad in ("", "abc", "0", "-1"):
            with self.subTest(ceiling=bad):
                book = PricingBook((zyte_snapshot(bad),))
                self.assertIs(book.settle("zyte", "http", None).certainty, CostCertainty.UNKNOWN)


class FleetTests(unittest.TestCase):
    def test_the_estimator_and_the_runner_share_one_fleet(self) -> None:
        # Two fleets would let an estimate price a provider the run never uses.
        import inspect

        from web_scraper.run import estimate_cli, runner

        self.assertIn("configured_providers", inspect.getsource(runner))
        self.assertIn("configured_providers", inspect.getsource(estimate_cli))

    def test_both_providers_join_the_fleet_when_configured(self) -> None:
        import os

        from web_scraper.run.estimate_cli import configured_providers

        for name in ("ZENROWS_API_KEY", "ZYTE_API_KEY"):
            os.environ[name] = "test"
            self.addCleanup(lambda n=name: os.environ.pop(n, None))
        names = {p.name for p in configured_providers()}
        self.assertIn("zenrows", names)
        self.assertIn("zyte", names)

    def test_a_provider_without_a_key_stays_out(self) -> None:
        import os

        from web_scraper.run.estimate_cli import configured_providers

        os.environ.pop("ZENROWS_API_KEY", None)
        os.environ.pop("ZYTE_API_KEY", None)
        names = {p.name for p in configured_providers()}
        self.assertNotIn("zenrows", names)
        self.assertNotIn("zyte", names)


class CapabilityMatrixTests(unittest.TestCase):
    """Documented capabilities, and the one-capability-is-enough rule.

    Firecrawl was once unreachable for BLOCKED because the matcher demanded that
    every capability be relevant. These pin the matrix for the new providers.
    """

    def test_the_matrix_is_what_the_documentation_describes(self) -> None:
        from web_scraper.providers.zenrows import STRATEGIES as ZR

        expected = {
            "basic": (False, False),
            "js": (True, False),
            "premium": (False, True),
            "js_premium": (True, True),
            "auto": (True, True),
        }
        actual = {s.id: (s.renders_javascript, s.premium_network) for s in ZR}
        self.assertEqual(actual, expected)

    def test_a_dual_capability_strategy_serves_a_block(self) -> None:
        from web_scraper.providers.router import _strategy_is_appropriate
        from web_scraper.providers.zenrows import AUTO, JS_PREMIUM, PREMIUM

        for strategy in (PREMIUM, JS_PREMIUM, AUTO):
            with self.subTest(strategy=strategy.id):
                self.assertTrue(_strategy_is_appropriate(strategy, Verdict.BLOCKED))

    def test_a_rendering_only_strategy_does_not_serve_a_block(self) -> None:
        from web_scraper.providers.router import _strategy_is_appropriate
        from web_scraper.providers.zenrows import JS

        self.assertFalse(_strategy_is_appropriate(JS, Verdict.BLOCKED))

    def test_zyte_http_is_a_premium_network_not_a_renderer(self) -> None:
        from web_scraper.providers.router import _strategy_is_appropriate
        from web_scraper.providers.zyte import BROWSER, HTTP

        self.assertTrue(_strategy_is_appropriate(HTTP, Verdict.BLOCKED))
        self.assertFalse(_strategy_is_appropriate(HTTP, Verdict.CSR_REQUIRED))
        self.assertTrue(_strategy_is_appropriate(BROWSER, Verdict.CSR_REQUIRED))


if __name__ == "__main__":
    unittest.main()


class CaptureSurfaceTests(unittest.TestCase):
    """Discovery has to be reachable, or the capture code is decoration.

    An earlier draft computed the captured traffic inside fetch() and dropped
    it: nothing a caller could reach, so discovery through these providers could
    not have worked. Ruff noticing an unused variable is what surfaced it.
    """

    def test_zenrows_returns_its_capture_to_a_caller_that_asks(self) -> None:
        envelope = json.dumps(
            {
                "html": "<html>page</html>",
                "xhr": [{"url": "https://example.com/api/x", "body": "{}"}],
            }
        ).encode()
        http = FakeHTTP(body=envelope, headers={"x-request-id": "r"})
        provider = ZenRowsProvider(api_key="k", opener=http, capture_network=True)
        response, captured = provider.fetch_with_capture(ProviderRequest(url=URL, strategy_id="js"))
        self.assertEqual(response.body, b"<html>page</html>")
        self.assertEqual(len(captured), 1)
        # Discovery's shape, not ZenRows' wire format: the calibration harness
        # and the runner feed both providers' captures to one collector.
        self.assertEqual(captured[0]["url"], "https://example.com/api/x")
        self.assertIn("resource_type", captured[0])
        self.assertIsInstance(captured[0]["body"], bytes)

    def test_zenrows_fetch_still_satisfies_the_plain_contract(self) -> None:
        http = FakeHTTP(body=b"<html>x</html>", headers={"x-request-id": "r"})
        response = ZenRowsProvider(api_key="k", opener=http).fetch(
            ProviderRequest(url=URL, strategy_id="basic")
        )
        self.assertEqual(response.target_status, 200)

    def test_zyte_returns_its_capture_in_discovery_shape(self) -> None:
        envelope = zyte_body(
            browserHtml="<html>x</html>",
            networkCapture=[
                {
                    "url": "https://example.com/api/rankings",
                    "statusCode": 200,
                    "httpResponseBody": base64.b64encode(b'{"a":1}').decode(),
                    "headers": [{"name": "content-type", "value": "application/json"}],
                    "request": {"method": "GET", "headers": []},
                }
            ],
        )
        provider = ZyteProvider(api_key="k", opener=FakeHTTP(body=envelope))
        response, captured = provider.fetch_with_capture(
            ProviderRequest(url=URL, strategy_id="browser_capture")
        )
        self.assertEqual(response.body, b"<html>x</html>")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["url"], "https://example.com/api/rankings")

    def test_both_capture_shapes_feed_the_existing_collector(self) -> None:
        from web_scraper.discovery import DiscoveryCollector, observed_from_mapping

        resolver = lambda h, p, **k: [(2, 1, 6, "", ("93.184.216.34", p))]  # noqa: E731
        envelope = zyte_body(
            browserHtml="<html>x</html>",
            networkCapture=[
                {
                    "url": "https://example.com/api/rankings",
                    "statusCode": 200,
                    "httpResponseBody": base64.b64encode(b'{"players":[{"id":1}]}').decode(),
                    "headers": [{"name": "content-type", "value": "application/json"}],
                    "request": {"method": "GET", "headers": []},
                }
            ],
        )
        _, captured = ZyteProvider(api_key="k", opener=FakeHTTP(body=envelope)).fetch_with_capture(
            ProviderRequest(url=URL, strategy_id="browser_capture")
        )
        collector = DiscoveryCollector(min_pages=1, resolver=resolver)
        collector.observe(observed_from_mapping(captured[0]))
        candidate = collector.candidates()[0]
        self.assertTrue(candidate.verdict.is_usable)
        self.assertIsNotNone(candidate.schema)
