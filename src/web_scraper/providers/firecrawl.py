"""Firecrawl adapter.

Verified against the live v2 documentation on 2026-08-19 (see
``docs/providers/firecrawl.md``). Nothing here is written from memory; where the
documentation is silent, this adapter reports *unknown* rather than guessing.

Firecrawl's role in this system is managed rendering and content normalisation,
not truth. Its markdown is convenient, but a 200 from Firecrawl says only that
Firecrawl answered — the body still goes through canonical triage like every
other byte stream, and a challenge page rendered perfectly into clean markdown
is still a challenge page.

Two documented facts shape this adapter more than any other:

**The cache is on by default.** ``maxAge`` defaults to 172800000 ms — two days.
A scraper that silently accepted that would publish two-day-old content as
current. This adapter therefore requests a live fetch (``maxAge=0``) on its
default strategies, and the one cache-using strategy marks its answers as
unprovable so freshness cannot be claimed for them.

**Per-mode pricing is not documented.** Firecrawl publishes "1 credit per page"
and says advanced features cost more, without a per-mode table, and documents no
cost response header. Reservations here are therefore conservative, and a call
whose cost is not reported settles as unknown spend rather than as free.
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

API_ENDPOINT = "https://api.firecrawl.dev/v2/scrape"
PROVIDER_NAME = "firecrawl"

#: Date the live documentation was last read end to end.
DOCS_VERIFIED_AT = "2026-08-19"

#: Firecrawl's own default, documented in ms. Recorded so the comment above is
#: checkable rather than folklore.
DOCUMENTED_DEFAULT_MAX_AGE_MS = 172_800_000

#: Documented list price. Not a measurement of what any given call costs.
DOCUMENTED_CREDITS_PER_PAGE = Decimal("1")

#: Header names are NOT documented. These are tried opportunistically; when none
#: is present the cost is unattributed, which is the honest answer.
CANDIDATE_COST_HEADERS = ("x-credits-used", "x-firecrawl-credits-used")
CANDIDATE_REMAINING_HEADERS = ("x-credits-remaining", "x-firecrawl-credits-remaining")

BASIC = ProviderStrategy(
    id="basic",
    nominal_cost=DOCUMENTED_CREDITS_PER_PAGE,
    reservation_cost=Decimal("2"),
    renders_javascript=True,
    premium_network=False,
    description="proxy=basic; fast pool, managed rendering, live fetch",
)

AUTO = ProviderStrategy(
    id="auto",
    nominal_cost=DOCUMENTED_CREDITS_PER_PAGE,
    # Documented to retry with the enhanced pool when basic fails, so one call
    # can become two fetches. The hold covers that even though the published
    # price does not mention it.
    reservation_cost=Decimal("4"),
    renders_javascript=True,
    premium_network=True,
    description="proxy=auto; retries on the enhanced pool if basic is refused",
)

ENHANCED = ProviderStrategy(
    id="enhanced",
    nominal_cost=DOCUMENTED_CREDITS_PER_PAGE,
    reservation_cost=Decimal("5"),
    renders_javascript=True,
    premium_network=True,
    description="proxy=enhanced; slower pool documented for advanced anti-bot",
)

CACHED = ProviderStrategy(
    id="cached",
    nominal_cost=DOCUMENTED_CREDITS_PER_PAGE,
    reservation_cost=Decimal("2"),
    renders_javascript=True,
    premium_network=False,
    description="serves Firecrawl's cache; cheap and fast, freshness NOT provable",
)

STRATEGIES: tuple[ProviderStrategy, ...] = (BASIC, CACHED, AUTO, ENHANCED)

_PROXY_MODE = {BASIC.id: "basic", AUTO.id: "auto", ENHANCED.id: "enhanced", CACHED.id: "basic"}


class FirecrawlProvider:
    """Firecrawl behind the vendor-neutral contract."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str | None = None,
        token_env: str = "FIRECRAWL_API_KEY",  # noqa: S107 - a variable NAME, not a secret
        endpoint: str = API_ENDPOINT,
        cache_max_age_ms: int = 0,
        opener: Opener | None = None,
    ) -> None:
        # Env-only by default, like every other credential in this project: a
        # key in a profile or a run config ends up in a snapshot or a commit.
        self._api_key = api_key or os.environ.get(token_env, "")
        self._endpoint = endpoint
        self._cache_max_age_ms = cache_max_age_ms
        self._opener = opener

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def strategies(self) -> tuple[ProviderStrategy, ...]:
        return STRATEGIES

    def build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        """The request body, exposed so tests can assert it without a network."""

        proxy = _PROXY_MODE.get(request.strategy_id)
        if proxy is None:
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message=f"unknown strategy {request.strategy_id!r}",
                provider=self.name,
            )

        payload: dict[str, Any] = {
            "url": request.url,
            # rawHtml, not markdown: triage and the extraction chain reason about
            # the document, and Firecrawl's markdown has already thrown away the
            # canaries, the JSON-LD and the challenge markup we detect blocks by.
            "formats": ["rawHtml"],
            "onlyMainContent": False,
            "proxy": proxy,
            # Firecrawl's default is two days of cache. Ask for a live fetch
            # unless this is explicitly the cache-using strategy.
            "maxAge": self._cache_max_age_ms if request.strategy_id == CACHED.id else 0,
            "timeout": int(request.timeout_seconds * 1000),
        }
        if request.wait_selector:
            payload["actions"] = [{"type": "wait", "selector": request.wait_selector}]
        if request.geo_code:
            payload["location"] = {"country": request.geo_code}
        return payload

    def fetch(self, request: ProviderRequest) -> ProviderResponse:
        if not self.configured:
            raise ProviderError(
                kind=ProviderErrorKind.AUTH,
                message="no API key: set FIRECRAWL_API_KEY in the environment",
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

        self._raise_for_provider_failure(result.status)
        envelope = result.json(provider=self.name)
        if not isinstance(envelope, dict):
            raise ProviderError(
                kind=ProviderErrorKind.MALFORMED_RESPONSE,
                message="expected a JSON object",
                provider=self.name,
                status=result.status,
            )
        if not envelope.get("success", False):
            raise ProviderError(
                kind=ProviderErrorKind.PROVIDER_FAULT,
                message=str(envelope.get("error") or "provider reported failure"),
                provider=self.name,
                status=result.status,
                retryable=True,
            )

        data = envelope.get("data") or {}
        metadata = data.get("metadata") or {}
        body = str(data.get("rawHtml") or data.get("html") or "").encode("utf-8")

        return ProviderResponse(
            provider=self.name,
            strategy_id=request.strategy_id,
            # The site's status, carried in metadata. Firecrawl's own 200 says
            # only that Firecrawl worked.
            target_status=_as_int(metadata.get("statusCode")),
            provider_status=result.status,
            body=body,
            headers=_content_headers(metadata),
            final_url=str(metadata.get("url") or metadata.get("sourceURL") or request.url),
            latency_ms=result.latency_ms,
            cost=_cost_from(result.headers),
            request_id=result.headers.get("x-request-id"),
            # Firecrawl documents no field stating the age of a cached body, so
            # a cache-eligible call can never prove freshness. Saying "unknown"
            # keeps a stale record from being published as current.
            from_cache=None if request.strategy_id == CACHED.id else False,
            content_age_seconds=None,
        )

    def _raise_for_provider_failure(self, status: int) -> None:
        """Vendor-side failures, never verdicts about the target."""

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
                message="payment required: credits exhausted",
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
            raise ProviderError(
                kind=ProviderErrorKind.BAD_REQUEST,
                message="provider rejected the request payload",
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
    """Firecrawl documents no cost header; try, then admit ignorance."""

    for name in CANDIDATE_COST_HEADERS:
        if name in headers:
            remaining = next(
                (headers[r] for r in CANDIDATE_REMAINING_HEADERS if r in headers), None
            )
            return ProviderCost.parse(headers[name], remaining=remaining)
    return ProviderCost.unattributed()


def _content_headers(metadata: dict[str, Any]) -> dict[str, str]:
    """Rebuild the few response headers triage actually reads."""

    headers: dict[str, str] = {}
    content_type = metadata.get("contentType")
    if content_type:
        headers["Content-Type"] = str(content_type)
    return headers


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
