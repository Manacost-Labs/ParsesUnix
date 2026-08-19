"""Turning a browser render into cheaper routes for the next run.

A page that needs a browser stays expensive until something changes. The change
worth making is nearly always the same: the page fetched its data from an
endpoint, and that endpoint is cheaper, faster and more stable than the markup
around it.

Discovery observes, judges, and *proposes*. It never writes a route: an endpoint
that answered once during one render is a coincidence, and a crawl that silently
began depending on it would be depending on something nobody agreed to call.
"""

from web_scraper.discovery.candidates import (
    CandidateVerdict,
    PaginationHint,
    RouteCandidate,
    SchemaSignature,
    find_matching_fields,
    graphql_operation_of,
    is_noise,
    looks_like_api,
    normalize_endpoint,
    pagination_hint_of,
    redact_url,
)
from web_scraper.discovery.collector import (
    DiscoveryCollector,
    ObservedRequest,
    describe_report,
    observed_from_mapping,
    profile_route_draft,
    summarise,
)

__all__ = [
    "CandidateVerdict",
    "DiscoveryCollector",
    "ObservedRequest",
    "PaginationHint",
    "RouteCandidate",
    "SchemaSignature",
    "describe_report",
    "find_matching_fields",
    "graphql_operation_of",
    "is_noise",
    "looks_like_api",
    "normalize_endpoint",
    "observed_from_mapping",
    "pagination_hint_of",
    "profile_route_draft",
    "redact_url",
    "summarise",
]
