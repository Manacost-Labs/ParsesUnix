"""Social sources behind one normalised contract.

Reddit and X are collected in very different ways — one through a sanctioned
API with our own credentials, one through the ordinary public route ladder — and
both produce the same :class:`~web_scraper.sources.contract.SocialEntity`. The
difference in how something was obtained is recorded as provenance rather than
being flattened away, because a consumer deciding what they may do with a record
needs to know which route produced it.
"""

from web_scraper.sources.contract import (
    Checkpoint,
    CollectionRoute,
    Engagement,
    EntityKind,
    Platform,
    Provenance,
    SearchWindow,
    SocialAccounting,
    SocialEntity,
    Thread,
    ThreadCompleteness,
    dedupe,
    external_links_of,
)
from web_scraper.sources.reddit import RateLimited, RedditAccessError, RedditAdapter
from web_scraper.sources.x import XApiConfig, XSearchAdapter, XSearchResult

__all__ = [
    "Checkpoint",
    "CollectionRoute",
    "Engagement",
    "EntityKind",
    "Platform",
    "Provenance",
    "RateLimited",
    "RedditAccessError",
    "RedditAdapter",
    "SearchWindow",
    "SocialAccounting",
    "SocialEntity",
    "Thread",
    "ThreadCompleteness",
    "XApiConfig",
    "XSearchAdapter",
    "XSearchResult",
    "dedupe",
    "external_links_of",
]
