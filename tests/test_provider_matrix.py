"""One matrix, all five vendors, on the two questions that cost real money.

Three of the five adapters shipped with a defect in the first question and two
in the second. Every one of those defects was invisible to the vendor's
documentation, to the type checker and to the per-adapter tests — including, in
two cases, a test that asserted the same wrong thing the code assumed.

So this file asks the questions in one place, of every vendor, in the same
words:

**Status.** Is the site's answer kept separate from the vendor's? A dead URL
must arrive as a dead URL, and a vendor's own refusal must never be presented as
a verdict about the site. Getting this backwards either quarantines a live page
or re-bills a dead one on every run, forever.

**Billing.** Does an unreported cost stay unknown? A silent zero is how a
budget is exceeded while every individual check passes.

The scripted responses are shaped from live measurements — the ZenRows code
prefixes, Bright Data's errors-over-HTTP-200, Zyte's status field, Firecrawl's
cost in the body — so a vendor that changes its wire format breaks this file
rather than quietly changing what the numbers mean.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.providers.base import (
    Provider,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResponse,
)
from web_scraper.providers.bright_data import BrightDataProvider
from web_scraper.providers.firecrawl import FirecrawlProvider
from web_scraper.providers.pricing import PricingBook
from web_scraper.providers.scrape_do import ScrapeDoProvider
from web_scraper.providers.zenrows import ZenRowsProvider
from web_scraper.providers.zyte import ZyteProvider

URL = "https://example.com/a"
PAGE = b"<html><body><article>" + b"word " * 200 + b"</article></body></html>"
GONE = b"<html><body>not found</body></html>"


class FakeHTTP:
    """Returns one scripted HTTP answer, whatever is asked of it."""

    def __init__(self, status: int = 200, body: bytes = b"{}", headers: dict | None = None):
        self.status, self.body, self.headers = status, body, headers or {}

    def urlopen(self, request, timeout=None):  # type: ignore[no-untyped-def]
        outer = self

        class Response:
            status = outer.status
            headers = outer.headers

            def read(self, amount=None):  # type: ignore[no-untyped-def]
                return outer.body if amount is None else outer.body[:amount]

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_):  # type: ignore[no-untyped-def]
                return False

        return Response()


Script = tuple[int, bytes, dict]


def firecrawl_envelope(target_status: int, *, html: str = "ok", credits: Any = 1) -> bytes:
    metadata: dict[str, Any] = {"statusCode": target_status, "url": URL, "contentType": "text/html"}
    if credits is not None:
        metadata["creditsUsed"] = credits
    return json.dumps({"success": True, "data": {"rawHtml": html, "metadata": metadata}}).encode()


def zyte_envelope(target_status: int, *, body: bytes = PAGE) -> bytes:
    return json.dumps(
        {
            "url": URL,
            "statusCode": target_status,
            "httpResponseBody": base64.b64encode(body).decode(),
            "httpResponseHeaders": [{"name": "content-type", "value": "text/html"}],
        }
    ).encode()


@dataclass(frozen=True)
class VendorCase:
    """One vendor, and how to script each situation on its own wire."""

    name: str
    build: Callable[[FakeHTTP], Provider]
    strategy: str
    target_ok: Script
    target_404: Script
    auth: Script
    quota: Script
    bad_request: Script
    #: The cost the ok-script reports, in native units.
    reported_cost: Decimal
    #: The same call with the vendor saying nothing about cost.
    cost_absent: Script
    #: A refusal the vendor charges nothing for, where one is documented.
    refusal_without_charge: Script | None = None
    #: Strategies whose cost cannot be bounded without an operator figure.
    unpriced_without_operator: bool = False
    notes: str = ""


VENDORS: tuple[VendorCase, ...] = (
    VendorCase(
        name="scrape.do",
        build=lambda http: ScrapeDoProvider(token="t", opener=http),
        strategy="normal",
        target_ok=(
            200,
            PAGE,
            {"scrape.do-initial-status-code": "200", "scrape.do-request-cost": "1"},
        ),
        # MEASURED: a dead URL costs a credit and arrives with the target's own
        # status in a dedicated header.
        target_404=(
            404,
            GONE,
            {"scrape.do-initial-status-code": "404", "scrape.do-request-cost": "1"},
        ),
        auth=(401, b"", {}),
        quota=(429, b"", {}),
        bad_request=(400, b"", {}),
        reported_cost=Decimal("1"),
        cost_absent=(200, PAGE, {"scrape.do-initial-status-code": "200"}),
    ),
    VendorCase(
        name="firecrawl",
        build=lambda http: FirecrawlProvider(api_key="k", opener=http),
        strategy="basic",
        # MEASURED: the cost is in the BODY, not a header.
        target_ok=(200, firecrawl_envelope(200), {}),
        target_404=(200, firecrawl_envelope(404), {}),
        auth=(401, b"{}", {}),
        quota=(402, b"{}", {}),
        bad_request=(400, b"{}", {}),
        reported_cost=Decimal("1"),
        cost_absent=(200, firecrawl_envelope(200, credits=None), {}),
    ),
    VendorCase(
        name="brightdata",
        build=lambda http: BrightDataProvider(api_key="k", zone="z", opener=http),
        strategy="unlocker",
        # MEASURED: the envelope is ALWAYS 200; the site's status is a header.
        target_ok=(200, PAGE, {"x-brd-status-code": "200"}),
        target_404=(200, GONE, {"x-brd-status-code": "404"}),
        # MEASURED: Bright Data reports its own failures with HTTP 200.
        auth=(200, b"", {"x-brd-err-code": "client_10002", "x-brd-err-msg": "zone not found"}),
        quota=(200, b"", {"x-brd-err-code": "client_20001", "x-brd-err-msg": "out of quota"}),
        bad_request=(400, b"", {}),
        reported_cost=Decimal("0"),
        # MEASURED: no cost figure of any kind, ever.
        cost_absent=(200, PAGE, {"x-brd-status-code": "200"}),
        notes="reports no cost at all; needs an operator CPM",
    ),
    VendorCase(
        name="zenrows",
        build=lambda http: ZenRowsProvider(api_key="k", opener=http),
        strategy="basic",
        target_ok=(200, PAGE, {"x-request-credits": "1", "x-request-cost": "0.001"}),
        # MEASURED: a target 404 arrives as problem+json with a RESP* code and
        # IS billed. Reading it as a provider error would leave the URL
        # unquarantined and re-billed every run.
        target_404=(
            404,
            json.dumps({"code": "RESP002", "title": "Not Found"}).encode(),
            {
                "content-type": "application/problem+json",
                "x-request-credits": "1",
                "x-request-cost": "0.001",
            },
        ),
        auth=(
            401,
            json.dumps({"code": "AUTH001", "title": "Invalid key"}).encode(),
            {"content-type": "application/problem+json"},
        ),
        quota=(
            429,
            json.dumps({"code": "REQS004", "title": "Rate limited"}).encode(),
            {"content-type": "application/problem+json"},
        ),
        # MEASURED: REQS001 is ZenRows refusing OUR request, and it costs zero.
        bad_request=(
            400,
            json.dumps({"code": "REQS001", "title": "domain forbidden"}).encode(),
            {"content-type": "application/problem+json"},
        ),
        reported_cost=Decimal("1"),
        cost_absent=(200, PAGE, {}),
        refusal_without_charge=(
            400,
            json.dumps({"code": "REQS001", "title": "domain forbidden"}).encode(),
            {"content-type": "application/problem+json"},
        ),
        unpriced_without_operator=True,
    ),
    VendorCase(
        name="zyte",
        build=lambda http: ZyteProvider(api_key="k", opener=http),
        strategy="http",
        # MEASURED: the site's status is a field inside a 200 envelope.
        target_ok=(200, zyte_envelope(200), {}),
        target_404=(200, zyte_envelope(404, body=GONE), {}),
        auth=(401, json.dumps({"type": "/auth/key-not-found"}).encode(), {}),
        quota=(429, json.dumps({"type": "/limits/over-user-limit"}).encode(), {}),
        bad_request=(400, json.dumps({"type": "/request/unprocessable"}).encode(), {}),
        reported_cost=Decimal("0"),
        # MEASURED: no cost field anywhere in the response.
        cost_absent=(200, zyte_envelope(200), {}),
        unpriced_without_operator=True,
        notes="reports no cost at all; needs operator ceilings",
    ),
)


def fetch(case: VendorCase, script: Script) -> ProviderResponse:
    status, body, headers = script
    provider = case.build(FakeHTTP(status=status, body=body, headers=headers))
    return provider.fetch(ProviderRequest(url=URL, strategy_id=case.strategy))


class TargetStatusMatrix(unittest.TestCase):
    """The site's answer and the vendor's answer, kept apart, for all five."""

    def test_a_healthy_page_reports_the_targets_own_success(self) -> None:
        for case in VENDORS:
            with self.subTest(case.name):
                response = fetch(case, case.target_ok)
                self.assertEqual(response.target_status, 200)
                self.assertTrue(response.provider_ok)

    def test_a_dead_url_arrives_as_a_dead_url_and_not_as_success(self) -> None:
        """The defect that hit Bright Data, Firecrawl and ZenRows."""

        for case in VENDORS:
            with self.subTest(case.name):
                response = fetch(case, case.target_404)
                self.assertEqual(
                    response.target_status,
                    404,
                    f"{case.name} lost the target's 404 — it would be re-billed every run",
                )

    def test_a_dead_url_is_never_reported_as_a_provider_failure(self) -> None:
        """The mirror image, and the one that leaves a URL unquarantined."""

        for case in VENDORS:
            with self.subTest(case.name):
                response = fetch(case, case.target_404)
                self.assertTrue(
                    response.provider_ok,
                    f"{case.name} blamed itself for the site's 404",
                )

    def test_the_vendors_own_refusals_never_become_a_verdict_about_the_site(self) -> None:
        expectations = (
            ("auth", ProviderErrorKind.AUTH),
            ("quota", ProviderErrorKind.QUOTA),
            ("bad_request", ProviderErrorKind.BAD_REQUEST),
        )
        for case in VENDORS:
            for attribute, kind in expectations:
                with self.subTest(f"{case.name}:{attribute}"):
                    with self.assertRaises(ProviderError) as caught:
                        fetch(case, getattr(case, attribute))
                    self.assertEqual(
                        caught.exception.kind,
                        kind,
                        f"{case.name} mapped its own {attribute} to {caught.exception.kind.value}",
                    )

    def test_every_vendor_separates_the_two_statuses_structurally(self) -> None:
        """Not an accident of one code path: the contract has two fields."""

        for case in VENDORS:
            with self.subTest(case.name):
                response = fetch(case, case.target_404)
                self.assertNotEqual(
                    (response.target_status, response.provider_status),
                    (response.provider_status, response.provider_status),
                    f"{case.name} put the same number in both fields",
                )


class BillingMatrix(unittest.TestCase):
    """An unknown cost is never a zero, in any vendor, on any path."""

    def test_a_reported_cost_is_carried_through_as_attributed(self) -> None:
        for case in VENDORS:
            if case.reported_cost == 0:
                continue
            with self.subTest(case.name):
                response = fetch(case, case.target_ok)
                self.assertTrue(response.cost.attributed)
                self.assertEqual(response.cost.credits, case.reported_cost)

    def test_an_unreported_cost_is_unattributed_rather_than_zero(self) -> None:
        for case in VENDORS:
            with self.subTest(case.name):
                response = fetch(case, case.cost_absent)
                self.assertFalse(
                    response.cost.attributed,
                    f"{case.name} reported nothing and it was recorded as free",
                )

    def test_an_unattributed_cost_settles_as_unknown_when_no_tariff_bounds_it(self) -> None:
        book = PricingBook()
        for case in VENDORS:
            if not case.unpriced_without_operator:
                continue
            with self.subTest(case.name):
                cost = book.settle(case.name, case.strategy, None)
                self.assertFalse(cost.is_known)
                self.assertIsNone(cost.credits)

    def test_a_vendor_that_states_its_own_dollars_settles_exactly(self) -> None:
        """ZenRows reports USD directly, which beats any rate we could apply."""

        response = fetch(VENDORS[3], VENDORS[3].target_ok)
        self.assertEqual(response.cost.usd, Decimal("0.001"))
        cost = PricingBook().settle(
            "zenrows", "basic", response.cost.credits, reported_usd=response.cost.usd
        )
        self.assertEqual(cost.estimated_usd, Decimal("0.001"))
        self.assertTrue(cost.is_known)

    def test_a_refusal_the_vendor_does_not_charge_for_still_raises(self) -> None:
        """Zero credits is not permission to treat it as a fetched page."""

        for case in VENDORS:
            if case.refusal_without_charge is None:
                continue
            with self.subTest(case.name), self.assertRaises(ProviderError):
                fetch(case, case.refusal_without_charge)

    def test_an_unpriced_strategy_is_never_the_cheapest_thing_in_the_fleet(self) -> None:
        """A zero here would out-rank every strategy whose price we know."""

        book = PricingBook()
        for case in VENDORS:
            if not case.unpriced_without_operator:
                continue
            with self.subTest(case.name):
                self.assertIsNone(book.expected_usd(case.name, case.strategy))
                self.assertIsNone(book.upper_bound_usd(case.name, case.strategy))


if __name__ == "__main__":
    unittest.main()
