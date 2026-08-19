"""Bright Data adapter.

Verified against the live documentation on 2026-08-19 (see
``docs/providers/bright-data.md``). Not written from memory.

Bright Data is the **high-reliability fallback**, not the default paid provider.
It is the most expensive door in the building, and the router should only reach
it when cheaper compatible strategies have failed to meet their confidence bound
or have demonstrably stopped working. Making it the first choice would spend the
most money on the URLs that a one-credit call would have resolved.

Two documented facts shape this adapter:

**Billing is per successful request — unless we send custom headers.** The
documentation is explicit that supplying manual headers, cookies or expect
elements moves the account onto being charged for *all* requests, successful or
not, because Bright Data no longer controls the outcome. This adapter therefore
does not forward caller headers. It is not an oversight: forwarding them would
silently convert every failed attempt into a billable one.

**Web Unlocker and Browser API are different products.** They are modelled as
separate strategies with different powers and different holds, so the router can
prefer the cheaper one. Rendering is only requested when a verdict actually calls
for it — paying browser prices to be refused by a WAF is pure loss.

Cost is reported in CPM (per 1000 successful requests) on the account, and the
per-request response carries no credit figure. Cost is therefore *unattributed*
here unless a debug header supplies one, which is the honest answer rather than
a zero.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from web_scraper.providers._transport import Opener, post_json
from web_scraper.providers.base import (
    ProviderCost,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResponse,
    ProviderStrategy,
)

API_ENDPOINT = "https://api.brightdata.com/request"
PROVIDER_NAME = "brightdata"

#: Date the live documentation was last read end to end.
DOCS_VERIFIED_AT = "2026-08-19"

#: Bright Data bills CPM — per 1000 successful requests — so a single call has
#: no published credit price expressible on the same scale as Scrape.do credits.
#: These are *planning weights* on this project's own scale, not a claim about
#: the vendor's tariff.
#:
#: They are set deliberately above every strategy of every cheaper provider, so
#: that a cost-ranking router reaches Bright Data only once the cheaper doors
#: have failed their confidence bound. An earlier draft put the unlocker at 12,
#: below Scrape.do's super_render at 15, which would have made the "fallback"
#: the first choice for any verdict both could serve. The test
#: ``test_it_is_priced_above_the_cheaper_providers`` exists to keep that from
#: silently coming back.
UNLOCKER_PLANNING_COST = Decimal("20")
BROWSER_PLANNING_COST = Decimal("40")

UNLOCKER = ProviderStrategy(
    id="unlocker",
    nominal_cost=UNLOCKER_PLANNING_COST,
    # Premium domains are documented to bill higher without a published
    # multiplier, so the hold is deliberately generous.
    reservation_cost=Decimal("40"),
    renders_javascript=False,
    premium_network=True,
    description="Web Unlocker; managed anti-bot network, no browser",
)

UNLOCKER_RENDER = ProviderStrategy(
    id="unlocker_render",
    nominal_cost=UNLOCKER_PLANNING_COST,
    reservation_cost=Decimal("45"),
    renders_javascript=True,
    premium_network=True,
    description="Web Unlocker with render=true; JavaScript executed on their side",
)

BROWSER = ProviderStrategy(
    id="browser",
    nominal_cost=BROWSER_PLANNING_COST,
    reservation_cost=Decimal("80"),
    renders_javascript=True,
    premium_network=True,
    description="Browser API; full remote browser, the last resort",
)

STRATEGIES: tuple[ProviderStrategy, ...] = (UNLOCKER, UNLOCKER_RENDER, BROWSER)

_RENDERING_STRATEGIES = frozenset({UNLOCKER_RENDER.id, BROWSER.id})


class BrightDataProvider:
    """Bright Data behind the vendor-neutral contract."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str | None = None,
        token_env: str = "BRIGHTDATA_API_KEY",  # noqa: S107 - a variable NAME, not a secret
        zone: str | None = None,
        zone_env: str = "BRIGHTDATA_ZONE",
        browser_zone: str | None = None,
        browser_zone_env: str = "BRIGHTDATA_BROWSER_ZONE",
        endpoint: str = API_ENDPOINT,
        opener: Opener | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get(token_env, "")
        self._zone = zone or os.environ.get(zone_env, "")
        # The Browser API is a different zone; falling back to the unlocker zone
        # would send browser traffic to a product that cannot serve it.
        self._browser_zone = browser_zone or os.environ.get(browser_zone_env, "")
        self._endpoint = endpoint
        self._opener = opener

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._zone)

    def strategies(self) -> tuple[ProviderStrategy, ...]:
        """Only the strategies this installation can actually run.

        The Browser API needs its own zone. Advertising it without one would let
        the router pick a strategy whose every call fails on configuration.
        """

        if self._browser_zone:
            return STRATEGIES
        return (UNLOCKER, UNLOCKER_RENDER)

    def build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        """The request body, exposed so tests can assert it without a network."""

        known = {s.id for s in STRATEGIES}
        if request.strategy_id not in known:
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message=f"unknown strategy {request.strategy_id!r}",
                provider=self.name,
            )
        if request.strategy_id == BROWSER.id and not self._browser_zone:
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message="the Browser API needs BRIGHTDATA_BROWSER_ZONE",
                provider=self.name,
            )

        zone = self._browser_zone if request.strategy_id == BROWSER.id else self._zone
        payload: dict[str, Any] = {
            "zone": zone,
            "url": request.url,
            # "raw" gives us the document. "json" would wrap it in a structure we
            # would only have to unwrap before triage anyway.
            "format": "raw",
        }
        if request.strategy_id in _RENDERING_STRATEGIES:
            payload["render"] = "true"
        if request.geo_code:
            payload["country"] = request.geo_code.lower()
        # Deliberately absent: custom headers and cookies. Sending them moves
        # the account onto all-requests billing, so failures would start costing
        # money too. See the module docstring.
        return payload

    def fetch(self, request: ProviderRequest) -> ProviderResponse:
        if not self._api_key:
            raise ProviderError(
                kind=ProviderErrorKind.AUTH,
                message="no API key: set BRIGHTDATA_API_KEY in the environment",
                provider=self.name,
            )
        if not self._zone:
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message="no zone: set BRIGHTDATA_ZONE in the environment",
                provider=self.name,
            )

        result = post_json(
            self._endpoint,
            self.build_payload(request),
            headers={"Authorization": f"Bearer {self._api_key}"},
            provider=self.name,
            timeout_seconds=request.timeout_seconds,
            opener=self._opener,
        )
        self._raise_for_provider_failure(result.status, result.body)

        # format=raw returns the target document itself, so the envelope status
        # is the vendor's and the target status must come from their headers.
        target_status = _as_int(result.headers.get("x-brd-http-status"))
        return ProviderResponse(
            provider=self.name,
            strategy_id=request.strategy_id,
            # When the vendor does not state the target status, a successful
            # unlock means the site answered; we report its own 200 rather than
            # inventing one, and triage judges the body either way.
            target_status=target_status if target_status is not None else result.status,
            provider_status=result.status,
            body=result.body,
            headers={
                k: v for k, v in result.headers.items() if k in {"content-type", "content-language"}
            },
            final_url=result.headers.get("x-brd-resolved-url") or request.url,
            latency_ms=result.latency_ms,
            cost=_cost_from(result.headers),
            request_id=result.headers.get("x-brd-request-id"),
            # Web Unlocker fetches live; there is no documented content cache.
            from_cache=False,
            content_age_seconds=None,
            detected_defense=result.headers.get("x-brd-detected-protection"),
        )

    def _raise_for_provider_failure(self, status: int, body: bytes) -> None:
        if status == 401:
            raise ProviderError(
                kind=ProviderErrorKind.AUTH,
                message="provider rejected the credentials (HTTP 401)",
                provider=self.name,
                status=status,
            )
        if status == 402:
            raise ProviderError(
                kind=ProviderErrorKind.QUOTA,
                message="payment required: account balance exhausted",
                provider=self.name,
                status=status,
            )
        if status == 429:
            raise ProviderError(
                kind=ProviderErrorKind.QUOTA,
                message="provider rate limit exceeded",
                provider=self.name,
                status=status,
                retryable=True,
            )
        if status == 400:
            # Their 400 is about OUR request — a bad zone, a malformed body.
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message=f"provider rejected the request: {body[:200]!r}",
                provider=self.name,
                status=status,
            )
        if status >= 500:
            raise ProviderError(
                kind=ProviderErrorKind.PROVIDER_FAULT,
                message=f"provider error (HTTP {status})",
                provider=self.name,
                status=status,
                retryable=True,
            )


def _cost_from(headers: dict[str, str]) -> ProviderCost:
    """CPM billing means a single call carries no price. Say so."""

    for name in ("x-brd-cost", "x-brd-credits-used"):
        if name in headers:
            return ProviderCost.parse(headers[name])
    return ProviderCost.unattributed()


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
