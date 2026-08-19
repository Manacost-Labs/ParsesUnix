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
from web_scraper.providers.breaker import ProviderBreakers
from web_scraper.providers.escalation import PaidAttempt, PaidEscalator
from web_scraper.providers.router import PaidDecision, PaidProviderRouter
from web_scraper.providers.scrape_do import ScrapeDoProvider

__all__ = [
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
    "ProviderStrategy",
    "ScrapeDoProvider",
]
