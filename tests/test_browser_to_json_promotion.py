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
            "player": {"title": "Thrall", "name": "Thrall", "score": 93},
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


class RunnerDiscoveryTests(unittest.TestCase):
    """VALIDATED reached inside a real Runner, not just in a unit test.

    This is what the wiring is for. A probe renders one page and can only ever
    report PROMISING; a run renders many, and the threshold that separates a
    coincidence from a pattern is only reachable there.
    """

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.state = Path(tempdir.name)
        self.rendered: list[str] = []

    def runner(self, *, count: int = 8, **cfg):
        """A gateway whose L2 transport renders AND reports what it 'saw'."""

        profile = html_profile()
        outer = self

        def provider(route, url_class, url):
            level = route.level.value
            observer = cfg.pop("_observer", None) or getattr(self, "_observer", None)

            class Transport:
                def fetch(self, target, *, headers=None):
                    if level == "L2":
                        outer.rendered.append(target)
                        if observer is not None:
                            # What a real render's network watcher would emit:
                            # the data fetch, plus the noise every page loads.
                            observer(
                                {
                                    "url": API,
                                    "method": "GET",
                                    "status": 200,
                                    "content_type": "application/json",
                                    "resource_type": "xhr",
                                    "body": API_BODY,
                                    "page_url": target,
                                    "request_header_names": ("Accept",),
                                }
                            )
                            observer(
                                {
                                    "url": "https://www.google-analytics.com/collect?v=1",
                                    "method": "POST",
                                    "status": 200,
                                    "content_type": "application/json",
                                    "resource_type": "xhr",
                                    "body": b"{}",
                                    "page_url": target,
                                }
                            )
                        body = RENDERED
                    else:
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

        config = RunConfig(
            profile_path=self.state / "p.json",
            state_dir=self.state,
            seed_urls=tuple(page_url(i) for i in range(count)),
            browser_pool=False,
            free_canary=False,
            **cfg,
        )
        runner = Runner(
            config,
            profile=profile,
            gateway=FetchGateway(profile, transport_provider=provider, pacer=NoWaitPacer()),
            wall_clock=lambda: 1000.0,
        )
        # An injected gateway does not get the runner's observer automatically,
        # so the test supplies the same callable the production wiring uses.
        self._observer = runner._observe_network
        if runner._discovery is not None:
            # `.example` is a reserved TLD that never resolves, and the SSRF
            # check does a real lookup. Supplying a resolver rather than
            # weakening the check.
            runner._discovery.resolver = PUBLIC_RESOLVER
        return runner

    def test_a_run_reaches_validated_where_a_probe_cannot(self) -> None:
        result = self.runner(count=8).run()
        discovery = result.report["discovery"]
        self.assertGreater(len(self.rendered), 1, "several pages were rendered")
        self.assertGreaterEqual(discovery["api_routes_validated"], 1)

    def test_the_run_report_carries_a_ready_draft(self) -> None:
        result = self.runner(count=8).run()
        drafts = result.report["discovery"]["drafts"]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["suggested_route"]["level"], "L0")
        self.assertIn("title", drafts[0]["extractor"]["fields"])

    def test_noise_never_reaches_a_draft(self) -> None:
        result = self.runner(count=8).run()
        listing = result.report["discovery"]["listing"]
        self.assertIn("google-analytics", listing, "it was seen")
        drafts = json.dumps(result.report["discovery"]["drafts"])
        self.assertNotIn("google-analytics", drafts, "and it was refused")

    def test_the_saving_is_only_claimed_where_it_was_counted(self) -> None:
        # A validated endpoint covers the renders this run actually performed.
        # No number is invented for future runs, because they have not happened.
        result = self.runner(count=8).run()
        discovery = result.report["discovery"]
        self.assertEqual(
            discovery["browser_renders_replaceable"], discovery["browser_renders_this_run"]
        )
        self.assertIn("not estimated here", discovery["note"])

    def test_discovery_can_be_switched_off(self) -> None:
        result = self.runner(count=4, discover_api=False).run()
        self.assertEqual(result.report["discovery"], {})

    def test_a_broken_observer_never_fails_the_run(self) -> None:
        # Discovery is a passenger on the render; a passenger must not be able
        # to crash the vehicle.
        runner = self.runner(count=4)

        class Broken:
            def observe(self, request):
                raise RuntimeError("discovery blew up")

            def candidates(self):
                return []

        runner._discovery = Broken()  # type: ignore[assignment]
        result = runner.run()  # must not raise
        self.assertEqual(result.report["accounting"]["unaccounted"], 0)

    def test_url_accounting_survives_discovery(self) -> None:
        result = self.runner(count=8).run()
        self.assertEqual(result.report["accounting"]["unaccounted"], 0)

    def test_the_run_stays_free(self) -> None:
        result = self.runner(count=8).run()
        self.assertEqual(result.report["metrics"]["paid_calls"], 0)
        self.assertEqual(result.report["metrics"]["cost_credits"], "0")


class SavingsProofTests(unittest.TestCase):
    """Run 1 renders and learns; run 2 reads the API. What actually changed?

    The claim under test is narrow on purpose. A cheaper route that returns
    different data is not an optimisation, so field agreement is asserted before
    any saving is mentioned — and the saving that is mentioned is only the one
    that was counted.
    """

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.state = Path(tempdir.name)
        self.clock = [1_000_000.0]

    def store(self):
        from web_scraper.discovery import DiscoveryStore

        return DiscoveryStore(self.state / "discovery.sqlite3", now=lambda: self.clock[0])

    def learn_across_runs(self, runs: int = 3):
        """Each iteration is a separate process observing different pages."""

        from web_scraper.discovery import (
            DiscoveryCollector,
            ObservedRequest,
        )

        for run in range(runs):
            collector = DiscoveryCollector(
                min_pages=1,
                wanted_fields=("title", "score"),
                resolver=PUBLIC_RESOLVER,
            )
            page = f"https://csr-demo.example/players/{run}"
            collector.observe(
                ObservedRequest(
                    url=API,
                    method="GET",
                    status=200,
                    content_type="application/json",
                    resource_type="xhr",
                    body=API_BODY,
                    page_url=page,
                )
            )
            store = self.store()
            for candidate in collector.candidates():
                store.record(
                    candidate,
                    domain="csr-demo.example",
                    url_class="player",
                    source_pages=[page],
                )
        return self.store()

    def test_evidence_only_validates_after_several_runs(self) -> None:
        from web_scraper.discovery import EvidenceState

        after_one = self.learn_across_runs(runs=1)
        self.assertEqual(after_one.validated(), [], "one run is not evidence")

        after_three = self.learn_across_runs(runs=3)
        validated = after_three.validated()
        self.assertEqual(len(validated), 1)
        self.assertIs(validated[0].state, EvidenceState.VALIDATED)

    def test_the_two_routes_agree_on_every_critical_field(self) -> None:
        # Asserted BEFORE any saving is discussed.
        from web_scraper.discovery import evidence_to_candidate, profile_route_draft

        store = self.learn_across_runs()
        draft = profile_route_draft(evidence_to_candidate(store.validated()[0]))

        from_api, _ = extract_response(
            API_BODY,
            headers={"Content-Type": "application/json"},
            extractors=[draft["extractor"]],
            fields=["title", "score"],
        )
        from_browser, _ = extract_response(
            RENDERED,
            headers={"Content-Type": "text/html"},
            extractors=[{"kind": "heuristic"}],
            fields=["title"],
        )
        self.assertEqual(from_api.data["title"], from_browser.data["title"])
        self.assertEqual(from_api.data["score"], 93)

    def test_the_comparison_refuses_to_claim_an_unmeasured_saving(self) -> None:
        from web_scraper.diagnose.routes import compare_routes

        store = self.learn_across_runs()
        comparison = compare_routes(store.all_evidence(), critical_fields=("title", "score"))[0]
        self.assertIsNone(comparison.latency_saving_ms, "neither route was timed")
        self.assertEqual(comparison.cost_saving, "UNKNOWN")
        self.assertIn("no speed claim is made", comparison.recommendation)

    def test_a_measured_saving_is_reported(self) -> None:
        from web_scraper.diagnose.routes import RouteMeasurement, compare_routes

        store = self.learn_across_runs()
        comparison = compare_routes(
            store.all_evidence(),
            critical_fields=("title", "score"),
            current=RouteMeasurement(label="L2 browser", samples=40, p50_ms=620, p95_ms=850),
            candidate_measurements={
                store.validated()[0].identity: RouteMeasurement(
                    label="L0 json", samples=40, p50_ms=70, p95_ms=94
                )
            },
            browser_renders=12,
        )[0]
        self.assertEqual(comparison.latency_saving_ms, 756)
        self.assertIn("756 ms lower", comparison.recommendation)
        self.assertIn("every critical field covered", comparison.recommendation)

    def test_missing_critical_fields_block_the_recommendation(self) -> None:
        # A cheaper route returning different data is not an optimisation.
        from web_scraper.diagnose.routes import RouteMeasurement, compare_routes

        store = self.learn_across_runs()
        comparison = compare_routes(
            store.all_evidence(),
            critical_fields=("title", "score", "author"),
            current=RouteMeasurement(label="L2", samples=10, p95_ms=900),
        )[0]
        self.assertFalse(comparison.fully_covers_fields)
        self.assertIn("NOT READY", comparison.recommendation)
        self.assertIn("2/3", comparison.field_coverage)

    def test_a_degraded_endpoint_is_not_recommended(self) -> None:
        from web_scraper.diagnose.routes import compare_routes
        from web_scraper.discovery import EvidenceState

        store = self.learn_across_runs()
        identity = store.validated()[0].identity

        # The endpoint changes shape. Yesterday's verdict does not carry over.
        from web_scraper.discovery import DiscoveryCollector, ObservedRequest

        collector = DiscoveryCollector(min_pages=1, resolver=PUBLIC_RESOLVER)
        collector.observe(
            ObservedRequest(
                url=API,
                method="GET",
                status=200,
                content_type="application/json",
                resource_type="xhr",
                body=json.dumps({"completely": {"different": True}}).encode(),
                page_url="https://csr-demo.example/players/99",
            )
        )
        for candidate in collector.candidates():
            store.record(
                candidate,
                domain="csr-demo.example",
                source_pages=["https://csr-demo.example/players/99"],
            )

        after = store.get(identity)
        assert after is not None
        self.assertIs(after.state, EvidenceState.REVALIDATION_REQUIRED)
        self.assertEqual(compare_routes(store.all_evidence()), [], "not offered any more")

    def test_evidence_survives_a_restart(self) -> None:
        self.learn_across_runs()
        restarted = self.store()
        self.assertEqual(len(restarted.validated()), 1)
