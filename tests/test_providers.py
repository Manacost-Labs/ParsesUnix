"""Provider contract, verified against headers captured from the live API.

The fixtures below are the real response headers scrape.do returned on
2026-08-19, with the token and request ids replaced. No test here makes a
network call.
"""

from __future__ import annotations

import io
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import ContentRules, Verdict
from web_scraper.providers import (
    ProviderCost,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ScrapeDoProvider,
)
from web_scraper.triage import classify_response

# Captured live, verbatim header names.
LIVE_OK_HEADERS = {
    "content-type": "text/html",
    "scrape.do-detected-waf": "CLOUDFLARE",
    "scrape.do-initial-status-code": "200",
    "scrape.do-remaining-credits": "40194",
    "scrape.do-request-cost": "1",
    "scrape.do-request-id": "00000000-0000-0000-0000-000000000000",
    "scrape.do-resolved-url": "https://example.com/",
}
BODY = (
    b"<!doctype html><html><head><title>Example Domain</title></head><body>"
    + b"x" * 600
    + b"</body></html>"
)


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        super().__init__(body)
        self.status = status
        self.headers = headers

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = BODY) -> None:
        self.status, self.headers, self.body = status, headers, body
        self.urls: list[str] = []

    def urlopen(self, request: object, timeout: float = 0) -> FakeResponse:
        self.urls.append(request.full_url)  # type: ignore[attr-defined]
        return FakeResponse(self.status, dict(self.headers), self.body)


def provider(opener: FakeOpener) -> ScrapeDoProvider:
    return ScrapeDoProvider(token="TESTTOKEN", opener=opener)


class QueryTests(unittest.TestCase):
    def test_internal_retries_are_disabled(self) -> None:
        # Our retry budget is authoritative; provider retries would double spend
        # and hide the real failure rate.
        query = provider(FakeOpener(200, {})).build_query(
            ProviderRequest(url="https://x.example/a", strategy_id="normal")
        )
        self.assertEqual(query["disableRetry"], "true")

    def test_each_strategy_maps_to_its_parameters(self) -> None:
        p = provider(FakeOpener(200, {}))
        cases = {
            "normal": ({}, {"render", "super"}),
            "render": ({"render": "true"}, {"super"}),
            "super": ({"super": "true"}, {"render"}),
            "super_render": ({"render": "true", "super": "true"}, set()),
        }
        for strategy_id, (present, absent) in cases.items():
            with self.subTest(strategy=strategy_id):
                query = p.build_query(
                    ProviderRequest(url="https://x.example/a", strategy_id=strategy_id)
                )
                for key, value in present.items():
                    self.assertEqual(query[key], value)
                for key in absent:
                    self.assertNotIn(key, query)

    def test_a_wait_selector_only_applies_to_rendering(self) -> None:
        p = provider(FakeOpener(200, {}))
        request = ProviderRequest(
            url="https://x.example/a", strategy_id="normal", wait_selector=".item"
        )
        self.assertNotIn("waitSelector", p.build_query(request))

    def test_an_unknown_strategy_is_a_bad_request(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            provider(FakeOpener(200, {})).build_query(
                ProviderRequest(url="https://x.example/a", strategy_id="turbo")
            )
        self.assertEqual(caught.exception.kind, ProviderErrorKind.BAD_REQUEST)

    def test_a_missing_token_is_refused_before_any_call(self) -> None:
        bare = ScrapeDoProvider(token="", token_env="DEFINITELY_UNSET_TOKEN_VAR")
        self.assertFalse(bare.configured)
        with self.assertRaises(ProviderError) as caught:
            bare.fetch(ProviderRequest(url="https://x.example/a", strategy_id="normal"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.AUTH)


class ResponseTests(unittest.TestCase):
    def test_a_live_shaped_success_is_translated(self) -> None:
        opener = FakeOpener(200, LIVE_OK_HEADERS)
        response = provider(opener).fetch(
            ProviderRequest(url="https://example.com/", strategy_id="normal")
        )
        self.assertEqual(response.target_status, 200)
        self.assertEqual(response.provider_status, 200)
        self.assertEqual(response.cost.credits, Decimal("1"))
        self.assertEqual(response.cost.remaining, Decimal("40194"))
        self.assertTrue(response.cost.attributed)
        self.assertEqual(response.detected_defense, "CLOUDFLARE")

    def test_target_status_is_taken_from_the_provider_header_not_the_envelope(self) -> None:
        # The site said 404 while the provider call itself succeeded. Reading the
        # envelope would report a live URL as a provider failure and vice versa.
        headers = {**LIVE_OK_HEADERS, "scrape.do-initial-status-code": "404"}
        response = provider(FakeOpener(200, headers)).fetch(
            ProviderRequest(url="https://example.com/gone", strategy_id="normal")
        )
        self.assertEqual(response.target_status, 404)
        self.assertTrue(response.provider_ok)

    def test_a_dead_target_is_a_dead_url_and_never_pays_again(self) -> None:
        # Measured live: a 404 still costs a credit. That is exactly why triage
        # must refuse to escalate it.
        headers = {**LIVE_OK_HEADERS, "scrape.do-initial-status-code": "404"}
        response = provider(FakeOpener(404, headers)).fetch(
            ProviderRequest(url="https://example.com/gone", strategy_id="normal")
        )
        verdict = classify_response(
            status=response.target_status, body=b"not found", rules=ContentRules(min_body_bytes=1)
        )
        self.assertEqual(verdict.verdict, Verdict.DEAD_URL)
        self.assertFalse(verdict.paid_escalation_allowed)
        self.assertEqual(response.cost.credits, Decimal("1"))

    def test_provider_success_is_not_data_success(self) -> None:
        # A 200 from the provider carrying a challenge page is still a block.
        challenge = b"<html><title>Just a moment...</title>checking your browser</html>"
        response = provider(FakeOpener(200, LIVE_OK_HEADERS, challenge)).fetch(
            ProviderRequest(url="https://x.example/a", strategy_id="super")
        )
        self.assertTrue(response.provider_ok)
        verdict = classify_response(status=response.target_status, body=response.body)
        self.assertEqual(verdict.verdict, Verdict.SOFT_BLOCK)


class CostTests(unittest.TestCase):
    def test_a_missing_cost_header_is_unattributed_not_free(self) -> None:
        headers = {k: v for k, v in LIVE_OK_HEADERS.items() if k != "scrape.do-request-cost"}
        response = provider(FakeOpener(200, headers)).fetch(
            ProviderRequest(url="https://x.example/a", strategy_id="normal")
        )
        self.assertFalse(response.cost.attributed)
        self.assertEqual(response.cost.credits, Decimal("0"))

    def test_a_malformed_cost_is_unattributed(self) -> None:
        self.assertFalse(ProviderCost.parse("not-a-number").attributed)

    def test_nominal_costs_match_what_was_measured(self) -> None:
        measured = {"normal": "1", "render": "5", "super": "10"}
        by_id = {s.id: s for s in ScrapeDoProvider(token="x").strategies()}
        for strategy_id, cost in measured.items():
            self.assertEqual(str(by_id[strategy_id].nominal_cost), cost)


class ProviderFailureTests(unittest.TestCase):
    def test_credential_rejection_is_an_auth_error(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            provider(FakeOpener(401, {})).fetch(
                ProviderRequest(url="https://x.example/a", strategy_id="normal")
            )
        self.assertEqual(caught.exception.kind, ProviderErrorKind.AUTH)

    def test_rate_limiting_is_a_quota_error_and_retryable(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            provider(FakeOpener(429, {})).fetch(
                ProviderRequest(url="https://x.example/a", strategy_id="normal")
            )
        self.assertEqual(caught.exception.kind, ProviderErrorKind.QUOTA)
        self.assertTrue(caught.exception.retryable)

    def test_a_provider_fault_is_not_reported_as_a_target_failure(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            provider(FakeOpener(502, {})).fetch(
                ProviderRequest(url="https://x.example/a", strategy_id="normal")
            )
        self.assertEqual(caught.exception.kind, ProviderErrorKind.PROVIDER_FAULT)

    def test_an_origin_5xx_relayed_by_the_provider_is_a_target_verdict(self) -> None:
        # The provider did its job; the SITE is down. Distinguished by the
        # presence of the target-status header.
        headers = {**LIVE_OK_HEADERS, "scrape.do-initial-status-code": "503"}
        response = provider(FakeOpener(503, headers)).fetch(
            ProviderRequest(url="https://x.example/a", strategy_id="normal")
        )
        verdict = classify_response(status=response.target_status, body=b"down")
        self.assertEqual(verdict.verdict, Verdict.ORIGIN_DOWN)
        self.assertFalse(verdict.paid_escalation_allowed)


class SecretHygieneTests(unittest.TestCase):
    def test_the_token_never_appears_in_the_translated_response(self) -> None:
        opener = FakeOpener(200, LIVE_OK_HEADERS)
        response = provider(opener).fetch(
            ProviderRequest(url="https://x.example/a", strategy_id="normal")
        )
        self.assertNotIn("TESTTOKEN", str(response.to_dict()))

    def test_a_token_comes_only_from_an_argument_or_the_named_env_var(self) -> None:
        # Config files and profiles get committed; a credential must not be
        # reachable from them. Behavioural check, not a grep of the source.
        import os

        os.environ["PARSESUNIX_TEST_TOKEN"] = "FROM-ENV"
        self.addCleanup(os.environ.pop, "PARSESUNIX_TEST_TOKEN", None)

        self.assertTrue(ScrapeDoProvider(token="EXPLICIT").configured)
        self.assertTrue(ScrapeDoProvider(token_env="PARSESUNIX_TEST_TOKEN").configured)
        # A different variable name yields nothing: there is no fallback source.
        self.assertFalse(ScrapeDoProvider(token_env="PARSESUNIX_UNSET_VAR").configured)


if __name__ == "__main__":
    unittest.main()
