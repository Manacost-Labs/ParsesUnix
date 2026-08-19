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
from web_scraper.providers.scrape_do import ScrapeDoProvider

__all__ = [
    "Provider",
    "ProviderCost",
    "ProviderError",
    "ProviderErrorKind",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderStrategy",
    "ScrapeDoProvider",
]
