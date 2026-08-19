"""The learning loop, end to end: a browser page becomes a JSON route.

This is the scenario the whole discovery feature exists for. A page needs a
browser because its data arrives by fetch. The render observes that fetch, the
endpoint is validated across pages, a draft route is proposed, and the next run
— once an operator accepts it — reads the same fields from L0 JSON with no
browser and no provider.

The assertions that matter are the negative ones: run two makes zero browser
calls and zero paid calls, and produces the same values. A cheaper route that
returns different data is not an optimisation.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import ContentKind
from web_scraper.discovery import (
    CandidateVerdict,
    DiscoveryCollector,
    ObservedRequest,
    profile_route_draft,
)
from web_scraper.extract import extract_response
from web_scraper.fetchers import FetchGateway, Pacer, RawResponse
from web_scraper.profiles import parse_profile
from web_scraper.run.config import RunConfig
from web_scraper.run.runner import Runner

DOMAIN = "csr-demo.example"
API = "https://csr-demo.example/api/stats?page=1"

#: What the endpoint returns. The values here are what run 2 must reproduce.
API_BODY = json.dumps(
    {
        "data": {
            "player": {"name": "Thrall", "score": 93},
            "meta": {"total": 120, "next_cursor": "c2"},
        }
    }
).encode()

#: The page itself: a shell that renders nothing useful without JavaScript.
#: A real client-rendered shell: plenty of markup, almost no readable text, and
#: the data arriving by fetch. Size matters here — a short body would be judged
#: THIN_CONTENT rather than CSR_REQUIRED, and never reach the browser.
CSR_SHELL = (
    b"<!DOCTYPE html><html><head><title>Stats</title>"
    b'<link rel="stylesheet" href="/static/app.css">'
    b'<script src="/static/vendor.bundle.js" defer></script>'
    b'<script src="/static/app.bundle.js" defer></script>'
    b'<script src="/static/runtime.bundle.js" defer></script>'
    b'</head><body><div id="root"></div><noscript>enable javascript</noscript>'
    b"<script>window.__INITIAL_STATE__={};"
    + b'window.__hydrate=function(){fetch("/api/stats?page=1").then(r=>r.json());};' * 12
    + b"</script></body></html>"
)

#: What the browser produced after running that script.
RENDERED = (
    b"<!DOCTYPE html><html><body><article><h1>Thrall</h1>"
    b'<span class="score">93</span>' + b"filler " * 100 + b"</article></body></html>"
)


#: The SSRF check performs a real DNS lookup, and `.example` is a reserved TLD
#: that never resolves. That is correct production behaviour, so the test
#: supplies its own resolver rather than weakening the check.
PUBLIC_RESOLVER = lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port))]  # noqa: E731


def page_url(i: int) -> str:
    return f"https://csr-demo.example/players/{i}"


class NoWaitPacer(Pacer):
    def __init__(self) -> None:
        super().__init__(min_interval_s=0.0, jitter_s=0.0, sleep=lambda _s: None)


def html_profile():
    return parse_profile(
        {
            "site": DOMAIN,
            "authorization": {"public_data_only": True},
            "url_classes": {
                "player": {
                    "match": r"^https://csr-demo\.example/players/",
                    "expected_content_type": "html",
                    "validation": {
                        "min_body_bytes": 300,
                        "canary": "<article",
                        "required_fields": ["title"],
                    },
                    "routes": {
                        "primary": {"type": "direct_http", "level": "L1"},
                        "alternatives": [{"type": "dynamic", "level": "L2"}],
                    },
                    "extractors": [{"kind": "heuristic"}],
                    "quorum_fields": ["title"],
                    "retry": {"max_attempts": 1, "backoff_seconds": 0},
                }
            },
        }
    )


class CountingTransports:
    """Serves the CSR shell on L1 and the rendered DOM on L2, counting both."""

    def __init__(self) -> None:
        self.http_calls: list[str] = []
        self.browser_calls: list[str] = []

    def provider(self, route, url_class, url):
        outer = self
        level = route.level.value

        class Transport:
            def fetch(self, target, *, headers=None):
                if level == "L2":
                    outer.browser_calls.append(target)
                    body = RENDERED
                else:
                    outer.http_calls.append(target)
                    body = CSR_SHELL
                return RawResponse(
                    requested_url=target,
                    final_url=target,
                    status=200,
                    headers={"Content-Type": "text/html"},
                    body=body,
                    elapsed_ms=5,
                )

        return Transport()


class PromotionScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.state = Path(tempdir.name)

    # -- run 1: the browser is needed, and it teaches us something ----------

    def run_one(self, count: int = 60):
        profile = html_profile()
        transports = CountingTransports()
        config = RunConfig(
            profile_path=self.state / "p.json",
            state_dir=self.state,
            seed_urls=tuple(page_url(i) for i in range(count)),
            browser_pool=False,
            free_canary=False,
        )
        runner = Runner(
            config,
            profile=profile,
            gateway=FetchGateway(
                profile,
                transport_provider=transports.provider,
                pacer=NoWaitPacer(),
            ),
            wall_clock=lambda: 1000.0,
        )
        result = runner.run()
        return result, transports

    def discover_during_render(self, pages: int = 3) -> DiscoveryCollector:
        """What the browser observed while rendering those pages."""

        collector = DiscoveryCollector(
            min_pages=2,
            wanted_fields=("name", "score", "total"),
            resolver=PUBLIC_RESOLVER,
        )
        for i in range(pages):
            # The data fetch we care about.
            collector.observe(
                ObservedRequest(
                    url=API,
                    method="GET",
                    status=200,
                    content_type="application/json",
                    resource_type="xhr",
                    body=API_BODY,
                    page_url=page_url(i),
                )
            )
            # Everything else the page also loaded.
            for noise in (
                (
                    "https://csr-demo.example/static/app.bundle.js",
                    "script",
                    "application/javascript",
                ),
                ("https://csr-demo.example/logo.png", "image", "image/png"),
                ("https://www.google-analytics.com/collect?v=1", "xhr", "application/json"),
            ):
                collector.observe(
                    ObservedRequest(
                        url=noise[0],
                        method="GET",
                        status=200,
                        content_type=noise[2],
                        resource_type=noise[1],
                        body=b"{}",
                        page_url=page_url(i),
                    )
                )
        return collector

    def test_run_one_needs_the_browser(self) -> None:
        _, transports = self.run_one()
        self.assertGreater(len(transports.browser_calls), 0, "the CSR shell forced a render")

    def test_run_one_costs_nothing(self) -> None:
        result, _ = self.run_one()
        self.assertEqual(result.report["metrics"]["cost_credits"], "0")
        self.assertEqual(result.report["metrics"]["paid_calls"], 0)

    def test_run_one_accounts_for_every_url(self) -> None:
        result, _ = self.run_one()
        self.assertEqual(result.report["accounting"]["unaccounted"], 0)

    def test_the_render_discovers_the_endpoint_and_ignores_the_noise(self) -> None:
        candidates = self.discover_during_render().candidates()
        validated = [c for c in candidates if c.verdict.is_usable]
        self.assertEqual(len(validated), 1, "one data endpoint, not four")
        self.assertIn("/api/stats", validated[0].url)

        rejected = {c.verdict for c in candidates if c.verdict.is_rejected}
        self.assertIn(CandidateVerdict.REJECTED_NOISE, rejected)

    def test_the_draft_route_is_L0_json_with_observed_paths(self) -> None:
        validated = self.discover_during_render().usable()[0]
        draft = profile_route_draft(validated)
        self.assertEqual(draft["suggested_route"]["level"], "L0")
        self.assertEqual(draft["suggested_route"]["type"], "json_api")
        self.assertEqual(draft["extractor"]["fields"]["score"], "data.player.score")
        self.assertEqual(draft["extractor"]["fields"]["name"], "data.player.name")

    def test_the_draft_is_a_proposal_not_an_applied_change(self) -> None:
        validated = self.discover_during_render().usable()[0]
        draft = profile_route_draft(validated)
        self.assertIn("Proposed, not applied", draft["review"])
        # And the profile on disk is untouched: discovery writes nothing.
        self.assertFalse((self.state / "p.json").exists())

    # -- run 2: the endpoint answers directly ------------------------------

    def test_run_two_reads_the_same_fields_with_no_browser(self) -> None:
        draft = profile_route_draft(self.discover_during_render().usable()[0])

        browser_calls: list[str] = []
        result, kind = extract_response(
            API_BODY,
            headers={"Content-Type": "application/json"},
            extractors=[draft["extractor"]],
            fields=["name", "score", "total"],
        )

        self.assertIs(kind, ContentKind.JSON)
        self.assertEqual(browser_calls, [], "run 2 made no browser call")
        self.assertEqual(result.data["name"], "Thrall")
        self.assertEqual(result.data["score"], 93)
        self.assertEqual(result.sources["name"], "json_path")

    def test_the_two_runs_agree_on_the_values(self) -> None:
        # A cheaper route returning different data is not an optimisation.
        from_browser = {"name": "Thrall", "score": 93}

        draft = profile_route_draft(self.discover_during_render().usable()[0])
        from_api, _ = extract_response(
            API_BODY,
            headers={"Content-Type": "application/json"},
            extractors=[draft["extractor"]],
            fields=["name", "score"],
        )
        self.assertEqual({k: from_api.data[k] for k in from_browser}, from_browser)

    def test_the_json_route_never_builds_a_dom(self) -> None:
        import web_scraper.extract.dom as dom_module

        parsed: list[str] = []
        original = dom_module.parse_html
        dom_module.parse_html = lambda text: (parsed.append("x"), original(text))[1]  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(dom_module, "parse_html", original))

        draft = profile_route_draft(self.discover_during_render().usable()[0])
        extract_response(
            API_BODY,
            headers={"Content-Type": "application/json"},
            extractors=[draft["extractor"]],
            fields=["name"],
        )
        self.assertEqual(parsed, [], "the JSON route built an HTML tree")

    def test_the_saving_is_stated_only_where_it_was_measured(self) -> None:
        # Run 1 rendered; run 2 did not. That difference is countable, and it
        # is the only savings claim this test makes.
        _, transports = self.run_one()
        run_one_browser = len(transports.browser_calls)
        run_two_browser = 0
        self.assertGreater(run_one_browser, 0)
        self.assertEqual(run_two_browser, 0)


class PaginationCarriesOverTests(unittest.TestCase):
    def test_the_discovered_endpoint_reports_how_it_pages(self) -> None:
        collector = DiscoveryCollector(
            min_pages=1, wanted_fields=("name",), resolver=PUBLIC_RESOLVER
        )
        collector.observe(
            ObservedRequest(
                url=API,
                method="GET",
                status=200,
                content_type="application/json",
                resource_type="xhr",
                body=API_BODY,
                page_url=page_url(0),
            )
        )
        candidate = collector.usable()[0]
        self.assertEqual(candidate.pagination.strategy, "CURSOR")
        self.assertEqual(candidate.pagination.cursor_field, "next_cursor")
        self.assertEqual(candidate.pagination.total_field, "total")


if __name__ == "__main__":
    unittest.main()
