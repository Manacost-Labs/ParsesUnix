from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Verdict
from web_scraper.fetchers import (
    FetchGateway,
    Pacer,
    RawResponse,
    TransportUnavailable,
)
from web_scraper.profiles import parse_profile
from web_scraper.storage import load_saved_response

FIXTURES = ROOT / "tests" / "fixtures"
PAGE_URL = "https://demo-news.example/articles/solar-farm-riverton"
FEED_URL = "https://demo-news.example/feed.xml"
API_URL = "https://demo-news.example/api/articles/48211"

FEED_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<rss version="2.0"><channel><title>Demo News</title>'
    b"<item><title>Solar farm opens near Riverton</title></item>"
    b"</channel></rss>"
)


def fixture_response(scenario: str, url: str = PAGE_URL) -> RawResponse:
    saved = load_saved_response(FIXTURES / scenario)
    return RawResponse(
        requested_url=url,
        final_url=url,
        status=saved.status,
        headers=saved.headers,
        body=saved.body,
        elapsed_ms=7,
    )


def response(url: str, status: int, body: bytes, headers: dict[str, str]) -> RawResponse:
    return RawResponse(
        requested_url=url, final_url=url, status=status, headers=headers, body=body, elapsed_ms=7
    )


class FakeTransport:
    """Returns queued responses per URL; the last one repeats."""

    def __init__(self, responses: dict[str, list[RawResponse]]) -> None:
        self._responses = {url: list(items) for url, items in responses.items()}
        self.calls: list[str] = []

    def fetch(self, url: str, *, headers=None) -> RawResponse:
        self.calls.append(url)
        queue = self._responses[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class RecordingPacer(Pacer):
    def __init__(self) -> None:
        self.pauses: list[str] = []
        self.backoffs: list[float] = []
        super().__init__(min_interval_s=0.0, jitter_s=0.0, sleep=lambda _s: None)

    def pause(self, domain: str) -> float:
        self.pauses.append(domain)
        return 0.0

    def backoff(self, seconds: float) -> float:
        self.backoffs.append(seconds)
        return seconds


def make_profile(primary: dict, alternatives: list[dict] | None = None, **overrides) -> object:
    url_class = {
        "match": "^https://demo-news\\.example/",
        "expected_content_type": "html",
        "validation": {"min_body_bytes": 500, "canary": "<article"},
        "routes": {"primary": primary, "alternatives": alternatives or []},
        "extractors": [{"kind": "json_ld", "schema_type": "Article"}],
        "retry": {"max_attempts": 2, "backoff_seconds": 5},
    }
    url_class.update(overrides)
    return parse_profile(
        {
            "site": "demo-news.example",
            "authorization": {"public_data_only": True},
            "url_classes": {"article": url_class},
        }
    )


def gateway_for(profile, transports_by_level: dict[str, FakeTransport], pacer=None):
    def provider(route, url_class, url):
        transport = transports_by_level.get(route.level.value)
        if transport is None:
            raise TransportUnavailable(f"no transport for {route.level.value}")
        return transport

    return FetchGateway(profile, transport_provider=provider, pacer=pacer or RecordingPacer())


class GatewayHappyPathTests(unittest.TestCase):
    def test_ssr_page_resolves_at_l1_without_escalation(self) -> None:
        profile = make_profile({"type": "direct_http", "level": "L1"})
        l1 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L1": l1}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertEqual(len(outcome.result.attempts), 1)
        self.assertFalse(outcome.paid_escalation_candidate)
        self.assertEqual(outcome.response.body, fixture_response("success").body)

    def test_json_api_primary_resolves_at_l0(self) -> None:
        profile = make_profile(
            {"type": "json_api", "level": "L0", "url": API_URL},
            validation={"min_body_bytes": 2, "required_json_paths": ["data.title"]},
        )
        l0 = FakeTransport(
            {
                API_URL: [
                    response(
                        API_URL,
                        200,
                        b'{"data": {"title": "Solar farm opens near Riverton"}}',
                        {"Content-Type": "application/json"},
                    )
                ]
            }
        )
        outcome = gateway_for(profile, {"L0": l0}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertEqual(outcome.result.attempts[0].level.value, "L0")


class GatewayNeverEscalatesTests(unittest.TestCase):
    def test_dead_url_is_terminal(self) -> None:
        profile = make_profile(
            {"type": "direct_http", "level": "L1"},
            [{"type": "dynamic", "level": "L2"}],
        )
        l1 = FakeTransport({PAGE_URL: [fixture_response("dead-url")]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L1": l1, "L2": l2}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.DEAD_URL)
        self.assertEqual(len(outcome.result.attempts), 1)
        self.assertEqual(l2.calls, [])

    def test_access_denied_message_is_terminal(self) -> None:
        profile = make_profile(
            {"type": "direct_http", "level": "L1"},
            [{"type": "dynamic", "level": "L2"}],
        )
        l1 = FakeTransport({PAGE_URL: [response(PAGE_URL, 403, b"Login required to continue", {})]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L1": l1, "L2": l2}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.ACCESS_DENIED)
        self.assertEqual(l2.calls, [])
        self.assertFalse(outcome.paid_escalation_candidate)

    def test_bare_403_escalates_to_browser(self) -> None:
        profile = make_profile(
            {"type": "direct_http", "level": "L1"},
            [{"type": "dynamic", "level": "L2"}],
        )
        l1 = FakeTransport({PAGE_URL: [response(PAGE_URL, 403, b"Forbidden", {})]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L1": l1, "L2": l2}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertEqual(
            [(a.level.value, a.verdict.value) for a in outcome.result.attempts],
            [("L1", "BLOCKED"), ("L2", "OK")],
        )

    def test_rate_limit_retries_with_retry_after_and_never_escalates(self) -> None:
        profile = make_profile(
            {"type": "direct_http", "level": "L1"},
            [{"type": "dynamic", "level": "L2"}],
        )
        pacer = RecordingPacer()
        l1 = FakeTransport({PAGE_URL: [fixture_response("rate-limited")]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L1": l1, "L2": l2}, pacer).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.RATE_LIMITED)
        self.assertEqual(len(outcome.result.attempts), 2)  # retry same route
        # Retry-After: 30 honored on both attempts, including the terminal one so
        # the next same-domain route/URL does not immediately re-hit the target.
        self.assertEqual(pacer.backoffs, [30.0, 30.0])
        self.assertEqual(l2.calls, [])
        skipped_reasons = " ".join(item["reason"] for item in outcome.skipped_routes)
        self.assertIn("escalation is not justified", skipped_reasons)

    def test_origin_down_retries_with_backoff_and_never_escalates(self) -> None:
        profile = make_profile(
            {"type": "direct_http", "level": "L1"},
            [{"type": "dynamic", "level": "L2"}],
        )
        pacer = RecordingPacer()
        l1 = FakeTransport({PAGE_URL: [fixture_response("origin-down")]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L1": l1, "L2": l2}, pacer).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.ORIGIN_DOWN)
        self.assertEqual(len(outcome.result.attempts), 2)
        self.assertEqual(pacer.backoffs, [5.0])  # backoff_seconds * attempt_no
        self.assertEqual(l2.calls, [])

    def test_parse_fail_tries_cheaper_route_but_never_higher(self) -> None:
        profile = make_profile(
            {"type": "direct_http", "level": "L1"},
            [
                {"type": "rss", "level": "L0", "url": FEED_URL},
                {"type": "dynamic", "level": "L2"},
            ],
        )
        l0 = FakeTransport(
            {
                FEED_URL: [
                    response(FEED_URL, 200, FEED_BODY, {"Content-Type": "application/rss+xml"})
                ]
            }
        )
        l1 = FakeTransport({PAGE_URL: [fixture_response("redesigned")]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L0": l0, "L1": l1, "L2": l2}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertEqual(
            [(a.level.value, a.verdict.value) for a in outcome.result.attempts],
            [("L1", "PARSE_FAIL"), ("L0", "OK")],
        )
        self.assertEqual(l2.calls, [])


class GatewayEscalationTests(unittest.TestCase):
    def test_soft_block_unlocks_l2_and_csr_passes_free(self) -> None:
        profile = make_profile(
            {"type": "direct_http", "level": "L1"},
            [{"type": "dynamic", "level": "L2"}],
        )
        l1 = FakeTransport({PAGE_URL: [fixture_response("soft-block")]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L1": l1, "L2": l2}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertEqual(
            [(a.level.value, a.verdict.value) for a in outcome.result.attempts],
            [("L1", "SOFT_BLOCK"), ("L2", "OK")],
        )
        self.assertFalse(outcome.paid_escalation_candidate)

    def test_blocked_everywhere_reports_paid_candidate_without_paying(self) -> None:
        profile = make_profile(
            {"type": "direct_http", "level": "L1"},
            [
                {"type": "dynamic", "level": "L2"},
                {"type": "provider", "level": "L3", "provider": "scrape.do"},
            ],
        )
        l1 = FakeTransport({PAGE_URL: [fixture_response("blocked")]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("blocked")]})
        outcome = gateway_for(profile, {"L1": l1, "L2": l2}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.BLOCKED)
        self.assertTrue(outcome.paid_escalation_candidate)
        paid_skips = [s for s in outcome.skipped_routes if s["route"]["level"] == "L3"]
        self.assertEqual(len(paid_skips), 1)
        self.assertIn("provider adapters", paid_skips[0]["reason"])

    def test_unavailable_transport_skips_route_instead_of_crashing(self) -> None:
        profile = make_profile(
            {"type": "direct_http", "level": "L1"},
            [{"type": "dynamic", "level": "L2"}],
        )
        l1 = FakeTransport({PAGE_URL: [fixture_response("soft-block")]})
        outcome = gateway_for(profile, {"L1": l1}).fetch_url(PAGE_URL)  # no L2 transport
        self.assertEqual(outcome.result.verdict, Verdict.SOFT_BLOCK)
        reasons = " ".join(item["reason"] for item in outcome.skipped_routes)
        self.assertIn("no transport for L2", reasons)

    def test_unresolvable_template_primary_falls_through_to_next_route(self) -> None:
        profile = make_profile(
            {
                "type": "json_api",
                "level": "L0",
                "url": "https://demo-news.example/api/articles/{id}",
            },
            [{"type": "direct_http", "level": "L1"}],
        )
        l1 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L1": l1}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertEqual(outcome.result.attempts[0].level.value, "L1")
        reasons = " ".join(item["reason"] for item in outcome.skipped_routes)
        self.assertIn("unresolved placeholders", reasons)

    def test_l0_block_reaches_l2_when_only_l2_alternative_exists(self) -> None:
        # The regression the +1 rule caused: L0 BLOCKED must reach an L2
        # alternative even with no L1 route between them.
        profile = make_profile(
            {"type": "json_api", "level": "L0", "url": API_URL},
            [{"type": "dynamic", "level": "L2"}],
        )
        l0 = FakeTransport({API_URL: [fixture_response("blocked", API_URL)]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L0": l0, "L2": l2}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertEqual(
            [(a.level.value, a.verdict.value) for a in outcome.result.attempts],
            [("L0", "BLOCKED"), ("L2", "OK")],
        )

    def test_unsafe_route_url_is_skipped_not_crashed(self) -> None:
        from web_scraper.probe.safety import UnsafeTarget

        class RaisingTransport:
            def fetch(self, url, *, headers=None):
                raise UnsafeTarget("target resolves to a non-public address: 10.0.0.7")

        profile = make_profile(
            {"type": "json_api", "level": "L0", "url": "https://demo-news.example/api/x"},
            [{"type": "direct_http", "level": "L1"}],
        )
        l1 = FakeTransport({PAGE_URL: [fixture_response("success")]})

        def provider(route, url_class, url):
            return RaisingTransport() if route.level.value == "L0" else l1

        gw = FetchGateway(profile, transport_provider=provider, pacer=RecordingPacer())
        outcome = gw.fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.OK)  # fell through to L1
        reasons = " ".join(s["reason"] for s in outcome.skipped_routes)
        self.assertIn("unsafe or invalid route URL", reasons)

    def test_an_identical_duplicate_route_is_rejected_by_the_validator(self) -> None:
        from web_scraper.profiles import ProfileError

        # Since routes carry an identity, an exact duplicate is now caught before
        # any network access rather than silently deduplicated at run time.
        with self.assertRaises(ProfileError) as caught:
            make_profile(
                {"type": "direct_http", "level": "L1"},
                [{"type": "direct_http", "level": "L1"}],
            )
        self.assertTrue(any("route identity" in error for error in caught.exception.errors))

    def test_two_identities_pointing_at_one_target_are_fetched_once(self) -> None:
        # Distinct identities the validator accepts, but the same concrete target:
        # the gateway must still not spend two requests on it.
        profile = make_profile(
            {"type": "json_api", "level": "L0", "url": API_URL, "id": "primary"},
            [{"type": "json_api", "level": "L0", "url": API_URL, "id": "mirror"}],
        )
        l0 = FakeTransport(
            {API_URL: [response(API_URL, 200, b"{}", {"Content-Type": "application/json"})]}
        )
        outcome = gateway_for(profile, {"L0": l0}).fetch_url(PAGE_URL)
        self.assertEqual(len(l0.calls), 1)
        self.assertIn("duplicate route", " ".join(s["reason"] for s in outcome.skipped_routes))

    def test_circuit_breaker_opens_after_repeated_hard_failures(self) -> None:
        from web_scraper.fetchers.circuit import CircuitBreaker

        profile = make_profile(
            {"type": "direct_http", "level": "L1", "url": None},
            retry={"max_attempts": 1, "backoff_seconds": 0},
        )
        breaker = CircuitBreaker(threshold=2)

        def gw():
            l1 = FakeTransport({PAGE_URL: [fixture_response("blocked")]})
            return FetchGateway(
                profile,
                transport_provider=lambda r, c, u: l1,
                pacer=RecordingPacer(),
                breaker=breaker,
            )

        gw().fetch_url(PAGE_URL)  # 1st hard failure
        gw().fetch_url(PAGE_URL)  # 2nd -> opens
        self.assertTrue(breaker.is_open("demo-news.example"))
        # Third call is short-circuited without touching the transport.
        blocked_transport = FakeTransport({PAGE_URL: [fixture_response("success")]})
        gw3 = FetchGateway(
            profile,
            transport_provider=lambda r, c, u: blocked_transport,
            pacer=RecordingPacer(),
            breaker=breaker,
        )
        outcome = gw3.fetch_url(PAGE_URL)
        self.assertEqual(blocked_transport.calls, [])  # not called
        self.assertIn("circuit breaker open", " ".join(s["reason"] for s in outcome.skipped_routes))

    def test_no_url_class_match_is_an_explicit_error(self) -> None:
        profile = make_profile({"type": "direct_http", "level": "L1"})
        with self.assertRaises(ValueError):
            gateway_for(profile, {}).fetch_url("https://other.example/page")


if __name__ == "__main__":
    unittest.main()


class CsrEscalationTests(unittest.TestCase):
    """The gap this verdict exists to close: a JS site must reach the browser."""

    def test_a_csr_shell_escalates_to_the_browser_and_resolves(self) -> None:
        profile = make_profile(
            {"type": "direct_http", "level": "L1"},
            [{"type": "dynamic", "level": "L2"}],
        )
        l1 = FakeTransport({PAGE_URL: [fixture_response("csr-shell", PAGE_URL)]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L1": l1, "L2": l2}).fetch_url(PAGE_URL)

        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertEqual(
            [(a.level.value, a.verdict.value) for a in outcome.result.attempts],
            [("L1", "CSR_REQUIRED"), ("L2", "OK")],
        )

    def test_a_csr_site_with_no_browser_route_never_becomes_a_paid_candidate(self) -> None:
        profile = make_profile({"type": "direct_http", "level": "L1"})
        l1 = FakeTransport({PAGE_URL: [fixture_response("csr-shell", PAGE_URL)]})
        outcome = gateway_for(profile, {"L1": l1}).fetch_url(PAGE_URL)

        self.assertEqual(outcome.result.verdict, Verdict.CSR_REQUIRED)
        # Rendering is our job, not a provider's: this must not read as "buy credits".
        self.assertFalse(outcome.paid_escalation_candidate)


class PaidEscalationWiringTests(unittest.TestCase):
    """The gateway's paid step: when it runs, and — mostly — when it does not."""

    def setUp(self) -> None:
        import tempfile
        from decimal import Decimal

        from web_scraper.budget import BudgetLedger
        from web_scraper.providers.escalation import PaidEscalator
        from web_scraper.providers.router import PaidProviderRouter

        self.Decimal = Decimal
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.budget = BudgetLedger(Path(tempdir.name) / "b.sqlite3", daily_credit_limit="100")
        self._make = lambda provider: PaidEscalator(
            provider,
            budget=self.budget,
            router=PaidProviderRouter(stats=None, _rng=lambda: 1.0),
        )

    def provider(self, body: bytes = b"", *, target_status: int = 200):
        from web_scraper.providers.base import ProviderCost, ProviderResponse
        from web_scraper.providers.scrape_do import STRATEGIES

        page = body or (b"<html><body><article>" + b"word " * 200 + b"</article></body></html>")

        class Recording:
            name = "scrape.do"

            def __init__(self) -> None:
                self.calls: list[str] = []

            def strategies(self):
                return STRATEGIES

            def fetch(self, request):
                self.calls.append(request.strategy_id)
                return ProviderResponse(
                    provider="scrape.do",
                    strategy_id=request.strategy_id,
                    target_status=target_status,
                    provider_status=200,
                    body=page,
                    headers={"Content-Type": "text/html"},
                    cost=ProviderCost.parse("5"),
                    request_id="req-9",
                )

        return Recording()

    def blocked_profile(self):
        return make_profile(
            {"type": "direct_http", "level": "L1"}, [{"type": "dynamic", "level": "L2"}]
        )

    def run_gateway(self, escalator, *, scenario: str = "blocked"):
        profile = self.blocked_profile()
        transports = {
            "L1": FakeTransport({PAGE_URL: [fixture_response(scenario)]}),
            "L2": FakeTransport({PAGE_URL: [fixture_response(scenario)]}),
        }
        gateway = FetchGateway(
            profile,
            transport_provider=lambda route, _c, _u: transports[route.level.value],
            pacer=RecordingPacer(),
            paid_escalator=escalator,
        )
        return gateway.fetch_url(PAGE_URL)

    def test_a_gateway_without_an_escalator_cannot_spend(self) -> None:
        outcome = self.run_gateway(None)
        self.assertEqual(outcome.result.verdict, Verdict.BLOCKED)
        self.assertIsNone(outcome.paid)
        self.assertEqual(outcome.cost.credits, self.Decimal("0"))
        self.assertTrue(outcome.cost.is_known, "no call happened: zero is measured, not assumed")
        self.assertEqual(self.budget.usage().credits, self.Decimal("0"))

    def test_paid_runs_only_after_every_free_route_is_spent(self) -> None:
        provider = self.provider()
        outcome = self.run_gateway(self._make(provider))
        levels = [a.level.value for a in outcome.result.attempts]
        self.assertEqual(levels, ["L1", "L2", "L3"], "L3 comes last, never first")
        self.assertEqual(len(provider.calls), 1, "at most one paid call per URL")

    def test_a_successful_paid_fetch_becomes_the_result(self) -> None:
        outcome = self.run_gateway(self._make(self.provider()))
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertIsNotNone(outcome.response)
        assert outcome.response is not None
        self.assertIn(b"<article>", outcome.response.body, "downstream sees ordinary bytes")
        self.assertEqual(outcome.response.status, 200, "the TARGET status, not the provider's")

    def test_the_cost_reported_is_the_cost_billed(self) -> None:
        outcome = self.run_gateway(self._make(self.provider()))
        self.assertEqual(outcome.cost.credits, self.Decimal("5"))
        self.assertEqual(self.budget.usage().credits, self.Decimal("5"))
        l3 = next(a for a in outcome.result.attempts if a.level.value == "L3")
        self.assertEqual(l3.cost.credits, self.Decimal("5"))
        self.assertEqual(l3.provider, "scrape.do")
        self.assertEqual(l3.request_id, "req-9")

    def test_a_free_success_never_reaches_the_provider(self) -> None:
        provider = self.provider()
        outcome = self.run_gateway(self._make(provider), scenario="success")
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertEqual(provider.calls, [], "nothing was blocked, so nothing was bought")
        self.assertIsNone(outcome.paid)

    def test_a_dead_url_never_reaches_the_provider(self) -> None:
        provider = self.provider()
        outcome = self.run_gateway(self._make(provider), scenario="dead-url")
        self.assertEqual(outcome.result.verdict, Verdict.DEAD_URL)
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.budget.usage().credits, self.Decimal("0"))

    def test_an_origin_outage_never_reaches_the_provider(self) -> None:
        provider = self.provider()
        outcome = self.run_gateway(self._make(provider), scenario="origin-down")
        self.assertEqual(outcome.result.verdict, Verdict.ORIGIN_DOWN)
        self.assertEqual(provider.calls, [])

    def test_a_refusal_to_spend_is_still_recorded(self) -> None:
        # Exhaust the budget, then confirm the URL still explains itself.
        self.budget.settle(
            self.budget.reserve(provider="scrape.do", credits=100), actual_credits=100
        )
        provider = self.provider()
        outcome = self.run_gateway(self._make(provider))
        self.assertEqual(provider.calls, [])
        self.assertIsNotNone(outcome.paid)
        assert outcome.paid is not None
        self.assertFalse(outcome.paid.attempted)
        l3 = next(a for a in outcome.result.attempts if a.level.value == "L3")
        self.assertEqual(l3.verdict, Verdict.BLOCKED, "the free verdict survives a refusal")
        self.assertIn("EXHAUSTED", l3.reason)

    def test_a_provider_that_is_also_blocked_keeps_the_block_verdict(self) -> None:
        challenge = b"<html><title>Just a moment...</title>checking your browser</html>"
        outcome = self.run_gateway(self._make(self.provider(challenge)))
        self.assertEqual(outcome.result.verdict, Verdict.SOFT_BLOCK)
        self.assertEqual(outcome.cost.credits, self.Decimal("5"), "a failed paid attempt is billed")
