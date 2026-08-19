"""Paid provider adapters behind one vendor-neutral contract.

Adapters translate a vendor's wire format into :mod:`web_scraper.providers.base`
types. They never decide escalation: whether a paid call happens at all remains
a triage verdict gated by the budget.
"""

from web_scraper.providers.base import (
    Provider,
    ProviderCost,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResponse,
    ProviderStrategy,
)
from web_scraper.providers.breaker import (
    Admission,
    BreakerState,
    BreakerStore,
    ProviderBreakers,
)
from web_scraper.providers.bright_data import BrightDataProvider
from web_scraper.providers.escalation import PaidAttempt, PaidEscalator
from web_scraper.providers.firecrawl import FirecrawlProvider
from web_scraper.providers.multi_escalation import MultiProviderEscalator
from web_scraper.providers.multi_router import (
    Candidate,
    MultiProviderDecision,
    MultiProviderRouter,
)
from web_scraper.providers.router import PaidDecision, PaidProviderRouter
from web_scraper.providers.scrape_do import ScrapeDoProvider
from web_scraper.providers.stats import (
    ProviderStatsStore,
    ProviderStrategyKey,
    ProviderStrategyStats,
)

__all__ = [
    "Admission",
    "BreakerState",
    "BreakerStore",
    "BrightDataProvider",
    "Candidate",
    "FirecrawlProvider",
    "MultiProviderDecision",
    "MultiProviderEscalator",
    "MultiProviderRouter",
    "PaidAttempt",
    "PaidDecision",
    "PaidEscalator",
    "PaidProviderRouter",
    "Provider",
    "ProviderBreakers",
    "ProviderCost",
    "ProviderError",
    "ProviderErrorKind",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderStatsStore",
    "ProviderStrategy",
    "ProviderStrategyKey",
    "ProviderStrategyStats",
    "ScrapeDoProvider",
]
