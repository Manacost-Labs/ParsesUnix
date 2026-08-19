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
    description="datacenter proxy, no rendering — the default and the one to beat",
)
RENDER = ProviderStrategy(
    id="render",
    nominal_cost=Decimal("5"),
    renders_javascript=True,
    description="headless rendering; for CSR_REQUIRED, never for a dead or failing origin",
)
SUPER = ProviderStrategy(
    id="super",
    nominal_cost=Decimal("10"),
    premium_network=True,
    description="residential/mobile network; only against proven blocking",
)
SUPER_RENDER = ProviderStrategy(
    id="super_render",
    nominal_cost=Decimal("15"),
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
    ) -> None:
        # Never accept a token from a profile or a config file: those get
        # committed. Environment or explicit argument only.
        self._token = token or os.environ.get(token_env, "")
        self._endpoint = endpoint
        self._opener = opener

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
                body = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            headers = {str(k).lower(): str(v) for k, v in (exc.headers or {}).items()}
            body = exc.read()
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

        return ProviderResponse(
            provider=self.name,
            strategy_id=request.strategy_id,
            target_status=_int_or_none(headers.get(HEADER_TARGET_STATUS)) or status,
            provider_status=status,
            body=body,
            headers=headers,
            final_url=headers.get(HEADER_RESOLVED_URL) or request.url,
            latency_ms=latency_ms,
            cost=cost,
            request_id=headers.get(HEADER_REQUEST_ID),
            detected_defense=headers.get(HEADER_DETECTED_WAF) or None,
        )


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
