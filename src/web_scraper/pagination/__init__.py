"""Pagination: knowing how a listing continues, and when to stop.

Two failures this package exists to prevent. A crawl that stops early and
reports success is silent data loss. A crawl that never stops is an outage of
its own making — an infinite scroll with no bound will keep asking for more
until something else breaks.

So every traversal ends with an explicit :class:`StopReason`, and the caller can
tell "the listing ended" apart from "we hit our own ceiling".
"""

from web_scraper.pagination.model import (
    PaginationPlan,
    PaginationStrategy,
    StopReason,
    TraversalBudget,
    TraversalState,
    detect_strategy,
)

__all__ = [
    "PaginationPlan",
    "PaginationStrategy",
    "StopReason",
    "TraversalBudget",
    "TraversalState",
    "detect_strategy",
]
