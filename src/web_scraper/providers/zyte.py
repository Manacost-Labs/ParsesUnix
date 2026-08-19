"""Zyte API adapter.

Verified against the live documentation on 2026-08-19
(``docs.zyte.com/zyte-api/usage/{reference,http,errors}.html``).

Zyte separates provider status from target status cleanly, which is worth
saying out loud after two vendors that did not: **its own HTTP status is about
Zyte**, and the site's status arrives in a ``statusCode`` field inside a 200
response. A 404 from the site is a 200 from Zyte carrying ``statusCode: 404``;
a 401 from Zyte is Zyte refusing our key. No heuristic is needed to tell them
apart, so none is used.

Two decisions shape the rest.

**Vendor extraction is never truth.** Zyte can return parsed products, articles
and more. This adapter asks for the raw or rendered body and nothing else: the
document goes through canonical triage and this project's own extraction, so a
vendor changing its parser cannot silently change our dataset.

**Pricing is not a constant.** Zyte's price depends on the website tier, the
features requested and the mode, and none of that is knowable from a request.
Treating the cheapest published figure as a bound would understate the bill on
exactly the hard domains where the paid layer gets used. So a call settles
EXACT when the response reports a cost, PROVISIONAL against a ceiling the
operator supplies from their own account, and UNKNOWN otherwise — which stops
paid work, correctly.
"""

from __future__ import annotations

import base64
import binascii
import os
from decimal import Decimal
from typing import Any

from web_scraper.providers._transport import DEFAULT_MAX_BODY_BYTES, Opener, post_json
from web_scraper.providers.base import (
    ProviderCost,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResponse,
    ProviderStrategy,
)

API_ENDPOINT = "https://api.zyte.com/v1/extract"
PROVIDER_NAME = "zyte"

DOCS_VERIFIED_AT = "2026-08-19"
DOCS_SOURCE = "https://docs.zyte.com/zyte-api/usage/reference.html"

#: Target status lives here, inside a 200 envelope.
FIELD_TARGET_STATUS = "statusCode"
FIELD_HTTP_BODY = "httpResponseBody"
FIELD_HTTP_HEADERS = "httpResponseHeaders"
FIELD_BROWSER_HTML = "browserHtml"
FIELD_NETWORK_CAPTURE = "networkCapture"

#: Zyte's own error envelope: {"status": N, "type": "/auth/key-not-found"}.
_AUTH_TYPES = ("/auth/",)
_LIMIT_TYPES = ("/limits/",)
_REQUEST_TYPES = ("/request/",)

#: Planning weights on this project's own scale — NOT a claim about Zyte's
#: tariff, which varies by website tier. They exist so the router ranks Zyte
#: sensibly before any history exists; real money comes from the pricing book.
HTTP_PLANNING_COST = Decimal("2")
BROWSER_PLANNING_COST = Decimal("8")
CAPTURE_PLANNING_COST = Decimal("10")

HTTP = ProviderStrategy(
    id="http",
    nominal_cost=HTTP_PLANNING_COST,
    reservation_cost=Decimal("6"),
    renders_javascript=False,
    premium_network=True,
    description="httpResponseBody; their network, no browser",
)

BROWSER = ProviderStrategy(
    id="browser",
    nominal_cost=BROWSER_PLANNING_COST,
    reservation_cost=Decimal("20"),
    renders_javascript=True,
    premium_network=True,
    description="browserHtml; full render on their side",
)

BROWSER_CAPTURE = ProviderStrategy(
    id="browser_capture",
    nominal_cost=CAPTURE_PLANNING_COST,
    reservation_cost=Decimal("25"),
    renders_javascript=True,
    premium_network=True,
    description="browserHtml + networkCapture; a LEARNING strategy, not a fallback",
)

STRATEGIES: tuple[ProviderStrategy, ...] = (HTTP, BROWSER, BROWSER_CAPTURE)

#: Default capture filter. Broad enough to see a site's own data endpoints,
#: narrow enough that a page of images does not fill the response.
DEFAULT_CAPTURE_FILTERS: tuple[dict[str, Any], ...] = (
    {"filterType": "url", "matchType": "contains", "value": "/api", "httpResponseBody": True},
    {"filterType": "url", "matchType": "contains", "value": "/graphql", "httpResponseBody": True},
    {"filterType": "url", "matchType": "contains", "value": ".json", "httpResponseBody": True},
)


class ZyteProvider:
    """Zyte API behind the vendor-neutral contract."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str | None = None,
        token_env: str = "ZYTE_API_KEY",  # noqa: S107 - a variable NAME
        endpoint: str = API_ENDPOINT,
        capture_filters: tuple[dict[str, Any], ...] = DEFAULT_CAPTURE_FILTERS,
        run_id: str = "",
        opener: Opener | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self._api_key = api_key or os.environ.get(token_env, "")
        self._endpoint = endpoint
        self._capture_filters = capture_filters
        # Tags are echoed back on Zyte's stats API, which is how spend can be
        # reconciled later. Only the run id and strategy go in: a tag carrying a
        # URL would put target data in someone else's billing system.
        self._run_id = run_id
        self._opener = opener
        self._max_body_bytes = max_body_bytes

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def strategies(self) -> tuple[ProviderStrategy, ...]:
        return STRATEGIES

    # -- request -----------------------------------------------------------

    def build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        """The request body, exposed so tests can assert it without a network."""

        known = {s.id for s in STRATEGIES}
        if request.strategy_id not in known:
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message=f"unknown strategy {request.strategy_id!r}",
                provider=self.name,
            )

        payload: dict[str, Any] = {"url": request.url}
        if request.strategy_id == HTTP.id:
            payload[FIELD_HTTP_BODY] = True
            payload[FIELD_HTTP_HEADERS] = True
        else:
            # browserHtml, never their parsed output: a vendor changing its
            # parser must not be able to change our dataset.
            payload[FIELD_BROWSER_HTML] = True

        if request.strategy_id == BROWSER_CAPTURE.id:
            payload[FIELD_NETWORK_CAPTURE] = [dict(f) for f in self._capture_filters]

        if request.geo_code:
            payload["geolocation"] = request.geo_code.upper()

        tags = {"strategy": request.strategy_id}
        if self._run_id:
            tags["run"] = self._run_id
        payload["tags"] = tags
        return payload

    def fetch(self, request: ProviderRequest) -> ProviderResponse:
        """The Provider contract. Any network capture is discarded here.

        A caller that wants discovery uses :meth:`fetch_with_capture`. Keeping
        the envelope on the instance would be stateful and unsafe across
        threads; putting it on ProviderResponse would push a vendor's network
        dump into the vendor-neutral contract.
        """

        response, _ = self.fetch_with_capture(request)
        return response

    def fetch_with_capture(
        self, request: ProviderRequest
    ) -> tuple[ProviderResponse, list[dict[str, Any]]]:
        """Fetch, and return the captured network entries in discovery's shape."""

        if not self.configured:
            raise ProviderError(
                kind=ProviderErrorKind.AUTH,
                message="no API key: set ZYTE_API_KEY in the environment",
                provider=self.name,
            )

        result = post_json(
            self._endpoint,
            self.build_payload(request),
            headers={"Authorization": f"Basic {self._basic_auth()}"},
            provider=self.name,
            timeout_seconds=request.timeout_seconds,
            opener=self._opener,
            max_body_bytes=self._max_body_bytes,
        )

        if result.status != 200:
            self._raise_for_provider_failure(result)

        envelope = result.json(provider=self.name)
        if not isinstance(envelope, dict):
            raise ProviderError(
                kind=ProviderErrorKind.MALFORMED_RESPONSE,
                message="expected a JSON object",
                provider=self.name,
                status=result.status,
            )

        body = self._body_of(envelope, request.strategy_id)
        response = ProviderResponse(
            provider=self.name,
            strategy_id=request.strategy_id,
            # The site's status, stated plainly by the vendor. No heuristic.
            target_status=_as_int(envelope.get(FIELD_TARGET_STATUS)),
            provider_status=result.status,
            body=body,
            headers=_headers_of(envelope),
            final_url=str(envelope.get("url") or request.url),
            latency_ms=result.latency_ms,
            cost=_cost_from(envelope, result.headers),
            request_id=str(envelope.get("echoData") or "") or None,
            truncated=result.truncated,
            from_cache=False,
            content_age_seconds=None,
        )
        return response, self.observed_requests(
            envelope, page_url=response.final_url or request.url
        )

    def _basic_auth(self) -> str:
        """API key as username, empty password — as documented."""

        return base64.b64encode(f"{self._api_key}:".encode()).decode()

    def _raise_for_provider_failure(self, result: Any) -> None:
        """Zyte's own failures. Never a verdict about the target."""

        detail = ""
        error_type = ""
        try:
            envelope = result.json(provider=self.name)
            if isinstance(envelope, dict):
                error_type = str(envelope.get("type") or "")
                detail = str(envelope.get("detail") or envelope.get("title") or "")
        except ProviderError:
            # A non-JSON error body is still a provider failure; the status
            # below carries the meaning.
            pass

        message = f"{error_type or 'error'}: {detail}".strip(": ")
        if error_type.startswith(_AUTH_TYPES) or result.status == 401:
            raise ProviderError(
                kind=ProviderErrorKind.AUTH,
                message=message or "provider rejected the credentials",
                provider=self.name,
                status=result.status,
            )
        if error_type.startswith(_LIMIT_TYPES) or result.status == 429:
            raise ProviderError(
                kind=ProviderErrorKind.QUOTA,
                message=message or "rate or quota limit reached",
                provider=self.name,
                status=result.status,
                retryable=True,
            )
        if error_type.startswith(_REQUEST_TYPES) or result.status in {400, 422}:
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message=message or "provider rejected the request",
                provider=self.name,
                status=result.status,
            )
        raise ProviderError(
            kind=ProviderErrorKind.PROVIDER_FAULT,
            message=message or f"provider error (HTTP {result.status})",
            provider=self.name,
            status=result.status,
            retryable=True,
        )

    def _body_of(self, envelope: dict[str, Any], strategy_id: str) -> bytes:
        """The document, decoded. base64 for HTTP mode, plain text for browser."""

        if strategy_id == HTTP.id:
            raw = envelope.get(FIELD_HTTP_BODY)
            if not raw:
                return b""
            try:
                return base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ProviderError(
                    kind=ProviderErrorKind.MALFORMED_RESPONSE,
                    message=f"httpResponseBody was not valid base64: {exc}",
                    provider=self.name,
                ) from exc
        return str(envelope.get(FIELD_BROWSER_HTML) or "").encode("utf-8")

    # -- discovery ---------------------------------------------------------

    def observed_requests(self, envelope: dict[str, Any], *, page_url: str) -> list[dict[str, Any]]:
        """Translate ``networkCapture`` into what discovery expects.

        Plain mappings for :func:`~web_scraper.discovery.observed_from_mapping`,
        so every existing protection applies unchanged: an entry carrying an
        Authorization header is rejected, a private target is rejected, and no
        response body is persisted anywhere.
        """

        out: list[dict[str, Any]] = []
        for entry in envelope.get(FIELD_NETWORK_CAPTURE) or []:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "")
            if not url:
                continue
            body = b""
            raw = entry.get(FIELD_HTTP_BODY)
            if raw:
                try:
                    body = base64.b64decode(raw, validate=True)
                except (binascii.Error, ValueError):
                    # A capture we cannot decode is one we cannot describe the
                    # shape of. Skipping it beats guessing at a schema.
                    body = b""
            request_part = entry.get("request") or {}
            request_headers = request_part.get("headers") or {}
            out.append(
                {
                    "url": url,
                    "method": str(request_part.get("method") or "GET"),
                    "status": int(entry.get(FIELD_TARGET_STATUS) or 200),
                    "content_type": _content_type_of(entry.get("headers")),
                    "resource_type": "xhr",
                    # Names only, never values.
                    "request_header_names": tuple(_header_names(request_headers)),
                    "request_body": request_part.get("body"),
                    "body": body,
                    "page_url": page_url,
                }
            )
        return out


def _header_names(headers: Any) -> list[str]:
    """Zyte returns headers as a list of {name, value} or as a mapping."""

    if isinstance(headers, dict):
        return [str(k) for k in headers]
    if isinstance(headers, list):
        return [str(h.get("name")) for h in headers if isinstance(h, dict) and h.get("name")]
    return []


def _content_type_of(headers: Any) -> str:
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == "content-type":
                return str(value)
    if isinstance(headers, list):
        for item in headers:
            if isinstance(item, dict) and str(item.get("name", "")).lower() == "content-type":
                return str(item.get("value", ""))
    return ""


def _headers_of(envelope: dict[str, Any]) -> dict[str, str]:
    """Rebuild the few response headers triage reads, in canonical casing.

    MEASURED: Zyte returns header names lowercased (``content-type``). Storing
    them as they arrive meant a consumer asking for ``Content-Type`` found
    nothing, and content detection fell back to sniffing the body instead of
    using the type the server actually declared.
    """

    out: dict[str, str] = {}
    raw = envelope.get(FIELD_HTTP_HEADERS)
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                name = str(item["name"])
                if name.lower() in {"content-type", "content-language", "etag", "last-modified"}:
                    out[name.title()] = str(item.get("value", ""))
    if "Content-Type" not in out and FIELD_BROWSER_HTML in envelope:
        # A rendered page is HTML by construction; saying so keeps content
        # detection from having to guess.
        out["Content-Type"] = "text/html"
    return out


def _cost_from(envelope: dict[str, Any], headers: dict[str, str]) -> ProviderCost:
    """A reported cost if there is one; unknown otherwise. Never zero.

    No cost field is documented on the extract response, so this looks in the
    places a cost could plausibly appear and admits ignorance when it is not
    there. The pricing book turns that into a PROVISIONAL bound when the
    operator has supplied one.
    """

    for key in ("costMicroUsd", "cost_micro_usd"):
        micro = envelope.get(key)
        if micro is not None:
            try:
                return ProviderCost.parse(Decimal(str(micro)) / Decimal("1000000"))
            except (ValueError, TypeError):
                break
    for header in ("zyte-request-cost", "x-request-cost"):
        if header in headers:
            return ProviderCost.parse(headers[header])
    return ProviderCost.unattributed()


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
