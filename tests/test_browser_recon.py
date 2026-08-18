from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.probe.browser import (
    BROWSER_RECON_SCHEMA,
    BrowserUnavailable,
    CapturedResponse,
    browser_recon,
    extract_candidates,
    find_field_paths,
    sanitize_headers,
    should_run_browser,
)

PUBLIC_RESOLVER = lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", 443))]  # noqa: E731


def capture(url: str, json_body, *, same_site: bool = True) -> CapturedResponse:
    return CapturedResponse(
        url=url,
        method="GET",
        status=200,
        content_type="application/json",
        same_site=same_site,
        request_headers={},
        body_bytes=1024,
        json_body=json_body,
    )


class SanitizeTests(unittest.TestCase):
    def test_sensitive_headers_are_removed_case_insensitively(self) -> None:
        headers = {
            "Cookie": "session=abc",
            "AUTHORIZATION": "Bearer zzz",
            "X-Api-Key": "k",
            "x-csrf-token": "t",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        cleaned = sanitize_headers(headers)
        self.assertEqual(set(cleaned), {"Content-Type", "Accept"})


class FieldMatchTests(unittest.TestCase):
    def test_finds_nested_fields_across_lists(self) -> None:
        payload = {"data": {"items": [{"title": "A", "publishedAt": "2026-01-01"}]}}
        found = find_field_paths(payload, ["title", "published_at"])
        self.assertEqual(found["title"], ["data.items[].title"])
        self.assertEqual(found["published_at"], ["data.items[].publishedAt"])

    def test_ranking_prefers_responses_with_more_target_fields(self) -> None:
        rich = capture(
            "https://demo-store.example/api/catalog", {"items": [{"title": "A", "price": 10}]}
        )
        poor = capture("https://demo-store.example/api/suggest", {"title": "only"})
        third_party = capture(
            "https://analytics.example/collect", {"title": "x", "price": 1}, same_site=False
        )
        not_json = CapturedResponse(
            url="https://demo-store.example/api/blob",
            method="GET",
            status=200,
            content_type="application/json",
            same_site=True,
            request_headers={},
            body_bytes=10,
            json_body=None,
        )
        candidates = extract_candidates([poor, third_party, rich, not_json], ["title", "price"])
        self.assertEqual(candidates[0].url, "https://demo-store.example/api/catalog")
        self.assertTrue(
            candidates[0].route
            == {
                "type": "json_api",
                "level": "L0",
                "url": "https://demo-store.example/api/catalog",
                "source": "browser-recon",
            }
        )
        self.assertEqual(
            [c.url for c in candidates[1:]],
            [
                "https://analytics.example/collect",
                "https://demo-store.example/api/suggest",
            ],
        )


class GateTests(unittest.TestCase):
    def test_ssr_report_skips_browser(self) -> None:
        report = {
            "rendering": {"classification": "ssr"},
            "recommendation": {"needs_browser_recon": False},
        }
        run, reason = should_run_browser(report)
        self.assertFalse(run)
        self.assertIn("ssr", reason)

    def test_csr_report_runs_browser(self) -> None:
        report = {
            "rendering": {"classification": "csr"},
            "recommendation": {"needs_browser_recon": True},
        }
        run, _ = should_run_browser(report)
        self.assertTrue(run)

    def test_recon_without_static_probe_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            browser_recon("https://1.1.1.1/", target_fields=["title"], resolver=PUBLIC_RESOLVER)

    def test_recon_skips_ssr_without_needing_playwright(self) -> None:
        report = {
            "rendering": {"classification": "ssr"},
            "recommendation": {"needs_browser_recon": False},
        }
        recon = browser_recon(
            "https://1.1.1.1/",
            target_fields=["title"],
            static_report=report,
            resolver=PUBLIC_RESOLVER,
        )
        self.assertEqual(recon.schema, BROWSER_RECON_SCHEMA)
        self.assertFalse(recon.executed)
        self.assertEqual(recon.candidates, ())

    def test_forced_recon_without_playwright_raises_install_hint(self) -> None:
        try:
            import playwright  # noqa: F401
        except ImportError:
            pass
        else:
            self.skipTest("playwright is installed; the unavailable path cannot be exercised")
        with self.assertRaises(BrowserUnavailable) as caught:
            browser_recon(
                "https://1.1.1.1/", target_fields=["title"], force=True, resolver=PUBLIC_RESOLVER
            )
        self.assertIn("playwright", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main()
