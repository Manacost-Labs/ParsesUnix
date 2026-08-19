"""Paid provider adapters behind one vendor-neutral contract.

Adapters translate a vendor's wire format into :mod:`web_scraper.providers.base`
types. They never decide escalation: whether a paid call happens at all remains
a triage verdict gated by the budget.
"""

from collections.abc import Mapping

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
from web_scraper.providers.pricing import (
    DEFAULT_SNAPSHOTS,
    PricingBook,
    PricingSnapshot,
    StrategyRate,
)
from web_scraper.providers.router import PaidDecision, PaidProviderRouter
from web_scraper.providers.scrape_do import ScrapeDoProvider
from web_scraper.providers.stats import (
    ProviderStatsStore,
    ProviderStrategyKey,
    ProviderStrategyStats,
)
from web_scraper.providers.zenrows import ZenRowsProvider
from web_scraper.providers.zyte import ZyteProvider

#: The date each adapter was last checked against the vendor's LIVE API, as
#: opposed to against its documentation. A provider absent from this mapping has
#: never been verified against a real call, and the preflight says so.
#:
#: Every one of these dates records a session that found at least one defect the
#: documentation, the types and the unit tests had all missed. The dates are
#: therefore not a formality: they are the difference between "we believe this
#: adapter works" and "we watched it work".
LIVE_VERIFIED_AT: Mapping[str, str] = {
    "scrape.do": "2026-08-19",
    "firecrawl": "2026-08-19",
    "brightdata": "2026-08-19",
    "zenrows": "2026-08-19",
    "zyte": "2026-08-19",
}

__all__ = [
    "DEFAULT_SNAPSHOTS",
    "LIVE_VERIFIED_AT",
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
    "PricingBook",
    "PricingSnapshot",
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
    "StrategyRate",
    "ZenRowsProvider",
    "ZyteProvider",
]
