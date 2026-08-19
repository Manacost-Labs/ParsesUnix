"""Discovery: what becomes a candidate, and — mostly — what does not."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.discovery import (
    CandidateVerdict,
    DiscoveryCollector,
    ObservedRequest,
    SchemaSignature,
    describe_report,
    graphql_operation_of,
    normalize_endpoint,
    pagination_hint_of,
    profile_route_draft,
    redact_url,
)

PAGE = "https://example.com/players"
STATS = json.dumps(
    {"data": {"players": [{"id": 1, "name": "Thrall", "score": 93}], "total": 120}}
).encode()


def observed(url="https://example.com/api/stats?page=1", **kw):
    base = {
        "url": url,
        "method": "GET",
        "status": 200,
        "content_type": "application/json",
        "resource_type": "xhr",
        "body": STATS,
        "page_url": PAGE,
    }
    base.update(kw)
    return ObservedRequest(**base)


class IdentityTests(unittest.TestCase):
    def test_pagination_does_not_split_one_endpoint_into_many(self) -> None:
        a = normalize_endpoint("https://e.com/api/items?page=1&sort=new")
        b = normalize_endpoint("https://e.com/api/items?page=2&sort=new")
        self.assertEqual(a, b)

    def test_id_shaped_path_segments_collapse(self) -> None:
        a = normalize_endpoint("https://e.com/api/user/1234/stats")
        b = normalize_endpoint("https://e.com/api/user/9999/stats")
        self.assertEqual(a, b)

    def test_genuinely_different_endpoints_stay_different(self) -> None:
        self.assertNotEqual(
            normalize_endpoint("https://e.com/api/players"),
            normalize_endpoint("https://e.com/api/matches"),
        )

    def test_a_meaningful_query_parameter_is_kept(self) -> None:
        self.assertNotEqual(
            normalize_endpoint("https://e.com/api/items?region=eu"),
            normalize_endpoint("https://e.com/api/items?region=us"),
        )

    def test_two_pages_of_one_endpoint_are_counted_once(self) -> None:
        collector = DiscoveryCollector(min_pages=1)
        collector.observe(observed(url="https://example.com/api/stats?page=1"))
        collector.observe(observed(url="https://example.com/api/stats?page=2"))
        self.assertEqual(len(collector.candidates()), 1)
        self.assertEqual(collector.candidates()[0].observed_count, 2)


class SecrecyTests(unittest.TestCase):
    def test_secret_query_values_are_redacted_before_storage(self) -> None:
        # Candidates end up in reports, logs and profile drafts.
        url = "https://e.com/api?token=SECRET&api_key=ALSO&page=1"
        redacted = redact_url(url)
        self.assertNotIn("SECRET", redacted)
        self.assertNotIn("ALSO", redacted)
        self.assertIn("page=1", redacted)

    def test_a_stored_candidate_carries_no_secret(self) -> None:
        collector = DiscoveryCollector(min_pages=1)
        collector.observe(observed(url="https://example.com/api/stats?apikey=LEAKME"))
        payload = json.dumps([c.to_dict() for c in collector.candidates()])
        self.assertNotIn("LEAKME", payload)

    def test_a_schema_signature_carries_shape_not_values(self) -> None:
        signature = SchemaSignature.of({"user": {"email": "a@b.c", "name": "Thrall"}})
        self.assertNotIn("a@b.c", signature.signature)
        self.assertNotIn("Thrall", signature.signature)
        self.assertIn("string", signature.signature)
        self.assertIn("email", signature.signature)

    def test_graphql_variables_are_never_captured(self) -> None:
        body = json.dumps(
            {"operationName": "Rankings", "query": "{x}", "variables": {"token": "SECRET"}}
        )
        operation = graphql_operation_of("https://e.com/graphql", body)
        self.assertEqual(operation, "Rankings")
        source = Path(ROOT / "src/web_scraper/discovery/candidates.py").read_text()
        self.assertNotIn('payload.get("variables")', source)


class RejectionTests(unittest.TestCase):
    def collector(self):
        return DiscoveryCollector(min_pages=1)

    def verdict(self, request):
        collector = self.collector()
        collector.observe(request)
        return collector.candidates()[0].verdict

    def test_an_authorised_request_is_rejected(self) -> None:
        # Rendering authorised it, not us.
        v = self.verdict(observed(request_header_names=("Authorization", "Accept")))
        self.assertIs(v, CandidateVerdict.REJECTED_AUTH)

    def test_a_cookie_bearing_request_is_rejected(self) -> None:
        self.assertIs(
            self.verdict(observed(request_header_names=("Cookie",))),
            CandidateVerdict.REJECTED_AUTH,
        )

    def test_a_csrf_token_is_treated_as_authorisation(self) -> None:
        self.assertIs(
            self.verdict(observed(request_header_names=("X-CSRF-Token",))),
            CandidateVerdict.REJECTED_AUTH,
        )

    def test_loopback_is_rejected(self) -> None:
        # A rendered page can ask the browser for anything.
        self.assertIs(
            self.verdict(observed(url="http://127.0.0.1:8080/api/stats")),
            CandidateVerdict.REJECTED_PRIVATE,
        )

    def test_the_metadata_service_is_rejected(self) -> None:
        self.assertIs(
            self.verdict(observed(url="http://169.254.169.254/latest/meta-data/")),
            CandidateVerdict.REJECTED_PRIVATE,
        )

    def test_private_ranges_are_rejected(self) -> None:
        for url in (
            "http://10.0.0.5/api/x",
            "http://192.168.1.1/api/x",
            "http://[::1]/api/x",
        ):
            with self.subTest(url=url):
                self.assertIs(self.verdict(observed(url=url)), CandidateVerdict.REJECTED_PRIVATE)

    def test_analytics_is_not_our_data(self) -> None:
        for url in (
            "https://www.google-analytics.com/collect?v=1",
            "https://api.segment.io/v1/track",
            "https://example.com/api/telemetry",
        ):
            with self.subTest(url=url):
                self.assertIs(self.verdict(observed(url=url)), CandidateVerdict.REJECTED_NOISE)

    def test_images_fonts_css_and_bundles_are_ignored(self) -> None:
        for resource in ("image", "font", "stylesheet", "script"):
            with self.subTest(resource=resource):
                self.assertIs(
                    self.verdict(observed(resource_type=resource)),
                    CandidateVerdict.REJECTED_NOISE,
                )

    def test_a_non_json_response_is_not_a_data_endpoint(self) -> None:
        self.assertIs(
            self.verdict(observed(content_type="text/html", resource_type="document")),
            CandidateVerdict.REJECTED_WRONG_SCHEMA,
        )

    def test_an_error_response_is_unstable(self) -> None:
        self.assertIs(self.verdict(observed(status=500)), CandidateVerdict.REJECTED_UNSTABLE)

    def test_an_endpoint_returning_two_shapes_is_unstable(self) -> None:
        collector = self.collector()
        collector.observe(observed(body=json.dumps({"a": 1}).encode()))
        collector.observe(observed(body=json.dumps({"totally": {"different": True}}).encode()))
        self.assertIs(collector.candidates()[0].verdict, CandidateVerdict.REJECTED_UNSTABLE)

    def test_a_rejection_is_reported_not_dropped(self) -> None:
        # "It found the API and refused it, because it needed a cookie" beats
        # silence when an operator asks why discovery found nothing.
        collector = self.collector()
        collector.observe(observed(request_header_names=("Cookie",)))
        candidate = collector.candidates()[0]
        self.assertIn("rendering authorised it", candidate.rejection_detail)


class PromotionTests(unittest.TestCase):
    def test_one_page_is_a_coincidence_not_a_route(self) -> None:
        collector = DiscoveryCollector(min_pages=2)
        collector.observe(observed())
        self.assertIs(collector.candidates()[0].verdict, CandidateVerdict.PROMISING)
        self.assertEqual(collector.usable(), [])

    def test_the_same_endpoint_across_pages_becomes_validated(self) -> None:
        collector = DiscoveryCollector(min_pages=2, wanted_fields=("name", "score"))
        collector.observe(observed(page_url="https://example.com/players/1"))
        collector.observe(observed(page_url="https://example.com/players/2"))
        candidate = collector.candidates()[0]
        self.assertIs(candidate.verdict, CandidateVerdict.VALIDATED)
        self.assertEqual(candidate.confidence, "HIGH")

    def test_a_promising_candidate_cannot_be_turned_into_a_draft(self) -> None:
        collector = DiscoveryCollector(min_pages=2)
        collector.observe(observed())
        with self.assertRaises(ValueError):
            profile_route_draft(collector.candidates()[0])

    def test_a_validated_candidate_produces_a_readable_draft(self) -> None:
        collector = DiscoveryCollector(min_pages=1, wanted_fields=("name", "score"))
        collector.observe(observed())
        draft = profile_route_draft(collector.candidates()[0])
        self.assertEqual(draft["suggested_route"]["type"], "json_api")
        self.assertEqual(draft["suggested_route"]["level"], "L0")
        self.assertEqual(draft["extractor"]["kind"], "json")
        self.assertIn("name", draft["extractor"]["fields"])
        self.assertIn("Proposed, not applied", draft["review"])

    def test_field_paths_come_from_where_they_were_observed(self) -> None:
        # A draft with guessed paths is a template, not evidence.
        collector = DiscoveryCollector(min_pages=1, wanted_fields=("score",))
        collector.observe(observed())
        draft = profile_route_draft(collector.candidates()[0])
        self.assertEqual(draft["extractor"]["fields"]["score"], "data.players[*].score")


class GraphQLTests(unittest.TestCase):
    def request(self, **kw):
        base = {
            "url": "https://example.com/graphql",
            "method": "POST",
            "content_type": "application/graphql-response+json",
            "resource_type": "fetch",
            "request_body": json.dumps({"operationName": "Rankings", "query": "{ x }"}),
            "body": json.dumps(
                {"data": {"rankings": {"pageInfo": {"hasNextPage": True, "endCursor": "abc"}}}}
            ).encode(),
        }
        base.update(kw)
        return observed(**base)

    def test_the_operation_name_is_captured(self) -> None:
        collector = DiscoveryCollector(min_pages=1)
        collector.observe(self.request())
        self.assertEqual(collector.candidates()[0].graphql_operation, "Rankings")

    def test_two_operations_on_one_endpoint_are_two_candidates(self) -> None:
        collector = DiscoveryCollector(min_pages=1)
        collector.observe(self.request())
        collector.observe(
            self.request(request_body=json.dumps({"operationName": "Profile", "query": "{y}"}))
        )
        self.assertEqual(len(collector.candidates()), 2)

    def test_graphql_cursor_pagination_is_recognised(self) -> None:
        collector = DiscoveryCollector(min_pages=1)
        collector.observe(self.request())
        self.assertEqual(collector.candidates()[0].pagination.strategy, "CURSOR")

    def test_a_draft_names_the_operation(self) -> None:
        collector = DiscoveryCollector(min_pages=1)
        collector.observe(self.request())
        draft = profile_route_draft(collector.candidates()[0])
        self.assertEqual(draft["suggested_route"]["graphql_operation"], "Rankings")


class PaginationTests(unittest.TestCase):
    def test_page_parameters_are_recognised(self) -> None:
        hint = pagination_hint_of("https://e.com/api?page=2", {"items": [], "page": 2})
        self.assertEqual(hint.strategy, "PAGE")

    def test_offset_parameters_are_recognised(self) -> None:
        hint = pagination_hint_of("https://e.com/api?offset=40&limit=20", {"items": []})
        self.assertEqual(hint.strategy, "OFFSET")

    def test_cursor_bodies_are_recognised(self) -> None:
        hint = pagination_hint_of("https://e.com/api", {"items": [], "next_cursor": "x"})
        self.assertEqual(hint.strategy, "CURSOR")
        self.assertEqual(hint.cursor_field, "next_cursor")

    def test_an_unpaged_endpoint_says_so(self) -> None:
        hint = pagination_hint_of("https://e.com/api", {"name": "x"})
        self.assertEqual(hint.strategy, "NONE")
        self.assertFalse(hint.is_paged)

    def test_a_total_is_captured_when_present(self) -> None:
        hint = pagination_hint_of("https://e.com/api?page=1", {"total": 120, "items": []})
        self.assertEqual(hint.total_field, "total")


class BoundsTests(unittest.TestCase):
    def test_candidates_per_page_are_capped(self) -> None:
        collector = DiscoveryCollector(min_pages=1, max_candidates=3)
        for i in range(10):
            collector.observe(observed(url=f"https://example.com/api/e{i}"))
        self.assertLessEqual(len(collector.candidates()), 3)

    def test_only_a_bounded_slice_of_a_body_is_inspected(self) -> None:
        from web_scraper.discovery.candidates import MAX_INSPECTED_BYTES

        huge = json.dumps({"x": "y" * (MAX_INSPECTED_BYTES + 1000)}).encode()
        collector = DiscoveryCollector(min_pages=1)
        collector.observe(observed(body=huge))
        # Truncated at our own ceiling: we cannot describe its shape, and
        # guessing one would be worse.
        self.assertIsNone(collector.candidates()[0].schema)


class ReportTests(unittest.TestCase):
    def test_the_listing_is_readable(self) -> None:
        collector = DiscoveryCollector(min_pages=1, wanted_fields=("name",))
        collector.observe(observed())
        collector.observe(
            observed(request_header_names=("Cookie",), url="https://example.com/api/me")
        )
        text = describe_report(collector.candidates())
        self.assertIn("structured route candidate", text)
        self.assertIn("auth:", text)
        self.assertIn("confidence:", text)

    def test_nothing_found_says_so(self) -> None:
        self.assertIn("no structured route candidates", describe_report([]))


if __name__ == "__main__":
    unittest.main()
