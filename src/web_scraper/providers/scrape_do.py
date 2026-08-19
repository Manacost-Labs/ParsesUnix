"""Scrape.do adapter.

Every header name and every cost below was measured against the live API on
2026-08-19, not taken from memory. The probe that produced them is reproduced in
``docs/providers/scrape-do.md``.

The costs that matter, measured on one plain target:

    normal   1 credit
    render   5 credits
    super   10 credits

and, decisively for how the rest of the system is built: **a 404 target still
costs a credit**. That is why dead URLs must be swept out before anything paid
runs, and why triage's "404 never escalates to a provider" rule is a money rule,
not a stylistic one.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Protocol

from web_scraper.providers._transport import DEFAULT_MAX_BODY_BYTES
from web_scraper.providers.base import (
    ProviderCost,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResponse,
    ProviderStrategy,
)


class _Opener(Protocol):
    """Just enough of urllib's opener for this adapter, so tests can substitute one."""

    def urlopen(self, request: Any, timeout: float = ...) -> Any: ...


API_ENDPOINT = "https://api.scrape.do/"
PROVIDER_NAME = "scrape.do"

#: Date the live API was last verified. CI warns when this goes stale.
DOCS_VERIFIED_AT = "2026-08-19"

#: Response headers, verbatim as the API returns them (lowercased by the client).
HEADER_COST = "scrape.do-request-cost"
HEADER_REMAINING = "scrape.do-remaining-credits"
HEADER_REQUEST_ID = "scrape.do-request-id"
HEADER_TARGET_STATUS = "scrape.do-initial-status-code"
HEADER_RESOLVED_URL = "scrape.do-resolved-url"
HEADER_DETECTED_WAF = "scrape.do-detected-waf"

NORMAL = ProviderStrategy(
    id="normal",
    nominal_cost=Decimal("1"),
    reservation_cost=Decimal("3"),
    description="datacenter proxy, no rendering — the default and the one to beat",
)
RENDER = ProviderStrategy(
    id="render",
    nominal_cost=Decimal("5"),
    reservation_cost=Decimal("10"),
    renders_javascript=True,
    description="headless rendering; for CSR_REQUIRED, never for a dead or failing origin",
)
SUPER = ProviderStrategy(
    id="super",
    nominal_cost=Decimal("10"),
    reservation_cost=Decimal("25"),
    premium_network=True,
    description="residential/mobile network; only against proven blocking",
)
SUPER_RENDER = ProviderStrategy(
    id="super_render",
    nominal_cost=Decimal("15"),
    reservation_cost=Decimal("30"),
    renders_javascript=True,
    premium_network=True,
    description="both at once; the last resort, never a default",
)

STRATEGIES: tuple[ProviderStrategy, ...] = (NORMAL, RENDER, SUPER, SUPER_RENDER)
_BY_ID = {strategy.id: strategy for strategy in STRATEGIES}


class ScrapeDoProvider:
    """Adapter for scrape.do. The token is read from the environment only."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        token: str | None = None,
        token_env: str = "SCRAPE_DO_TOKEN",  # noqa: S107 - a variable NAME, not a secret
        endpoint: str = API_ENDPOINT,
        opener: _Opener | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        # Never accept a token from a profile or a config file: those get
        # committed. Environment or explicit argument only.
        self._token = token or os.environ.get(token_env, "")
        self._endpoint = endpoint
        self._opener = opener
        self._max_body_bytes = max_body_bytes

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def strategies(self) -> tuple[ProviderStrategy, ...]:
        return STRATEGIES

    def build_query(self, request: ProviderRequest) -> dict[str, str]:
        """Query parameters for one call — public so tests can assert them."""

        strategy = _BY_ID.get(request.strategy_id)
        if strategy is None:
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message=f"unknown strategy {request.strategy_id!r}",
                provider=self.name,
            )
        params: dict[str, str] = {
            "token": self._token,
            "url": request.url,
            # Our retry budget is authoritative. Letting the provider retry too
            # would double the spend and hide the real failure rate.
            "disableRetry": "true",
        }
        if strategy.renders_javascript:
            params["render"] = "true"
            if request.wait_selector:
                params["waitSelector"] = request.wait_selector
        if strategy.premium_network:
            params["super"] = "true"
        if request.geo_code:
            params["geoCode"] = request.geo_code
        if request.session_id is not None:
            params["sessionId"] = str(request.session_id)
        return params

    def fetch(self, request: ProviderRequest) -> ProviderResponse:
        if not self.configured:
            raise ProviderError(
                kind=ProviderErrorKind.AUTH,
                message="no token: set SCRAPE_DO_TOKEN in the environment",
                provider=self.name,
            )
        query = self.build_query(request)
        url = self._endpoint + "?" + urllib.parse.urlencode(query)
        started = time.monotonic()
        try:
            opener: _Opener = self._opener or urllib.request  # type: ignore[assignment]
            with opener.urlopen(
                urllib.request.Request(url),  # noqa: S310 - our own constant https endpoint
                timeout=request.timeout_seconds,
            ) as response:
                status = int(response.status)
                headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
                body = response.read(self._max_body_bytes + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            headers = {str(k).lower(): str(v) for k, v in (exc.headers or {}).items()}
            body = exc.read(self._max_body_bytes + 1)
        except TimeoutError as exc:
            raise ProviderError(
                kind=ProviderErrorKind.TIMEOUT,
                message=str(exc),
                provider=self.name,
                retryable=True,
            ) from exc
        except OSError as exc:
            raise ProviderError(
                kind=ProviderErrorKind.TRANSPORT,
                message=str(exc),
                provider=self.name,
                retryable=True,
            ) from exc

        truncated = len(body) > self._max_body_bytes
        if truncated:
            body = body[: self._max_body_bytes]
        latency_ms = int((time.monotonic() - started) * 1000)
        cost = ProviderCost.parse(headers.get(HEADER_COST), remaining=headers.get(HEADER_REMAINING))

        # A provider-side failure is not a verdict about the site. Raise it as a
        # provider error so the core never reads it as "the target is dead".
        if status in {401, 403} and HEADER_COST not in headers:
            raise ProviderError(
                kind=ProviderErrorKind.AUTH,
                message=f"provider rejected the credentials (HTTP {status})",
                provider=self.name,
                status=status,
            )
        if status == 429:
            raise ProviderError(
                kind=ProviderErrorKind.QUOTA,
                message="provider rate limit or quota exhausted",
                provider=self.name,
                status=status,
                retryable=True,
            )
        if status >= 500 and HEADER_TARGET_STATUS not in headers:
            raise ProviderError(
                kind=ProviderErrorKind.PROVIDER_FAULT,
                message=f"provider returned HTTP {status}",
                provider=self.name,
                status=status,
                retryable=True,
            )
        if status >= 400 and HEADER_TARGET_STATUS not in headers:
            # Scrape.do answered 4xx and said nothing about any target, so this
            # is its refusal of OUR call. Without this branch it fell through
            # with no target status at all, triage read the missing status as
            # ORIGIN_DOWN — a NEUTRAL verdict — and the strategy that failed was
            # never charged with it while the URL went back in the queue.
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message=f"provider rejected the request (HTTP {status})",
                provider=self.name,
                status=status,
            )

        target_status = _int_or_none(headers.get(HEADER_TARGET_STATUS))
        if target_status is not None and 300 <= target_status < 400:
            # MEASURED: the header is the status of the FIRST hop, not the last.
            # A rendering call on a page that redirects reported 308 while
            # handing back the final document, and triage read the 308 as a
            # malformed answer — so a perfectly good page came back PARSE_FAIL
            # and the URL looked worth escalating to something dearer. A
            # redirect is by definition not the answer; the envelope status
            # describes the document we were actually given.
            target_status = status
        return ProviderResponse(
            provider=self.name,
            strategy_id=request.strategy_id,
            target_status=target_status or status,
            # Scrape.do mirrors the target's status into its own envelope, so a
            # dead URL arrives as HTTP 404 from a provider that did its job
            # perfectly. Copying that into provider health would report a
            # healthy vendor as failing on every dead URL it correctly
            # described. When it named the target's status, it worked.
            provider_status=200 if target_status is not None else status,
            body=body,
            headers=headers,
            final_url=headers.get(HEADER_RESOLVED_URL) or request.url,
            latency_ms=latency_ms,
            cost=cost,
            request_id=headers.get(HEADER_REQUEST_ID),
            detected_defense=headers.get(HEADER_DETECTED_WAF) or None,
            truncated=truncated,
        )


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
