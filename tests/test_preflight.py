"""Fail before the run, not at minute forty of it."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.run.config import RunConfig
from web_scraper.run.preflight import preflight, secret_leak_check

PROFILE = json.dumps(
    {
        "site": "demo.example",
        "authorization": {"public_data_only": True},
        "url_classes": {
            "page": {
                "match": "^https://demo\\.example/",
                "expected_content_type": "html",
                "validation": {"min_body_bytes": 100, "canary": "<article"},
                "routes": {"primary": {"type": "direct_http", "level": "L1"}},
                "extractors": [{"kind": "json_ld"}],
            }
        },
    }
)


class PreflightCase(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        self.profile_path = self.root / "profile.json"
        self.profile_path.write_text(PROFILE)

    def config(self, **kw) -> RunConfig:
        kw.setdefault("profile_path", self.profile_path)
        kw.setdefault("state_dir", self.root / "state")
        return RunConfig(**kw)


class BlockingTests(PreflightCase):
    def test_a_healthy_free_run_passes(self) -> None:
        report = preflight(self.config())
        self.assertTrue(report.ok, report.explain())

    def test_a_missing_profile_blocks(self) -> None:
        report = preflight(self.config(profile_path=self.root / "nope.json"))
        self.assertFalse(report.ok)
        self.assertIn("profile", [c.name for c in report.failures])

    def test_an_invalid_profile_blocks(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text('{"site": "demo.example"}')
        report = preflight(self.config(profile_path=bad))
        self.assertFalse(report.ok)

    def test_a_funded_run_without_credentials_blocks(self) -> None:
        # It would run, escalate nothing, and report poor coverage afterwards.
        # Better to say so before anything is fetched.
        import os

        for name in ("SCRAPE_DO_TOKEN", "FIRECRAWL_API_KEY", "BRIGHTDATA_API_KEY"):
            os.environ.pop(name, None)
        report = preflight(self.config(daily_credit_limit="100"))
        self.assertFalse(report.ok)
        self.assertIn("provider_credentials", [c.name for c in report.failures])


class WarningTests(PreflightCase):
    def test_a_missing_browser_warns_but_does_not_block(self) -> None:
        # A free run on a machine without Chromium is still a useful run.
        report = preflight(self.config())
        browser = next(c for c in report.checks if c.name == "browser")
        self.assertEqual(browser.severity, "warning")
        if not browser.ok:
            self.assertTrue(report.ok, "a warning never blocks")

    def test_stale_pricing_warns(self) -> None:
        import datetime as dt
        from decimal import Decimal

        from web_scraper.providers.pricing import PricingBook, PricingSnapshot, StrategyRate

        ancient = PricingSnapshot(
            provider="p",
            native_unit="credits",
            pricing_source="x",
            docs_verified_at="2019-01-01",
            effective_at="2019-01-01",
            rates={"s": StrategyRate(Decimal("1"), Decimal("0.001"))},
        )
        report = preflight(self.config(), pricing=PricingBook((ancient,)))
        pricing = next(c for c in report.checks if c.name == "pricing")
        self.assertFalse(pricing.ok)
        self.assertEqual(pricing.severity, "warning")
        self.assertTrue(report.ok)
        del dt

    def test_a_free_run_says_so_rather_than_complaining(self) -> None:
        report = preflight(self.config())
        budget = next(c for c in report.checks if c.name == "budget")
        self.assertTrue(budget.ok)
        self.assertIn("free run", budget.detail)


class SecretScanTests(unittest.TestCase):
    def test_it_catches_the_obvious_carriers(self) -> None:
        for payload in (
            '{"headers": {"Authorization: Bearer abc"}}',
            "GET /x?api_key=secret",
            "Cookie: session=abc",
        ):
            with self.subTest(payload=payload):
                self.assertTrue(secret_leak_check(payload))

    def test_a_clean_report_is_clean(self) -> None:
        self.assertEqual(secret_leak_check('{"verdict": "OK", "cost": "1"}'), [])

    def test_it_catches_a_real_token_value_from_the_environment(self) -> None:
        import os

        os.environ["SCRAPE_DO_TOKEN"] = "TESTTOKENVALUE123"
        self.addCleanup(lambda: os.environ.pop("SCRAPE_DO_TOKEN", None))
        self.assertTrue(secret_leak_check("url=...&token=TESTTOKENVALUE123"))


if __name__ == "__main__":
    unittest.main()
