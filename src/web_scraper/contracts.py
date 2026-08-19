"""Shared data contracts for the web-scraper core.

Every layer (triage, probe, profiles, fetchers, storage, reporting)
communicates through the types defined here. Scripts and adapters must
not invent their own verdicts, levels, or result shapes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class Verdict(StrEnum):
    OK = "OK"
    DEAD_URL = "DEAD_URL"
    ORIGIN_DOWN = "ORIGIN_DOWN"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ACCESS_DENIED = "ACCESS_DENIED"
    BLOCKED = "BLOCKED"
    SOFT_BLOCK = "SOFT_BLOCK"
    THIN_CONTENT = "THIN_CONTENT"
    #: HTTP 200 carrying a client-rendered shell: the markup arrived, the data
    #: did not. Rendering is the answer, so this unlocks the browser — but it is
    #: not evidence of blocking and must never justify paying a provider.
    CSR_REQUIRED = "CSR_REQUIRED"
    NOT_MODIFIED = "NOT_MODIFIED"  # 304 to a conditional request: unchanged, keep prior data
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PARSE_FAIL = "PARSE_FAIL"


#: Verdicts that may unlock the next *free* level (L1/L2 browser retry).
FREE_ESCALATION_VERDICTS = frozenset({Verdict.BLOCKED, Verdict.SOFT_BLOCK, Verdict.CSR_REQUIRED})

#: The only verdicts that may ever justify a *paid* (L3/L4) escalation.
#: A strict subset of the free set: reaching a browser is cheap, spending money is
#: not. A client-rendered page is not a blocked page — it needs rendering, which
#: we can do ourselves — so CSR_REQUIRED is deliberately absent here.
PAID_ESCALATION_VERDICTS = frozenset({Verdict.BLOCKED, Verdict.SOFT_BLOCK})


class Level(StrEnum):
    """Fetching levels ordered from cheapest to most expensive."""

    L0 = "L0"  # machine-readable routes: JSON API, RSS/Atom, sitemap
    L1 = "L1"  # direct HTTP (stdlib or Scrapling session)
    L2 = "L2"  # local browser: dynamic or stealthy fetcher
    L3 = "L3"  # paid provider: scrape.do / Firecrawl
    L4 = "L4"  # paid unblocker: Bright Data

    @property
    def rank(self) -> int:
        return int(self.value[1])

    @property
    def is_paid(self) -> bool:
        return self in (Level.L3, Level.L4)


class RouteType(StrEnum):
    JSON_API = "json_api"
    RSS = "rss"
    SITEMAP = "sitemap"
    DIRECT_HTTP = "direct_http"
    DYNAMIC = "dynamic"
    STEALTHY = "stealthy"
    PROVIDER = "provider"


#: Levels at which each route type is allowed to run.
ROUTE_LEVELS: Mapping[RouteType, frozenset[Level]] = {
    RouteType.JSON_API: frozenset({Level.L0}),
    RouteType.RSS: frozenset({Level.L0}),
    RouteType.SITEMAP: frozenset({Level.L0}),
    RouteType.DIRECT_HTTP: frozenset({Level.L1}),
    RouteType.DYNAMIC: frozenset({Level.L2}),
    RouteType.STEALTHY: frozenset({Level.L2}),
    RouteType.PROVIDER: frozenset({Level.L3, Level.L4}),
}

ROUTE_MODES = ("single", "bulk")


@dataclass(frozen=True)
class Route:
    """A concrete way to fetch a URL class at a specific level."""

    type: RouteType
    level: Level
    url: str | None = None  # concrete URL or template with {placeholders}
    mode: str = "single"
    provider: str | None = None  # required for RouteType.PROVIDER only
    #: Stable identity for statistics. A profile may declare one so a route keeps
    #: its history across a URL change; otherwise it is derived (see ``route_id``).
    id: str | None = None

    @property
    def route_id(self) -> str:
        """The key under which this route's history is remembered.

        Type and level alone are not an identity: a class can declare two JSON
        APIs at L0, and merging their statistics would tell the router that one
        endpoint is half-broken instead of that one works and one is dead.

        A declared ``id`` wins, so renaming or re-pointing an endpoint keeps its
        history. Otherwise a route without a URL derives to its bare type, which
        is both readable and backward compatible with statistics recorded before
        identities existed.
        """

        if self.id:
            return self.id
        if not self.url:
            return self.type.value
        digest = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:8]
        return f"{self.type.value}:{digest}"

    def __post_init__(self) -> None:
        allowed = ROUTE_LEVELS[self.type]
        if self.level not in allowed:
            raise ValueError(
                f"route type {self.type.value!r} is not allowed at level "
                f"{self.level.value}; allowed: {sorted(level.value for level in allowed)}"
            )
        if self.mode not in ROUTE_MODES:
            raise ValueError(f"route mode must be one of {ROUTE_MODES}, got {self.mode!r}")
        if self.type is RouteType.PROVIDER and not self.provider:
            raise ValueError("provider routes must name the provider")
        if self.type is not RouteType.PROVIDER and self.provider:
            raise ValueError("only provider routes may carry a provider name")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "level": self.level.value,
            "url": self.url,
            "mode": self.mode,
            "provider": self.provider,
            "id": self.id,
            "route_id": self.route_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Route:
        return cls(
            type=RouteType(data["type"]),
            level=Level(data["level"]),
            url=data.get("url"),
            mode=data.get("mode", "single"),
            provider=data.get("provider"),
            id=data.get("id"),
        )


@dataclass(frozen=True)
class ContentRules:
    """Validation rules applied to a response body during triage."""

    min_body_bytes: int = 200
    canary: str | None = None
    canaries: tuple[str, ...] = ()
    expected_content_type: str | None = None
    required_json_paths: tuple[str, ...] = ()
    stop_signatures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.min_body_bytes < 0:
            raise ValueError("min_body_bytes must be non-negative")

    @property
    def all_canaries(self) -> tuple[str, ...]:
        merged = tuple(c for c in (self.canary,) if c) + tuple(self.canaries)
        return merged


@dataclass(frozen=True)
class TriageResult:
    verdict: Verdict
    reason: str
    status: int | None
    body_bytes: int
    block_signature: str | None = None

    @property
    def paid_escalation_allowed(self) -> bool:
        return self.verdict in PAID_ESCALATION_VERDICTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "status": self.status,
            "body_bytes": self.body_bytes,
            "block_signature": self.block_signature,
            "paid_escalation_allowed": self.paid_escalation_allowed,
        }


@dataclass(frozen=True)
class Cost:
    """What something cost, including the case where nobody told us.

    Three states that must never be collapsed into each other:

    - :meth:`free` - no paid call happened. A *measured* zero.
    - :meth:`of` - the provider reported a number. Known spend.
    - :meth:`unknown` - a paid call happened and its cost is NOT known.

    The third state is the reason this type exists. Reporting unknown spend as
    ``0`` understates the bill, and an understated bill is how a budget is
    quietly exceeded: every downstream total would look affordable while real
    money had already left. Unknown stays unknown all the way to the report.
    """

    credits: Decimal | None = Decimal("0")
    attributed: bool = True

    def __post_init__(self) -> None:
        # The two fields are one fact expressed twice; disagreement would let a
        # caller construct an "attributed unknown" that reads as zero.
        if (self.credits is None) is self.attributed:
            raise ValueError("credits is None if and only if the cost is unattributed")

    @classmethod
    def free(cls) -> Cost:
        """No paid call. Zero is the truth here, not a placeholder."""

        return cls(credits=Decimal("0"), attributed=True)

    @classmethod
    def of(cls, credits: Any) -> Cost:
        """A reported cost. Anything unparseable is unknown, never zero."""

        try:
            return cls(credits=Decimal(str(credits)), attributed=True)
        except (InvalidOperation, ValueError, TypeError):
            return cls.unknown()

    @classmethod
    def unknown(cls) -> Cost:
        """Spend happened; the amount is not known. Never worth zero."""

        return cls(credits=None, attributed=False)

    @property
    def is_known(self) -> bool:
        return self.attributed

    @property
    def known_credits(self) -> Decimal:
        """For summing. Callers MUST also carry the unknown count alongside."""

        return self.credits if self.credits is not None else Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "credits": str(self.credits) if self.credits is not None else None,
            "attributed": self.attributed,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Cost:
        if data is None:
            return cls.free()
        if isinstance(data, Mapping):
            if not data.get("attributed", True):
                return cls.unknown()
            return cls.of(data.get("credits", "0"))
        # A bare scalar is the pre-structured form; treat it as reported.
        return cls.of(data)

    def __str__(self) -> str:
        return "unknown" if self.credits is None else str(self.credits)


@dataclass(frozen=True)
class Attempt:
    """One fetch attempt against one URL through one route."""

    url: str
    level: Level
    verdict: Verdict
    reason: str
    route: Route | None = None
    status: int | None = None
    body_bytes: int = 0
    elapsed_ms: int | None = None
    provider: str | None = None
    cost: Cost = field(default_factory=Cost.free)
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "level": self.level.value,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "route": self.route.to_dict() if self.route else None,
            "status": self.status,
            "body_bytes": self.body_bytes,
            "elapsed_ms": self.elapsed_ms,
            "provider": self.provider,
            "cost": self.cost.to_dict(),
            "request_id": self.request_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Attempt:
        route = data.get("route")
        return cls(
            url=data["url"],
            level=Level(data["level"]),
            verdict=Verdict(data["verdict"]),
            reason=data["reason"],
            route=Route.from_dict(route) if route else None,
            status=data.get("status"),
            body_bytes=data.get("body_bytes", 0),
            elapsed_ms=data.get("elapsed_ms"),
            provider=data.get("provider"),
            cost=Cost.from_dict(data.get("cost", data.get("cost_credits"))),
            request_id=data.get("request_id"),
        )


@dataclass(frozen=True)
class Result:
    """Final outcome for one URL after all attempts."""

    url: str
    verdict: Verdict
    attempts: tuple[Attempt, ...] = ()
    data: Mapping[str, Any] | None = None
    extractor_source: str | None = None  # json_ld | app_state | meta | css | xpath | heuristic

    @property
    def resolved(self) -> bool:
        return self.verdict is Verdict.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "verdict": self.verdict.value,
            "resolved": self.resolved,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "data": dict(self.data) if self.data is not None else None,
            "extractor_source": self.extractor_source,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Result:
        return cls(
            url=data["url"],
            verdict=Verdict(data["verdict"]),
            attempts=tuple(Attempt.from_dict(item) for item in data.get("attempts", ())),
            data=data.get("data"),
            extractor_source=data.get("extractor_source"),
        )
