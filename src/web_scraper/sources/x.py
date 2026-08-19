"""X search without the API by default, and never around a login wall.

Two decisions define this adapter, and both are refusals.

**The API is off unless someone turns it on.** X's API is expensive and priced
per tier, so an adapter that reaches for it automatically can spend a lot of
money answering a question the free path could have answered. ``api.enabled``
defaults to ``False``; when it is off there is no code path to the API at all,
not merely a branch that is skipped.

**A login wall is an answer, not an obstacle.** When X returns a sign-in
interstitial, this adapter reports ``AUTH_REQUIRED`` and stops. It does not
supply cookies, replay a session, or try a different address. The content behind
that wall is not public, and the platform has said so; collecting it anyway
would be circumventing an access control regardless of how easy it is.

What remains is the ordinary route ladder every other target uses — a permitted
public page, fetched free if it can be, through the browser if it needs
rendering, through a paid provider only if triage says the origin refused us.
The adapter's job is to know what an X search result *means*, because a generic
scraper pointed at a search page cannot tell a post from a quote from a repost,
and dedupes on the wrong thing.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from web_scraper.contracts import Verdict
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

PUBLIC_SEARCH = "https://x.com/search"

#: Verdicts that mean "this is not ours to collect". Distinct from a block: a
#: block is the origin refusing our traffic, an auth wall is the platform
#: stating that the content requires an account.
NOT_PUBLIC_VERDICTS = frozenset({Verdict.AUTH_REQUIRED, Verdict.ACCESS_DENIED})

#: A post id in a canonical URL: /<handle>/status/<id>.
_STATUS_RE = re.compile(r"/(?P<handle>[^/]+)/status/(?P<id>\d+)")


class LoginWallEncountered(RuntimeError):
    """X asked for an account. That ends the collection for this URL."""


@dataclass(frozen=True)
class XApiConfig:
    """Off by default. Enabling it is a spending decision, so it is explicit."""

    enabled: bool = False
    bearer_env: str = "X_BEARER_TOKEN"

    def token(self) -> str | None:
        if not self.enabled:
            return None
        import os

        return os.environ.get(self.bearer_env) or None

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "bearer_env": self.bearer_env}


@dataclass(frozen=True)
class XSearchResult:
    entities: tuple[SocialEntity, ...]
    checkpoint: Checkpoint
    accounting: SocialAccounting
    #: Set when the platform required an account. The run continues; this query
    #: does not.
    login_wall: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "entities": [e.to_dict() for e in self.entities],
            "checkpoint": self.checkpoint.to_dict(),
            "accounting": self.accounting.to_dict(),
            "login_wall": self.login_wall,
        }


@dataclass
class XSearchAdapter:
    """Search semantics for X, over whatever route the gateway provides.

    ``fetch`` is the ordinary gateway callable. The adapter does not own a
    transport: it builds the search URL, interprets what comes back, and
    normalises it. That keeps every route decision — free, browser, paid — in
    the one place that already knows how to make it.
    """

    fetch: Callable[[str], Any]
    api: XApiConfig = field(default_factory=XApiConfig)
    clock: Callable[[], float] = time.time
    max_links_per_entity: int = 3

    # -- query building ----------------------------------------------------

    def search_url(self, window: SearchWindow) -> str:
        """X's own search grammar, so the platform does the filtering.

        Expressing the time window in the query rather than filtering after the
        fetch matters: a page of results we then discard was still a page we
        paid to render.
        """

        parts = [window.query]
        if window.community:
            parts.append(f"from:{window.community}")
        if window.since is not None:
            parts.append(f"since:{_as_date(window.since)}")
        if window.until is not None:
            parts.append(f"until:{_as_date(window.until)}")
        query = " ".join(parts)
        tab = "live" if window.sort == "new" else "top"
        return f"{PUBLIC_SEARCH}?q={quote(query)}&f={tab}"

    # -- collection --------------------------------------------------------

    def search(
        self, window: SearchWindow, *, checkpoint: Checkpoint | None = None
    ) -> XSearchResult:
        if self.api.enabled:
            # Deliberately not implemented rather than silently falling back:
            # an operator who turned the API on wants the API, and quietly
            # using the public path instead would answer a different question
            # than the one they paid to ask.
            raise NotImplementedError(
                "the X API path is not implemented; disable api.enabled to use the "
                "public route, or implement the API adapter against current pricing"
            )

        url = self.search_url(window)
        outcome = self.fetch(url)
        verdict = outcome.result.verdict

        if verdict in NOT_PUBLIC_VERDICTS:
            # Not an obstacle to route around. The platform has stated that this
            # content requires an account.
            logger.info("x search hit a login wall for %r; stopping this query", window.query)
            return XSearchResult(
                entities=(),
                checkpoint=checkpoint or Checkpoint(),
                accounting=SocialAccounting(discovered=0),
                login_wall=True,
            )

        if verdict is not Verdict.OK or outcome.response is None:
            return XSearchResult(
                entities=(),
                checkpoint=checkpoint or Checkpoint(),
                accounting=SocialAccounting(discovered=0, unavailable=0),
            )

        entities = self.parse(outcome.response.body, source_url=url, provenance_of=outcome)
        deduped = dedupe(entities)
        newest = max((e.created_at or 0.0) for e in deduped) if deduped else None
        return XSearchResult(
            entities=tuple(deduped),
            checkpoint=Checkpoint(
                latest_seen_id=deduped[0].platform_post_id if deduped else None,
                latest_seen_at=newest or (checkpoint.latest_seen_at if checkpoint else None),
            ),
            accounting=SocialAccounting(
                discovered=len(entities),
                resolved=len(deduped),
                partial=len(entities) - len(deduped),
            ),
        )

    # -- parsing -----------------------------------------------------------

    def parse(
        self, body: bytes, *, source_url: str, provenance_of: Any = None
    ) -> list[SocialEntity]:
        """Turn a rendered search page into typed entities.

        Embedded JSON is preferred over the DOM wherever it exists: markup is
        restyled constantly while the data shape behind it moves far less, and a
        selector-based parser silently returns nothing the week the class names
        change.
        """

        text = body.decode("utf-8", errors="replace")
        entities = self._from_embedded_json(text, source_url, provenance_of)
        if entities:
            return entities
        return self._from_links(text, source_url, provenance_of)

    def _from_embedded_json(self, text: str, source_url: str, outcome: Any) -> list[SocialEntity]:
        out: list[SocialEntity] = []
        for match in re.finditer(r'\{"rest_id":"(\d+)".{0,4000}?"full_text":"(.*?)"', text):
            post_id, raw_text = match.group(1), match.group(2)
            out.append(
                self._entity(
                    post_id=post_id,
                    text=_unescape(raw_text),
                    kind=EntityKind.POST,
                    source_url=source_url,
                    outcome=outcome,
                )
            )
        return out

    def _from_links(self, text: str, source_url: str, outcome: Any) -> list[SocialEntity]:
        """Fallback: canonical status links, which survive most redesigns."""

        seen: set[str] = set()
        out: list[SocialEntity] = []
        for match in _STATUS_RE.finditer(text):
            post_id = match.group("id")
            if post_id in seen:
                continue
            seen.add(post_id)
            out.append(
                self._entity(
                    post_id=post_id,
                    text="",
                    kind=EntityKind.POST,
                    source_url=source_url,
                    outcome=outcome,
                    author_name=match.group("handle"),
                )
            )
        return out

    def _entity(
        self,
        *,
        post_id: str,
        text: str,
        kind: EntityKind,
        source_url: str,
        outcome: Any,
        author_name: str | None = None,
        conversation_id: str | None = None,
        reply_to_id: str | None = None,
    ) -> SocialEntity:
        now = self.clock()
        paid = getattr(outcome, "paid", None) if outcome is not None else None
        route = CollectionRoute.PROVIDER if paid and paid.attempted else CollectionRoute.PUBLIC_PAGE
        return SocialEntity(
            platform=Platform.X,
            platform_post_id=post_id,
            kind=kind,
            created_at=None,  # not stated on the search page; never invented
            text=text,
            author_name=author_name,
            conversation_id=conversation_id or post_id,
            reply_to_id=reply_to_id,
            canonical_url=f"https://x.com/{author_name or 'i'}/status/{post_id}",
            engagement=Engagement(),
            engagement_collected_at=now,
            external_links=tuple(_external_links(text)[: self.max_links_per_entity]),
            provenance=Provenance(
                platform=Platform.X,
                route=route,
                source_url=source_url,
                collected_at=now,
                provider=getattr(paid, "provider", None) if paid else None,
                strategy_id=getattr(paid, "strategy_id", None) if paid else None,
                adapter="x",
            ),
        )

    # -- conversations -----------------------------------------------------

    def conversation(self, post_url: str) -> Thread:
        """Whatever of a conversation is publicly reachable, honestly bounded.

        X shows a fraction of most conversations without an account. The result
        records what was retrieved and states that the rest was not, rather than
        presenting a fragment as the whole discussion.
        """

        outcome = self.fetch(post_url)
        verdict = outcome.result.verdict
        match = _STATUS_RE.search(urlsplit(post_url).path)
        root_id = match.group("id") if match else post_url

        if verdict in NOT_PUBLIC_VERDICTS or outcome.response is None:
            root = self._entity(
                post_id=root_id,
                text="",
                kind=EntityKind.POST,
                source_url=post_url,
                outcome=outcome,
            )
            return Thread(
                root=root,
                entities=(root,),
                completeness=ThreadCompleteness(
                    entities_loaded=1,
                    unresolved_branches=("conversation requires an account",),
                ),
            )

        entities = self.parse(outcome.response.body, source_url=post_url, provenance_of=outcome)
        entities = dedupe(entities)
        root = next(
            (e for e in entities if e.platform_post_id == root_id),
            self._entity(
                post_id=root_id,
                text="",
                kind=EntityKind.POST,
                source_url=post_url,
                outcome=outcome,
            ),
        )
        replies = tuple(
            _as_reply(e, conversation_id=root.platform_post_id, parent=root.platform_post_id)
            for e in entities
            if e.platform_post_id != root.platform_post_id
        )
        return Thread(
            root=root,
            entities=(root, *replies),
            completeness=ThreadCompleteness(
                entities_loaded=len(replies) + 1,
                # A public page never states how many replies exist, so we can
                # never claim to have them all.
                unresolved_branches=("public view shows a subset of replies",),
            ),
        )


def _as_reply(entity: SocialEntity, *, conversation_id: str, parent: str) -> SocialEntity:
    from dataclasses import replace

    return replace(
        entity,
        kind=EntityKind.REPLY,
        conversation_id=conversation_id,
        reply_to_id=parent,
        depth=1,
    )


def _external_links(text: str) -> list[str]:
    return re.findall(r"https?://(?!x\.com|twitter\.com)[^\s\"'<>]+", text)


def _unescape(raw: str) -> str:
    try:
        decoded = json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw
    return decoded if isinstance(decoded, str) else raw


def _as_date(timestamp: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(timestamp, tz=dt.UTC).date().isoformat()


__all__: Sequence[str] = [
    "LoginWallEncountered",
    "XApiConfig",
    "XSearchAdapter",
    "XSearchResult",
]
