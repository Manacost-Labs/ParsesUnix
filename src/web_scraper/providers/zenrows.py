"""ZenRows adapter.

Verified against the live documentation on 2026-08-19
(``docs.zenrows.com/universal-scraper-api/api-reference``). Written from what
that page says, not from memory — the previous provider round cost five real
defects to the opposite habit.

Two things about this API shape the adapter more than anything else.

**The API key travels in the query string.** ``?apikey=...`` is how ZenRows
authenticates, which means the request URL *is* a secret. It must never reach a
log line, an exception message, a snapshot or a report. Every error raised here
carries the target URL and the strategy, never the URL we actually called.

**With ``original_status=true`` the envelope status IS the target status.** That
is what we want — triage needs the site's real answer — but it removes the
separation the other adapters rely on: a 401 could be the site refusing the
visit or ZenRows refusing our key. The distinguishing signal is
``X-Request-Id``: ZenRows sets it on requests it actually processed, so a 4xx
*with* that header is the site talking and a 4xx *without* it is ZenRows.
Getting this backwards would record a billing failure as a dead URL, which is
precisely the confusion that cost us on Bright Data.
"""

from __future__ import annotations

import os
import urllib.parse
from decimal import Decimal
from typing import Any

from web_scraper.providers._transport import DEFAULT_MAX_BODY_BYTES, Opener, get
from web_scraper.providers.base import (
    ProviderCost,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResponse,
    ProviderStrategy,
)

API_ENDPOINT = "https://api.zenrows.com/v1/"
PROVIDER_NAME = "zenrows"

#: Date the live documentation was read end to end.
DOCS_VERIFIED_AT = "2026-08-19"
DOCS_SOURCE = "https://docs.zenrows.com/universal-scraper-api/api-reference"

#: Response headers, exactly as documented.
HEADER_COST = "x-request-cost"
HEADER_REQUEST_ID = "x-request-id"
HEADER_FINAL_URL = "zr-final-url"
HEADER_CONCURRENCY_LIMIT = "concurrency-limit"
HEADER_CONCURRENCY_REMAINING = "concurrency-remaining"

#: Documented credit multipliers. Used for PLANNING only — a reported cost
#: always wins, because a multiplier is what we expect and the header is what
#: happened.
MULTIPLIER_BASIC = Decimal("1")
MULTIPLIER_JS = Decimal("5")
MULTIPLIER_PREMIUM = Decimal("10")
MULTIPLIER_JS_PREMIUM = Decimal("25")

BASIC = ProviderStrategy(
    id="basic",
    nominal_cost=MULTIPLIER_BASIC,
    reservation_cost=Decimal("2"),
    renders_javascript=False,
    premium_network=False,
    description="plain fetch through their pool; 1 credit",
)

JS = ProviderStrategy(
    id="js",
    nominal_cost=MULTIPLIER_JS,
    reservation_cost=Decimal("7"),
    renders_javascript=True,
    premium_network=False,
    description="js_render=true; 5 credits",
)

PREMIUM = ProviderStrategy(
    id="premium",
    nominal_cost=MULTIPLIER_PREMIUM,
    reservation_cost=Decimal("13"),
    renders_javascript=False,
    premium_network=True,
    description="premium_proxy=true, residential IPs; 10 credits",
)

JS_PREMIUM = ProviderStrategy(
    id="js_premium",
    nominal_cost=MULTIPLIER_JS_PREMIUM,
    reservation_cost=Decimal("30"),
    renders_javascript=True,
    premium_network=True,
    description="both; 25 credits — the most expensive thing this vendor sells",
)

AUTO = ProviderStrategy(
    id="auto",
    # Nominal is the cheapest mode it MIGHT pick; the hold is the dearest.
    # Charging the plan the optimistic figure and reserving the pessimistic one
    # is the only pair that is safe in both directions.
    nominal_cost=MULTIPLIER_BASIC,
    reservation_cost=Decimal("30"),
    renders_javascript=True,
    premium_network=True,
    description="mode=auto; the vendor chooses, so the hold covers the worst case",
)

STRATEGIES: tuple[ProviderStrategy, ...] = (BASIC, JS, PREMIUM, JS_PREMIUM, AUTO)

#: Per-strategy request parameters, from the documented parameter table.
_PARAMS: dict[str, dict[str, str]] = {
    BASIC.id: {},
    JS.id: {"js_render": "true"},
    PREMIUM.id: {"premium_proxy": "true"},
    JS_PREMIUM.id: {"js_render": "true", "premium_proxy": "true"},
    AUTO.id: {"mode": "auto"},
}

#: Strategies that run a browser, and can therefore report network traffic.
RENDERING_STRATEGIES = frozenset({JS.id, JS_PREMIUM.id, AUTO.id})


class ZenRowsProvider:
    """ZenRows behind the vendor-neutral contract."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str | None = None,
        token_env: str = "ZENROWS_API_KEY",  # noqa: S107 - a variable NAME
        endpoint: str = API_ENDPOINT,
        capture_network: bool = False,
        opener: Opener | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        # Env-only, like every credential in this project: a key in a profile
        # or a config file ends up in a snapshot or a commit.
        self._api_key = api_key or os.environ.get(token_env, "")
        self._endpoint = endpoint
        # json_response asks the vendor to return the page's XHR/fetch traffic.
        # Off by default: it changes the response shape, and discovery should be
        # something a caller opts into rather than a surprise.
        self._capture_network = capture_network
        self._opener = opener
        self._max_body_bytes = max_body_bytes

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def strategies(self) -> tuple[ProviderStrategy, ...]:
        return STRATEGIES

    # -- request -----------------------------------------------------------

    def build_query(self, request: ProviderRequest) -> dict[str, str]:
        """The query parameters, exposed so tests can assert them without a call.

        Deliberately returns the parameters rather than the URL: the URL carries
        the API key, and a helper that returns it invites logging it.
        """

        params = _PARAMS.get(request.strategy_id)
        if params is None:
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message=f"unknown strategy {request.strategy_id!r}",
                provider=self.name,
            )

        query: dict[str, str] = {
            "url": request.url,
            # Without this the API answers 200 for everything and triage would
            # judge a 404 page as thin content instead of a dead URL.
            "original_status": "true",
            **params,
        }
        if request.wait_selector and request.strategy_id in RENDERING_STRATEGIES:
            query["wait_for"] = request.wait_selector
        if request.geo_code and request.strategy_id in {PREMIUM.id, JS_PREMIUM.id, AUTO.id}:
            # Documented as requiring premium_proxy, so it is only sent where
            # that is actually on.
            query["proxy_country"] = request.geo_code.lower()
        if request.session_id is not None:
            query["session_id"] = str(request.session_id)
        if self._capture_network and request.strategy_id in RENDERING_STRATEGIES:
            query["json_response"] = "true"
        return query

    def fetch(self, request: ProviderRequest) -> ProviderResponse:
        """The Provider contract. Any captured traffic is discarded here.

        A caller that wants discovery uses :meth:`fetch_with_capture`, which
        returns both. Stashing the capture on the instance would be stateful and
        unsafe across threads, and putting it on ProviderResponse would push a
        vendor's network dump into the vendor-neutral contract.
        """

        response, _ = self.fetch_with_capture(request)
        return response

    def fetch_with_capture(
        self, request: ProviderRequest
    ) -> tuple[ProviderResponse, list[dict[str, Any]]]:
        """Fetch, and return whatever network traffic the vendor reported."""

        if not self.configured:
            raise ProviderError(
                kind=ProviderErrorKind.AUTH,
                message="no API key: set ZENROWS_API_KEY in the environment",
                provider=self.name,
            )

        query = self.build_query(request)
        # The key is added here and nowhere else, so no other code path holds a
        # URL that must not be printed.
        url = self._endpoint + "?" + urllib.parse.urlencode({**query, "apikey": self._api_key})
        result = get(
            url,
            provider=self.name,
            timeout_seconds=request.timeout_seconds,
            opener=self._opener,
            max_body_bytes=self._max_body_bytes,
        )

        request_id = result.headers.get(HEADER_REQUEST_ID)
        self._raise_for_provider_failure(result.status, result.headers, request_id)

        body = result.body
        captured: list[dict[str, Any]] = []
        if query.get("json_response") == "true":
            body, captured = _split_json_response(body, provider=self.name)

        return ProviderResponse(
            provider=self.name,
            strategy_id=request.strategy_id,
            # With original_status=true the envelope status IS the target's.
            target_status=result.status,
            # ZenRows itself succeeded: it processed the request and billed for
            # it. Reporting the target's status here too would make a site 404
            # look like a provider fault.
            provider_status=200,
            body=body,
            headers=_content_headers(result.headers),
            final_url=result.headers.get(HEADER_FINAL_URL) or request.url,
            latency_ms=result.latency_ms,
            cost=_cost_from(result.headers),
            request_id=request_id,
            truncated=result.truncated,
            from_cache=False,
            content_age_seconds=None,
        ), captured

    def _raise_for_provider_failure(
        self, status: int, headers: dict[str, str], request_id: str | None
    ) -> None:
        """Separate ZenRows' own failures from the target's.

        With ``original_status=true`` both arrive as the envelope status, so the
        header is the only discriminator: ZenRows sets ``X-Request-Id`` on
        requests it processed. A 4xx carrying one is the site answering; a 4xx
        without one is ZenRows refusing us.

        Documented status semantics are not published for this case, so the rule
        is stated here as the assumption it is, and the live acceptance test
        exercises both sides of it.
        """

        if request_id:
            return  # ZenRows processed it; whatever the status is, it is the site's

        if status in {401, 403}:
            raise ProviderError(
                kind=ProviderErrorKind.AUTH,
                message=f"provider rejected the credentials (HTTP {status})",
                provider=self.name,
                status=status,
            )
        if status == 402:
            raise ProviderError(
                kind=ProviderErrorKind.QUOTA,
                message="payment required: plan credits exhausted",
                provider=self.name,
                status=status,
            )
        if status == 429:
            raise ProviderError(
                kind=ProviderErrorKind.QUOTA,
                message=(
                    "provider rate limit or concurrency ceiling reached "
                    f"(limit {headers.get(HEADER_CONCURRENCY_LIMIT, '?')})"
                ),
                provider=self.name,
                status=status,
                retryable=True,
            )
        if status == 400:
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message="provider rejected the request parameters",
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

    # -- discovery ---------------------------------------------------------

    def observed_requests(
        self, response: ProviderResponse, captured: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Translate captured network traffic into the shape discovery expects.

        Deliberately returns plain mappings for
        :func:`~web_scraper.discovery.observed_from_mapping`, so this module
        stays unaware of the discovery layer and every existing protection —
        auth rejection, SSRF validation, noise filtering — applies unchanged.
        """

        out: list[dict[str, Any]] = []
        for entry in captured:
            url = str(entry.get("url") or "")
            if not url:
                continue
            body = entry.get("body")
            if isinstance(body, str):
                body_bytes = body.encode("utf-8", errors="replace")
            elif isinstance(body, (bytes, bytearray)):
                body_bytes = bytes(body)
            else:
                body_bytes = b""
            headers = entry.get("headers") or {}
            out.append(
                {
                    "url": url,
                    "method": str(entry.get("method") or "GET"),
                    "status": int(entry.get("status") or 200),
                    "content_type": str(
                        headers.get("content-type") or headers.get("Content-Type") or ""
                    ),
                    "resource_type": "xhr",
                    # Names only. Values are never carried out of this function,
                    # so a captured Authorization cannot reach a candidate.
                    "request_header_names": tuple(headers),
                    "body": body_bytes,
                    "page_url": response.final_url or "",
                }
            )
        return out


def _split_json_response(body: bytes, *, provider: str) -> tuple[bytes, list[dict[str, Any]]]:
    """With ``json_response=true`` the body is an envelope, not the page.

    Returns the page HTML and the captured XHR entries separately, so callers
    that do not care about discovery see exactly what they would have seen
    without it.
    """

    import json

    try:
        envelope = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProviderError(
            kind=ProviderErrorKind.MALFORMED_RESPONSE,
            message=f"json_response body was not JSON: {exc}",
            provider=provider,
        ) from exc

    if not isinstance(envelope, dict):
        return body, []
    html = envelope.get("html") or envelope.get("content") or ""
    entries = envelope.get("xhr") or envelope.get("requests") or []
    return (
        str(html).encode("utf-8"),
        [entry for entry in entries if isinstance(entry, dict)],
    )


def _cost_from(headers: dict[str, str]) -> ProviderCost:
    """Documented header, used as reported. Absent means unknown, never zero."""

    reported = headers.get(HEADER_COST)
    if reported is None:
        return ProviderCost.unattributed()
    return ProviderCost.parse(reported)


def _content_headers(headers: dict[str, str]) -> dict[str, str]:
    """Only what triage reads. The rest carries vendor bookkeeping."""

    keep = {}
    for name in ("content-type", "content-language"):
        if name in headers:
            keep[name.title()] = headers[name]
    return keep
