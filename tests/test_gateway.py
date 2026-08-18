from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Verdict  # noqa: E402
from web_scraper.fetchers import (  # noqa: E402
    FetchGateway,
    Pacer,
    RawResponse,
    TransportUnavailable,
)
from web_scraper.profiles import parse_profile  # noqa: E402
from web_scraper.storage import load_saved_response  # noqa: E402

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

    def test_access_denied_is_terminal(self) -> None:
        profile = make_profile(
            {"type": "direct_http", "level": "L1"},
            [{"type": "dynamic", "level": "L2"}],
        )
        l1 = FakeTransport({PAGE_URL: [response(PAGE_URL, 403, b"Forbidden", {})]})
        l2 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L1": l1, "L2": l2}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.ACCESS_DENIED)
        self.assertEqual(l2.calls, [])
        self.assertFalse(outcome.paid_escalation_candidate)

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
        self.assertEqual(pacer.backoffs, [30.0])  # Retry-After: 30 was honored
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
            {FEED_URL: [response(FEED_URL, 200, FEED_BODY, {"Content-Type": "application/rss+xml"})]}
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
            {"type": "json_api", "level": "L0", "url": "https://demo-news.example/api/articles/{id}"},
            [{"type": "direct_http", "level": "L1"}],
        )
        l1 = FakeTransport({PAGE_URL: [fixture_response("success")]})
        outcome = gateway_for(profile, {"L1": l1}).fetch_url(PAGE_URL)
        self.assertEqual(outcome.result.verdict, Verdict.OK)
        self.assertEqual(outcome.result.attempts[0].level.value, "L1")
        reasons = " ".join(item["reason"] for item in outcome.skipped_routes)
        self.assertIn("unresolved placeholders", reasons)

    def test_no_url_class_match_is_an_explicit_error(self) -> None:
        profile = make_profile({"type": "direct_http", "level": "L1"})
        with self.assertRaises(ValueError):
            gateway_for(profile, {}).fetch_url("https://other.example/page")


if __name__ == "__main__":
    unittest.main()
