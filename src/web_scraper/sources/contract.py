"""One shape for social content, whatever platform it came from.

A Reddit comment and a reply on X are the same kind of thing to a consumer:
authored text, at a time, by someone, inside a conversation. Storing them in two
platform-shaped tables pushes that reconciliation onto every downstream query,
and the reconciliation is where the mistakes live — a "score" that means karma
on one platform and likes on the other, silently summed.

Three separations carry the weight here.

**Identity is the platform's, never ours.** ``platform_post_id`` is the id the
platform assigns. Deduplicating on text hashes instead would merge two people
saying "this" and split one post that was edited.

**Completeness is stated, not implied.** A thread with unresolved branches is
recorded as incomplete. A partial conversation that presents itself as whole is
worse than no conversation: an analysis over it is confidently wrong.

**Content freshness and engagement freshness are different facts.** A post's
text is usually final within minutes; its score keeps moving for days. One
``collected_at`` covering both means a consumer either re-fetches text that
never changes or trusts a score from last week.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Platform(StrEnum):
    REDDIT = "reddit"
    X = "x"


class EntityKind(StrEnum):
    """What sort of thing this is inside its conversation."""

    POST = "post"
    COMMENT = "comment"
    REPLY = "reply"
    QUOTE = "quote"
    REPOST = "repost"


class CollectionRoute(StrEnum):
    """How this entity was obtained. Part of its provenance, not a detail."""

    #: The platform's own sanctioned interface, with our credentials.
    OFFICIAL_API = "official_api"
    #: A permitted public page, through the ordinary free route ladder.
    PUBLIC_PAGE = "public_page"
    #: A permitted public page, fetched through a paid provider.
    PROVIDER = "provider"


@dataclass(frozen=True)
class Engagement:
    """Counts that keep moving after the content is final."""

    score: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    like_count: int | None = None
    view_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "comment_count": self.comment_count,
            "share_count": self.share_count,
            "like_count": self.like_count,
            "view_count": self.view_count,
        }


@dataclass(frozen=True)
class Provenance:
    """Where this came from and under what terms.

    Recorded per entity rather than per run: a thread can legitimately mix a
    post fetched from the official API with replies read off a public page, and
    a consumer deciding whether they may republish something needs to know which
    is which.
    """

    platform: Platform
    route: CollectionRoute
    source_url: str
    collected_at: float
    #: Set only when a paid provider fetched it, so cost can be traced back.
    provider: str | None = None
    strategy_id: str | None = None
    #: The adapter that produced this, for reproducing a result later.
    adapter: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform.value,
            "route": self.route.value,
            "source_url": self.source_url,
            "collected_at": self.collected_at,
            "provider": self.provider,
            "strategy_id": self.strategy_id,
            "adapter": self.adapter,
        }


@dataclass(frozen=True)
class SocialEntity:
    """One post, comment or reply, normalised across platforms."""

    platform: Platform
    platform_post_id: str
    kind: EntityKind
    created_at: float | None
    text: str
    provenance: Provenance

    title: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    community: str | None = None
    conversation_id: str | None = None
    reply_to_id: str | None = None
    depth: int = 0
    canonical_url: str | None = None
    engagement: Engagement = field(default_factory=Engagement)
    #: When the engagement numbers were read. Distinct from ``collected_at``
    #: because a score re-read tomorrow does not make the text newer.
    engagement_collected_at: float | None = None
    attachments: tuple[str, ...] = ()
    external_links: tuple[str, ...] = ()

    @property
    def natural_key(self) -> str:
        """Stable identity for deduplication. Platform id, never text.

        Text hashing would merge two people writing "this" and split one post
        that was edited for a typo.
        """

        return f"{self.platform.value}:{self.platform_post_id}"

    @property
    def is_root(self) -> bool:
        return self.reply_to_id is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "natural_key": self.natural_key,
            "platform": self.platform.value,
            "platform_post_id": self.platform_post_id,
            "kind": self.kind.value,
            "title": self.title,
            "text": self.text,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "community": self.community,
            "conversation_id": self.conversation_id,
            "reply_to_id": self.reply_to_id,
            "depth": self.depth,
            "created_at": self.created_at,
            "canonical_url": self.canonical_url,
            "engagement": self.engagement.to_dict(),
            "engagement_collected_at": self.engagement_collected_at,
            "attachments": list(self.attachments),
            "external_links": list(self.external_links),
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class ThreadCompleteness:
    """Whether a conversation was fully retrieved — stated, never assumed.

    ``more_nodes`` is the platform telling us there is more it did not send.
    ``unresolved_branches`` is what we chose not to follow, usually a depth or
    request ceiling of our own. Both make a thread incomplete, but they are
    different problems: the first is the platform's paging, the second is our
    budget, and an operator can only act on the second.
    """

    entities_loaded: int = 0
    more_nodes: int = 0
    unresolved_branches: tuple[str, ...] = ()
    depth_limit_hit: bool = False
    request_limit_hit: bool = False

    @property
    def is_complete(self) -> bool:
        return (
            self.more_nodes == 0
            and not self.unresolved_branches
            and not self.depth_limit_hit
            and not self.request_limit_hit
        )

    @property
    def reason(self) -> str:
        if self.is_complete:
            return "every branch was retrieved"
        parts = []
        if self.more_nodes:
            parts.append(f"{self.more_nodes} more-node(s) the platform did not send")
        if self.unresolved_branches:
            # Naming them, not counting them: "1 branch not followed" tells an
            # operator nothing they can act on.
            parts.append("not followed: " + "; ".join(self.unresolved_branches))
        if self.depth_limit_hit:
            parts.append("our depth ceiling was reached")
        if self.request_limit_hit:
            parts.append("our request ceiling was reached")
        return "; ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entities_loaded": self.entities_loaded,
            "more_nodes": self.more_nodes,
            "unresolved_branches": list(self.unresolved_branches),
            "depth_limit_hit": self.depth_limit_hit,
            "request_limit_hit": self.request_limit_hit,
            "thread_complete": self.is_complete,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Thread:
    """A conversation and an honest statement of how much of it we have."""

    root: SocialEntity
    entities: tuple[SocialEntity, ...]
    completeness: ThreadCompleteness

    @property
    def is_complete(self) -> bool:
        return self.completeness.is_complete

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root.to_dict(),
            "entities": [e.to_dict() for e in self.entities],
            "completeness": self.completeness.to_dict(),
        }


@dataclass(frozen=True)
class SearchWindow:
    """What to search for, in terms every platform can express."""

    query: str
    community: str | None = None
    since: float | None = None
    until: float | None = None
    sort: str = "new"
    limit: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "community": self.community,
            "since": self.since,
            "until": self.until,
            "sort": self.sort,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class Checkpoint:
    """Where an incremental collection got to, so the next run resumes.

    Both a cursor and a timestamp are kept. A cursor is exact but platform-
    specific and can expire; a timestamp always works but can re-deliver items
    on the boundary. Storing both means a run can prefer the cursor and still
    have something to fall back to when it is refused.
    """

    latest_seen_id: str | None = None
    latest_seen_at: float | None = None
    cursor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "latest_seen_id": self.latest_seen_id,
            "latest_seen_at": self.latest_seen_at,
            "cursor": self.cursor,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Checkpoint:
        if not data:
            return cls()
        return cls(
            latest_seen_id=data.get("latest_seen_id"),
            latest_seen_at=data.get("latest_seen_at"),
            cursor=data.get("cursor"),
        )


class SocialAccountingError(RuntimeError):
    """Raised when a collection cannot account for what it discovered."""


@dataclass(frozen=True)
class SocialAccounting:
    """Every discovered entity must end somewhere. Unaccounted must be zero."""

    discovered: int = 0
    resolved: int = 0
    partial: int = 0
    unavailable: int = 0

    @property
    def unaccounted(self) -> int:
        return self.discovered - (self.resolved + self.partial + self.unavailable)

    @property
    def is_complete(self) -> bool:
        return self.unaccounted == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovered": self.discovered,
            "resolved": self.resolved,
            "partial": self.partial,
            "unavailable": self.unavailable,
            "unaccounted": self.unaccounted,
            "complete": self.is_complete,
        }


def dedupe(entities: Sequence[SocialEntity]) -> list[SocialEntity]:
    """Collapse by platform id, keeping the first occurrence.

    Deliberately not by content: an edited post is the same post, and two people
    posting the same word are not.
    """

    seen: set[str] = set()
    out: list[SocialEntity] = []
    for entity in entities:
        if entity.natural_key in seen:
            continue
        seen.add(entity.natural_key)
        out.append(entity)
    return out


def external_links_of(
    entities: Sequence[SocialEntity],
    *,
    allowed_domains: Sequence[str] = (),
    max_per_entity: int = 3,
) -> list[str]:
    """Links worth handing to the ordinary web pipeline, bounded.

    Unbounded link following turns one social query into an open-ended crawl of
    whatever the internet linked to that day. The caller states which domains it
    wants and how many links per entity it will take.
    """

    from urllib.parse import urlsplit

    allowed = {d.lower().lstrip(".") for d in allowed_domains}
    out: list[str] = []
    for entity in entities:
        taken = 0
        for link in entity.external_links:
            if taken >= max_per_entity:
                break
            host = urlsplit(link).netloc.lower()
            if allowed and not any(host == d or host.endswith(f".{d}") for d in allowed):
                continue
            out.append(link)
            taken += 1
    return out
