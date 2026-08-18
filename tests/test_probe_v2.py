from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.probe import PROBE_REPORT_SCHEMA, FetchResult, UnsafeTarget, probe  # noqa: E402
from web_scraper.probe import analysis  # noqa: E402
from web_scraper.probe.safety import ValidatingRedirectHandler  # noqa: E402
from web_scraper.profiles import parse_profile  # noqa: E402
from web_scraper.profiles.draft import draft_profile_from_probe, merge_api_candidate  # noqa: E402
from web_scraper.storage import load_saved_response  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

PUBLIC_RESOLVER = lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", 443))]  # noqa: E731
PRIVATE_RESOLVER = lambda host, port, **kw: [(2, 1, 6, "", ("10.0.0.7", 443))]  # noqa: E731

ROBOTS_BODY = b"""User-agent: *
Disallow: /admin
Crawl-delay: 5
Sitemap: https://demo-news.example/sitemap.xml
"""


def make_fetch(pages: dict[str, FetchResult]):
    def fetch(url: str) -> FetchResult:
        if url in pages:
            return pages[url]
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=404,
            headers={"Content-Type": "text/plain"},
            body=b"not found",
            truncated=False,
            redirect_chain=(),
        )

    return fetch


def fixture_fetch_result(scenario: str) -> FetchResult:
    saved = load_saved_response(FIXTURES / scenario)
    return FetchResult(
        requested_url=saved.url,
        final_url=saved.url,
        status=saved.status,
        headers=saved.headers,
        body=saved.body,
        truncated=False,
        redirect_chain=(),
    )


class AnalysisTests(unittest.TestCase):
    def test_success_page_discovery(self) -> None:
        saved = load_saved_response(FIXTURES / "success")
        discovery = analysis.discover(saved.body, saved.url, "text/html")
        self.assertIn("Article", discovery["json_ld_types"])
        self.assertEqual(
            discovery["canonical_url"],
            "https://demo-news.example/articles/solar-farm-riverton",
        )
        self.assertEqual(discovery["opengraph"].get("og:title"), "Solar farm opens near Riverton")
        self.assertEqual(len(discovery["alternates"]["rss_atom"]), 1)
        self.assertTrue(discovery["alternates"]["amp_url"].startswith("https://demo-news.example/amp/"))

    def test_success_page_is_ssr(self) -> None:
        saved = load_saved_response(FIXTURES / "success")
        discovery = analysis.discover(saved.body, saved.url, "text/html")
        rendering = analysis.classify_rendering(saved.body, "text/html", discovery["app_state"])
        self.assertEqual(rendering["classification"], saved.probe_expectations["rendering"])

    def test_csr_shell_is_csr_and_needs_browser(self) -> None:
        saved = load_saved_response(FIXTURES / "csr-shell")
        discovery = analysis.discover(saved.body, saved.url, "text/html")
        rendering = analysis.classify_rendering(saved.body, "text/html", discovery["app_state"])
        self.assertEqual(rendering["classification"], "csr")
        recommendation = analysis.recommend(discovery, rendering, {"sitemaps": []}, "OK")
        self.assertEqual(recommendation["start_level"], "L2")
        self.assertTrue(recommendation["needs_browser_recon"])

    def test_redesigned_page_is_still_ssr(self) -> None:
        saved = load_saved_response(FIXTURES / "redesigned")
        discovery = analysis.discover(saved.body, saved.url, "text/html")
        rendering = analysis.classify_rendering(saved.body, "text/html", discovery["app_state"])
        self.assertEqual(rendering["classification"], "ssr")

    def test_next_data_page_is_hybrid(self) -> None:
        body = (
            b"<html><head></head><body><main>" + b"word " * 200 +
            b'</main><script id="__NEXT_DATA__" type="application/json">{"props":{}}</script></body></html>'
        )
        app_state = analysis.extract_app_state(body.decode())
        self.assertTrue(app_state["next_data"])
        rendering = analysis.classify_rendering(body, "text/html", app_state)
        self.assertEqual(rendering["classification"], "hybrid")

    def test_api_hints_include_graphql(self) -> None:
        hints = analysis.extract_api_hints('fetch("/graphql/query?op=Articles")')
        self.assertTrue(hints["graphql"])


class RedirectSafetyTests(unittest.TestCase):
    def test_redirect_to_private_address_is_rejected(self) -> None:
        handler = ValidatingRedirectHandler(allow_private=False, resolver=PRIVATE_RESOLVER)
        request = Request("https://public.example/start")
        with self.assertRaises(UnsafeTarget):
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://internal.example/admin"
            )

    def test_allowed_redirect_is_recorded_in_chain(self) -> None:
        handler = ValidatingRedirectHandler(allow_private=False, resolver=PUBLIC_RESOLVER)
        request = Request("https://public.example/start")
        handler.redirect_request(
            request, None, 301, "Moved", {}, "https://public.example/final"
        )
        self.assertEqual(
            handler.chain,
            [{"from": "https://public.example/start", "to": "https://public.example/final", "status": 301}],
        )


class ProbeReportTests(unittest.TestCase):
    def build_report(self):
        page = fixture_fetch_result("success")
        robots = FetchResult(
            requested_url="https://demo-news.example/robots.txt",
            final_url="https://demo-news.example/robots.txt",
            status=200,
            headers={"Content-Type": "text/plain"},
            body=ROBOTS_BODY,
            truncated=False,
            redirect_chain=(),
        )
        fetch = make_fetch({page.requested_url: page, robots.requested_url: robots})
        return probe(page.requested_url, fetch=fetch, resolver=PUBLIC_RESOLVER)

    def test_report_contract_is_stable(self) -> None:
        report = self.build_report()
        payload = report.to_dict()
        self.assertEqual(payload["schema"], PROBE_REPORT_SCHEMA)
        for key in (
            "requested_url",
            "final_url",
            "status",
            "verdict",
            "reason",
            "redirect_chain",
            "headers",
            "fetch",
            "robots",
            "discovery",
            "rendering",
            "recommendation",
        ):
            self.assertIn(key, payload)
        json.dumps(payload)  # the whole report must be JSON-serializable

    def test_report_hash_and_robots(self) -> None:
        report = self.build_report()
        saved = load_saved_response(FIXTURES / "success")
        self.assertEqual(report.fetch["sha256"], hashlib.sha256(saved.body).hexdigest())
        self.assertEqual(report.robots["sitemaps"], ["https://demo-news.example/sitemap.xml"])
        self.assertEqual(report.robots["crawl_delay"], 5)
        self.assertTrue(report.robots["target_allowed"])  # /articles/ not disallowed
        self.assertEqual(report.verdict, "OK")

    def test_robots_disallow_is_reported(self) -> None:
        page = fixture_fetch_result("success")
        disallow = FetchResult(
            requested_url="https://demo-news.example/robots.txt",
            final_url="https://demo-news.example/robots.txt",
            status=200,
            headers={"Content-Type": "text/plain"},
            body=b"User-agent: *\nDisallow: /articles/\n",
            truncated=False,
            redirect_chain=(),
        )
        fetch = make_fetch({page.requested_url: page, disallow.requested_url: disallow})
        report = probe(page.requested_url, fetch=fetch, resolver=PUBLIC_RESOLVER)
        self.assertFalse(report.robots["target_allowed"])

    def test_recommendation_prefers_l0_for_discovered_feed(self) -> None:
        report = self.build_report()
        self.assertEqual(report.recommendation["start_level"], "L0")
        types = {route["type"] for route in report.recommendation["candidate_routes"]}
        self.assertIn("rss", types)
        self.assertIn("sitemap", types)


class DraftProfileTests(unittest.TestCase):
    def build_report(self, scenario: str):
        page = fixture_fetch_result(scenario)
        fetch = make_fetch({page.requested_url: page})
        return probe(page.requested_url, fetch=fetch, resolver=PUBLIC_RESOLVER, include_robots=False)

    def test_draft_from_success_report_is_valid(self) -> None:
        report = self.build_report("success")
        draft = draft_profile_from_probe(report, url_class="article", required_fields=("title",))
        profile = parse_profile(draft)  # must not raise
        self.assertEqual(profile.site, "demo-news.example")
        article = profile.url_classes["article"]
        self.assertEqual(article.primary_route.level.value, "L0")
        self.assertTrue(article.matches("https://demo-news.example/articles/another-story"))
        kinds = [extractor["kind"] for extractor in article.extractors]
        self.assertEqual(kinds[0], "json_ld")
        self.assertIn("meta", kinds)

    def test_draft_from_csr_report_starts_at_l2(self) -> None:
        report = self.build_report("csr-shell")
        draft = draft_profile_from_probe(report, url_class="catalog")
        profile = parse_profile(draft)
        self.assertEqual(profile.url_classes["catalog"].primary_route.level.value, "L2")

    def test_browser_candidate_promotes_csr_draft_to_l0(self) -> None:
        report = self.build_report("csr-shell")
        draft = draft_profile_from_probe(report, url_class="catalog")
        candidate = {"route": {"type": "json_api", "level": "L0", "url": "https://demo-store.example/api/catalog?page=1"}}
        merged = merge_api_candidate(draft, "catalog", candidate)
        profile = parse_profile(merged)
        catalog = profile.url_classes["catalog"]
        self.assertEqual(catalog.primary_route.level.value, "L0")
        self.assertEqual(catalog.primary_route.type.value, "json_api")
        self.assertEqual(catalog.alternative_routes[0].level.value, "L2")


if __name__ == "__main__":
    unittest.main()
