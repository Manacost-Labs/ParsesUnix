"""How a listing continues, and the bounds on following it."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs, urlsplit


class PaginationStrategy(StrEnum):
    NONE = "NONE"
    PAGE = "PAGE"  # ?page=2
    OFFSET = "OFFSET"  # ?offset=20&limit=20
    CURSOR = "CURSOR"  # ?cursor=abc / after=abc
    INFINITE_SCROLL = "INFINITE_SCROLL"  # more records appear as you scroll


class StopReason(StrEnum):
    """Why a traversal ended. Never absent: an unexplained stop is a defect."""

    COMPLETE = "COMPLETE"  # the listing said there is no more
    NO_NEW_RECORDS = "NO_NEW_RECORDS"  # a page added nothing we had not seen
    REPEATED_CURSOR = "REPEATED_CURSOR"  # the cursor cycled: a real pagination bug
    EXPECTED_COUNT_REACHED = "EXPECTED_COUNT_REACHED"
    MAX_PAGES = "MAX_PAGES"  # our ceiling, not the site's end
    MAX_RECORDS = "MAX_RECORDS"
    MAX_SCROLLS = "MAX_SCROLLS"
    TIME_BUDGET = "TIME_BUDGET"
    ERROR = "ERROR"

    @property
    def is_exhaustive(self) -> bool:
        """Did we see the whole listing, or stop because of our own limit?"""

        return self in {
            StopReason.COMPLETE,
            StopReason.NO_NEW_RECORDS,
            StopReason.EXPECTED_COUNT_REACHED,
        }


#: Query parameters that identify each strategy, in priority order.
_PAGE_KEYS = ("page", "p", "pagenum", "page_number")
_OFFSET_KEYS = ("offset", "start", "from", "skip")
_CURSOR_KEYS = ("cursor", "after", "next", "next_cursor", "continuation", "page_token")

_NEXT_LINK_RE = re.compile(
    r"<(?:a|link)\b[^>]*\brel=[\"'][^\"']*next[^\"']*[\"'][^>]*>", re.IGNORECASE
)

#: "next" as a class or link text — books.toscrape.com and most templates use
#: `<li class="next"><a href="page-2.html">` and never set rel="next".
_NEXT_MARKUP_RE = re.compile(
    r"class=[\"'][^\"']*\bnext\b[^\"']*[\"']|>\s*next\s*(?:&[a-z]+;|»|→)?\s*<", re.IGNORECASE
)

#: Pagination in the PATH rather than the query: /page/2/, /page-2.html, /p/2.
#: This is the most common shape on the web and a query-only detector misses it.
_PATH_PAGE_RE = re.compile(r"/(?:page|p)[-/](\d+)(?:\.[a-z]+)?/?$", re.IGNORECASE)
_SCROLL_MARKER_RE = re.compile(
    rb"infinite[-_ ]?scroll|data-infinite|IntersectionObserver|loadMore|load_more"
    rb"|\$\(window\)\.scroll|addEventListener\(\s*[\"']scroll",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TraversalBudget:
    """Ceilings on following a listing. Every one of them is a stop reason."""

    max_pages: int = 200
    max_records: int = 100_000
    max_scrolls: int = 30
    time_budget_seconds: float = 600.0
    #: Consecutive pages adding nothing new before we call it done.
    empty_streak: int = 2

    def __post_init__(self) -> None:
        if min(self.max_pages, self.max_records, self.max_scrolls, self.empty_streak) < 1:
            raise ValueError("traversal budget values must be >= 1")
        if self.time_budget_seconds <= 0:
            raise ValueError("time_budget_seconds must be positive")


@dataclass(frozen=True)
class PaginationPlan:
    """What was detected about how this listing continues."""

    strategy: PaginationStrategy
    parameter: str | None = None
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["strategy"] = self.strategy.value
        payload["evidence"] = list(self.evidence)
        return payload


def _query_key(url: str, candidates: Sequence[str]) -> str | None:
    query = parse_qs(urlsplit(url).query)
    lowered = {key.lower(): key for key in query}
    return next((lowered[name] for name in candidates if name in lowered), None)


def detect_strategy(
    *,
    url: str = "",
    body: bytes | None = None,
    json_payload: Mapping[str, Any] | None = None,
) -> PaginationPlan:
    """Infer how a listing continues from the URL, the payload and the markup.

    Order matters: an explicit cursor beats an offset beats a page number,
    because a site that offers a cursor means it to be used. Infinite scroll is
    inferred last — it is the least precise signal and the most expensive to act
    on.
    """

    evidence: list[str] = []

    if json_payload is not None:
        for key in _CURSOR_KEYS:
            if json_payload.get(key):
                return PaginationPlan(PaginationStrategy.CURSOR, key, (f"payload carries {key!r}",))
        for key in ("next", "next_page", "next_url"):
            if json_payload.get(key):
                return PaginationPlan(PaginationStrategy.CURSOR, key, (f"payload carries {key!r}",))

    if url:
        if match := _PATH_PAGE_RE.search(urlsplit(url).path):
            return PaginationPlan(
                PaginationStrategy.PAGE, None, (f"page number in path ({match.group(0)})",)
            )
        if cursor := _query_key(url, _CURSOR_KEYS):
            return PaginationPlan(PaginationStrategy.CURSOR, cursor, (f"query {cursor!r}",))
        if offset := _query_key(url, _OFFSET_KEYS):
            return PaginationPlan(PaginationStrategy.OFFSET, offset, (f"query {offset!r}",))
        if page := _query_key(url, _PAGE_KEYS):
            return PaginationPlan(PaginationStrategy.PAGE, page, (f"query {page!r}",))

    if body:
        text = body.decode("utf-8", errors="ignore")
        if _NEXT_LINK_RE.search(text):
            evidence.append("rel=next link")
            return PaginationPlan(PaginationStrategy.PAGE, None, tuple(evidence))
        if _SCROLL_MARKER_RE.search(body):
            evidence.append("infinite-scroll markers in markup")
            return PaginationPlan(PaginationStrategy.INFINITE_SCROLL, None, tuple(evidence))
        if _NEXT_MARKUP_RE.search(text):
            evidence.append("a 'next' control in the markup")
            return PaginationPlan(PaginationStrategy.PAGE, None, tuple(evidence))
    return PaginationPlan(PaginationStrategy.NONE, None, ("no pagination signal",))


@dataclass
class TraversalState:
    """Running state of one traversal, and the arbiter of when to stop.

    Deliberately a small state machine rather than a loop condition scattered
    through a crawler: the stop reason has to be a single, reportable value.
    """

    budget: TraversalBudget = field(default_factory=TraversalBudget)
    expected_count: int | None = None
    pages_visited: int = 0
    scrolls: int = 0
    elapsed_seconds: float = 0.0
    seen_keys: set[str] = field(default_factory=set)
    cursors_seen: set[str] = field(default_factory=set)
    empty_pages: int = 0
    stop_reason: StopReason | None = None

    @property
    def records(self) -> int:
        return len(self.seen_keys)

    def observe_page(self, keys: Sequence[str], *, cursor: str | None = None) -> StopReason | None:
        """Fold one page in; returns a stop reason once the traversal should end."""

        self.pages_visited += 1
        if cursor is not None:
            if cursor in self.cursors_seen:
                # The listing pointed back at a page we already fetched: this is
                # a pagination bug, not the end, and looping would never finish.
                self.stop_reason = StopReason.REPEATED_CURSOR
                return self.stop_reason
            self.cursors_seen.add(cursor)

        fresh = {key for key in keys if key not in self.seen_keys}
        self.seen_keys |= fresh
        self.empty_pages = 0 if fresh else self.empty_pages + 1

        if self.expected_count is not None and self.records >= self.expected_count:
            self.stop_reason = StopReason.EXPECTED_COUNT_REACHED
        elif self.empty_pages >= self.budget.empty_streak:
            self.stop_reason = StopReason.NO_NEW_RECORDS
        elif self.records >= self.budget.max_records:
            self.stop_reason = StopReason.MAX_RECORDS
        elif self.pages_visited >= self.budget.max_pages:
            self.stop_reason = StopReason.MAX_PAGES
        elif self.elapsed_seconds >= self.budget.time_budget_seconds:
            self.stop_reason = StopReason.TIME_BUDGET
        return self.stop_reason

    def observe_scroll(self, keys: Sequence[str]) -> StopReason | None:
        """One scroll step of an infinite listing."""

        self.scrolls += 1
        reason = self.observe_page(keys)
        if reason is None and self.scrolls >= self.budget.max_scrolls:
            self.stop_reason = StopReason.MAX_SCROLLS
        return self.stop_reason

    def finish(self, reason: StopReason = StopReason.COMPLETE) -> StopReason:
        if self.stop_reason is None:
            self.stop_reason = reason
        return self.stop_reason

    @property
    def complete(self) -> bool:
        """Did we see the whole listing?

        False when we stopped at our own ceiling, and false when an expected
        count was declared and not reached — a listing that promised 1247 rows
        and gave 300 is an incident, not a success.
        """

        if self.stop_reason is None or not self.stop_reason.is_exhaustive:
            return False
        if self.expected_count is not None:
            return self.records >= self.expected_count
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages_visited": self.pages_visited,
            "scrolls": self.scrolls,
            "records": self.records,
            "expected_count": self.expected_count,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "exhaustive": bool(self.stop_reason and self.stop_reason.is_exhaustive),
            "complete": self.complete,
        }
