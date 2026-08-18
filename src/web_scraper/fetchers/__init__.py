"""Fetch Gateway L0-L2: free-route fetching with mandatory triage.

Paid providers (L3-L4) are intentionally absent: they arrive as isolated
adapters in stage 3 and are gated by the budget ledger, never by this
package.
"""

from web_scraper.fetchers.base import RawResponse, Transport, TransportUnavailable
from web_scraper.fetchers.circuit import CircuitBreaker
from web_scraper.fetchers.gateway import (
    FetchGateway,
    GatewayOutcome,
    default_transport_provider,
    resolve_route_url,
    rules_for_route,
)
from web_scraper.fetchers.pacing import Pacer, parse_retry_after
from web_scraper.fetchers.sessions import SessionPool
from web_scraper.fetchers.transports import (
    PlaywrightRenderTransport,
    ScraplingSessionTransport,
    ScraplingStealthyTransport,
    UrllibTransport,
    playwright_available,
    scrapling_available,
)

__all__ = [
    "CircuitBreaker",
    "FetchGateway",
    "GatewayOutcome",
    "Pacer",
    "PlaywrightRenderTransport",
    "RawResponse",
    "ScraplingSessionTransport",
    "ScraplingStealthyTransport",
    "SessionPool",
    "Transport",
    "TransportUnavailable",
    "UrllibTransport",
    "default_transport_provider",
    "parse_retry_after",
    "playwright_available",
    "resolve_route_url",
    "rules_for_route",
    "scrapling_available",
]
