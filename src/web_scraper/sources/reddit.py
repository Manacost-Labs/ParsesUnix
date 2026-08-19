"""Reddit through its own sanctioned interface, and nothing else.

**Access note, recorded because it shaped this file.** Reddit blocks this
project's user agent: `reddit.com` and `redditinc.com` refuse our requests, so
the official API documentation could not be read directly. The endpoint shapes
and rate-limit header names below come from *secondary* sources and are marked
``NOT VENDOR VERIFIED``. Nothing here is a measurement.

That blocking is also the reason this adapter talks only to the OAuth API with
credentials the operator registers themselves. Scraping Reddit's public pages
after the site has refused this crawler would be routing around an access
decision the platform has already made, and no amount of "the data is public"
changes what that is.

Two consequences worth stating plainly:

**Rate limits are read, never assumed.** Secondary sources disagree about the
per-client ceiling — 60 requests per minute in one, about 100 in another. Since
they disagree and neither is the vendor, this adapter does not encode a number
at all: it reads ``X-Ratelimit-Remaining`` and ``X-Ratelimit-Reset`` from each
response and paces itself on what the platform actually says.

**A 429 defers; it never rotates.** Being rate limited is the platform telling
us we are going too fast. Sending the same request through a different address
is not a fix for that, it is a way of not hearing it. The adapter waits for the
window the response names.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

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
)

logger = logging.getLogger(__name__)

#: NOT VENDOR VERIFIED — see the module docstring. Confirm before production.
OAUTH_BASE = "https://oauth.reddit.com"
TOKEN_ENDPOINT = "https://www.reddit.com/api/v1/access_token"  # noqa: S105 - a URL
DOCS_VERIFIED_AT = "NOT VENDOR VERIFIED (2026-08-19: reddit.com refuses this user agent)"

#: Rate-limit headers, per secondary sources. Absence is handled: if Reddit ever
#: renames these, we lose the pacing signal but never mistake it for "unlimited".
HEADER_REMAINING = "x-ratelimit-remaining"
HEADER_RESET = "x-ratelimit-reset"
HEADER_USED = "x-ratelimit-used"

#: Below this many remaining requests, slow down before the platform has to say
#: so. Being told 429 is a failure we could have avoided by reading the header.
LOW_WATER_MARK = 5

#: Ceilings on one thread traversal. A conversation with ten thousand comments
#: is a legitimate thing to refuse; what is not legitimate is refusing it and
#: calling the result complete.
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_REQUESTS = 20


class RedditAccessError(RuntimeError):
    """The platform refused us, or we have no credentials to offer it."""


class RateLimited(RuntimeError):
    """We are going too fast. Carries the platform's own wait."""

    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(f"rate limited; retry after {retry_after_seconds:.0f}s")
        self.retry_after_seconds = retry_after_seconds


class Opener(Protocol):
    def urlopen(self, request: Any, timeout: float = ...) -> Any: ...


@dataclass
class RateLimitState:
    """What the platform last told us about our budget."""

    remaining: float | None = None
    reset_seconds: float | None = None
    used: float | None = None
    observed_at: float = 0.0

    @property
    def should_slow_down(self) -> bool:
        return self.remaining is not None and self.remaining <= LOW_WATER_MARK

    def to_dict(self) -> dict[str, Any]:
        return {
            "remaining": self.remaining,
            "reset_seconds": self.reset_seconds,
            "used": self.used,
            "observed_at": self.observed_at,
            "should_slow_down": self.should_slow_down,
        }


@dataclass
class RedditAdapter:
    """Search and thread reconstruction over Reddit's OAuth API.

    Credentials are the operator's own, from the environment. This adapter has
    no fallback to public pages: if the API refuses, the answer is that we could
    not collect the data, not that we found another way in.
    """

    client_id: str = ""
    client_secret: str = ""
    user_agent: str = ""
    opener: Opener | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    max_depth: int = DEFAULT_MAX_DEPTH
    max_requests: int = DEFAULT_MAX_REQUESTS
    _token: str | None = field(default=None, repr=False)
    _token_expires_at: float = field(default=0.0, repr=False)
    rate_limit: RateLimitState = field(default_factory=RateLimitState)

    def __post_init__(self) -> None:
        import os

        self.client_id = self.client_id or os.environ.get("REDDIT_CLIENT_ID", "")
        self.client_secret = self.client_secret or os.environ.get("REDDIT_CLIENT_SECRET", "")
        # Reddit's own guidance is that a client identifies itself honestly.
        # A generic or absent agent is how a client gets blocked, and imitating
        # a browser here would be pretending not to be an application.
        self.user_agent = self.user_agent or os.environ.get(
            "REDDIT_USER_AGENT", "ParsesUnix/0.3 (+https://github.com/Manacost-Labs/ParsesUnix)"
        )

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # -- transport ---------------------------------------------------------

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Return the payload as the platform sent it.

        Deliberately not coerced to a dict: the comments endpoint answers with a
        LIST of two listings — the submission, then its comment forest — and
        flattening that to ``{}`` silently loses the entire thread.
        """

        if not self.configured:
            raise RedditAccessError(
                "no credentials: set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET. "
                "This adapter has no unauthenticated fallback by design."
            )
        self._respect_rate_limit()

        url = f"{OAUTH_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        request = urllib.request.Request(  # noqa: S310 - constant https host
            url,
            headers={
                "Authorization": f"bearer {self._access_token()}",
                "User-Agent": self.user_agent,
            },
        )
        client: Any = self.opener or urllib.request
        try:
            with client.urlopen(request, timeout=30) as response:
                headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
                body = response.read()
        except urllib.error.HTTPError as exc:
            headers = {str(k).lower(): str(v) for k, v in (exc.headers or {}).items()}
            self._observe_rate_limit(headers)
            if exc.code == 429:
                raise RateLimited(_retry_after(headers, default=60.0)) from exc
            if exc.code in {401, 403}:
                raise RedditAccessError(f"platform refused the request (HTTP {exc.code})") from exc
            raise RedditAccessError(f"HTTP {exc.code} from the platform") from exc

        self._observe_rate_limit(headers)
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise RedditAccessError(f"response was not JSON: {exc}") from exc
        return payload

    def _access_token(self) -> str:
        if self._token and self.clock() < self._token_expires_at:
            return self._token
        data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        auth = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        del auth  # basic auth is built by hand below to keep the opener injectable
        import base64

        secret = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        request = urllib.request.Request(
            TOKEN_ENDPOINT,
            data=data,
            headers={
                "Authorization": f"Basic {secret}",
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        client: Any = self.opener or urllib.request
        try:
            with client.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            raise RedditAccessError(f"token request refused (HTTP {exc.code})") from exc
        token = payload.get("access_token")
        if not token:
            raise RedditAccessError("token response carried no access_token")
        self._token = str(token)
        self._token_expires_at = self.clock() + float(payload.get("expires_in", 3600)) - 60
        return self._token

    def _observe_rate_limit(self, headers: dict[str, str]) -> None:
        self.rate_limit = RateLimitState(
            remaining=_as_float(headers.get(HEADER_REMAINING)),
            reset_seconds=_as_float(headers.get(HEADER_RESET)),
            used=_as_float(headers.get(HEADER_USED)),
            observed_at=self.clock(),
        )

    def _respect_rate_limit(self) -> None:
        """Slow down before being told to, using the platform's own numbers.

        This is the whole rate-limit strategy. There is deliberately no other
        one: no address rotation, no parallel credentials. A 429 is the platform
        saying we are going too fast, and the only honest response is to go
        slower.
        """

        if not self.rate_limit.should_slow_down:
            return
        wait = self.rate_limit.reset_seconds or 1.0
        logger.info(
            "reddit rate budget nearly spent (%s remaining); waiting %.0fs",
            self.rate_limit.remaining,
            wait,
        )
        self.sleep(wait)

    # -- search ------------------------------------------------------------

    def search(
        self, window: SearchWindow, *, checkpoint: Checkpoint | None = None
    ) -> tuple[list[SocialEntity], Checkpoint, SocialAccounting]:
        """One page of results, plus where to resume."""

        path = f"/r/{window.community}/search" if window.community else "/search"
        params: dict[str, Any] = {
            "q": window.query,
            "sort": window.sort,
            "limit": min(window.limit, 100),
            "raw_json": 1,
        }
        if window.community:
            params["restrict_sr"] = "true"
        if checkpoint and checkpoint.cursor:
            params["after"] = checkpoint.cursor

        payload = self._request(path, params)
        if not isinstance(payload, dict):
            raise RedditAccessError("search returned an unexpected shape")
        children = payload.get("data", {}).get("children", []) or []

        discovered = len(children)
        entities: list[SocialEntity] = []
        unavailable = 0
        for child in children:
            entity = self._entity_from(child.get("data") or {}, kind=EntityKind.POST)
            if entity is None:
                # Deleted or otherwise unreadable. Counted, never dropped: an
                # entity that vanishes from the accounting is one nobody knows
                # to look for.
                unavailable += 1
                continue
            if not _within(entity, window):
                unavailable += 1
                continue
            entities.append(entity)

        entities = dedupe(entities)
        newest = max((e.created_at or 0.0) for e in entities) if entities else None
        next_checkpoint = Checkpoint(
            latest_seen_id=entities[0].platform_post_id if entities else None,
            latest_seen_at=newest
            if newest
            else (checkpoint.latest_seen_at if checkpoint else None),
            cursor=payload.get("data", {}).get("after"),
        )
        accounting = SocialAccounting(
            discovered=discovered, resolved=len(entities), unavailable=unavailable
        )
        return entities, next_checkpoint, accounting

    # -- threads -----------------------------------------------------------

    def thread(self, submission_id: str, *, community: str | None = None) -> Thread:
        """A post and as much of its comment tree as we are willing to fetch."""

        path = (
            f"/r/{community}/comments/{submission_id}"
            if community
            else f"/comments/{submission_id}"
        )
        payload = self._request(path, {"raw_json": 1, "depth": self.max_depth})
        listings = payload if isinstance(payload, list) else [payload]
        post_children = _children(listings[0]) if listings else []
        if not post_children:
            raise RedditAccessError(f"no submission in the response for {submission_id}")
        root = self._entity_from(post_children[0].get("data") or {}, kind=EntityKind.POST)
        if root is None:
            raise RedditAccessError(f"submission {submission_id} is unreadable")

        comments: list[SocialEntity] = []
        more_nodes = 0
        unresolved: list[str] = []
        depth_hit = False

        forest = _children(listings[1]) if len(listings) > 1 else []
        stack = [(node, 1, root.platform_post_id) for node in forest]
        while stack:
            node, depth, parent_id = stack.pop(0)
            kind = node.get("kind")
            data = node.get("data") or {}
            if kind == "more":
                # The platform is telling us there is more it did not send.
                count = int(data.get("count") or len(data.get("children") or []) or 0)
                more_nodes += max(count, 1)
                continue
            if depth > self.max_depth:
                depth_hit = True
                unresolved.append(str(data.get("id") or "?"))
                continue

            entity = self._entity_from(
                data,
                kind=EntityKind.COMMENT if depth == 1 else EntityKind.REPLY,
                conversation_id=root.platform_post_id,
                reply_to_id=parent_id,
                depth=depth,
            )
            if entity is None:
                continue
            comments.append(entity)

            replies = data.get("replies")
            if isinstance(replies, dict):
                for child in _children(replies):
                    stack.append((child, depth + 1, entity.platform_post_id))

        completeness = ThreadCompleteness(
            entities_loaded=len(comments) + 1,
            more_nodes=more_nodes,
            unresolved_branches=tuple(unresolved),
            depth_limit_hit=depth_hit,
        )
        return Thread(root=root, entities=(root, *comments), completeness=completeness)

    # -- mapping -----------------------------------------------------------

    def _entity_from(
        self,
        data: dict[str, Any],
        *,
        kind: EntityKind,
        conversation_id: str | None = None,
        reply_to_id: str | None = None,
        depth: int = 0,
    ) -> SocialEntity | None:
        post_id = data.get("id")
        if not post_id:
            return None
        author = data.get("author")
        if author in {"[deleted]", "[removed]"}:
            author = None
        body = data.get("selftext") or data.get("body") or ""
        if body in {"[deleted]", "[removed]"}:
            body = ""

        permalink = data.get("permalink")
        canonical = f"https://www.reddit.com{permalink}" if permalink else None
        now = self.clock()

        links: list[str] = []
        outbound = data.get("url")
        if outbound and isinstance(outbound, str) and not outbound.startswith("/r/"):
            links.append(outbound)

        return SocialEntity(
            platform=Platform.REDDIT,
            platform_post_id=str(post_id),
            kind=kind,
            created_at=_as_float(data.get("created_utc")),
            text=str(body),
            title=data.get("title"),
            author_id=data.get("author_fullname"),
            author_name=author,
            community=data.get("subreddit"),
            conversation_id=conversation_id or str(post_id),
            reply_to_id=reply_to_id,
            depth=depth,
            canonical_url=canonical,
            engagement=Engagement(
                score=_as_int(data.get("score")),
                comment_count=_as_int(data.get("num_comments")),
            ),
            engagement_collected_at=now,
            external_links=tuple(links),
            provenance=Provenance(
                platform=Platform.REDDIT,
                route=CollectionRoute.OFFICIAL_API,
                source_url=canonical or f"{OAUTH_BASE}/comments/{post_id}",
                collected_at=now,
                adapter="reddit",
            ),
        )


def _children(listing: Any) -> list[dict[str, Any]]:
    if not isinstance(listing, dict):
        return []
    data = listing.get("data")
    if not isinstance(data, dict):
        return []
    children = data.get("children")
    return children if isinstance(children, list) else []


def _within(entity: SocialEntity, window: SearchWindow) -> bool:
    if entity.created_at is None:
        return True
    if window.since is not None and entity.created_at < window.since:
        return False
    return not (window.until is not None and entity.created_at > window.until)


def _retry_after(headers: dict[str, str], *, default: float) -> float:
    raw = headers.get("retry-after") or headers.get(HEADER_RESET)
    value = _as_float(raw)
    return value if value is not None and value > 0 else default


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def collect_incrementally(
    adapter: RedditAdapter,
    window: SearchWindow,
    *,
    checkpoint: Checkpoint | None = None,
    max_pages: int = 5,
) -> tuple[list[SocialEntity], Checkpoint, SocialAccounting]:
    """Walk pages until the window is exhausted or our own ceiling is hit.

    A 429 ends the walk rather than retrying through another route: the pages
    already collected are returned with the checkpoint that reaches the rest, so
    the next run continues from there instead of starting over.
    """

    collected: list[SocialEntity] = []
    discovered = resolved = unavailable = 0
    cursor = checkpoint

    for _ in range(max_pages):
        try:
            page, cursor, accounting = adapter.search(window, checkpoint=cursor)
        except RateLimited as exc:
            logger.info("reddit rate limited; deferring to the next run (%s)", exc)
            break
        discovered += accounting.discovered
        resolved += accounting.resolved
        unavailable += accounting.unavailable
        collected.extend(page)
        if not cursor.cursor or not page:
            break

    final = dedupe(collected)
    # Deduplication removes entities we did resolve; they are still accounted
    # for, as duplicates rather than as losses.
    duplicates = len(collected) - len(final)
    return (
        final,
        cursor or Checkpoint(),
        SocialAccounting(
            discovered=discovered,
            resolved=resolved - duplicates,
            partial=duplicates,
            unavailable=unavailable,
        ),
    )


__all__: Sequence[str] = [
    "DOCS_VERIFIED_AT",
    "RateLimitState",
    "RateLimited",
    "RedditAccessError",
    "RedditAdapter",
    "collect_incrementally",
]
