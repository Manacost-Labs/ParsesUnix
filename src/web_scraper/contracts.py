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


class FieldImportance(StrEnum):
    """How much a missing field matters. Three answers, not a number.

    A percentage of fields extracted is the metric everyone reaches for and it
    is useless: losing a description and losing the price are both "one field",
    and only one of them makes the dataset wrong. The severity has to be
    declared per field, by whoever knows what the data is for.

    ``CRITICAL``
        The record is not a record without it. Missing means FAIL.
    ``IMPORTANT``
        The record is usable but poorer. Missing means a warning, and enough of
        them means the profile is degrading.
    ``OPTIONAL``
        Nice to have. Missing is information, not a problem.
    """

    CRITICAL = "critical"
    IMPORTANT = "important"
    OPTIONAL = "optional"

    @property
    def blocks_certification(self) -> bool:
        return self is FieldImportance.CRITICAL


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


class ContentKind(StrEnum):
    """What a response actually *is*, as distinct from what it claims to be.

    The distinction earns its place because ``Content-Type`` is frequently
    wrong. Plenty of APIs answer JSON as ``text/plain``; plenty of error pages
    answer HTML with a JSON content type. Extracting with the wrong assumption
    does not raise — it silently returns nothing, which is the worst possible
    failure for a data pipeline.
    """

    HTML = "HTML"
    JSON = "JSON"
    TEXT = "TEXT"
    BINARY = "BINARY"
    UNKNOWN = "UNKNOWN"

    @property
    def is_extractable(self) -> bool:
        """Is there any point handing this to an extractor?"""

        return self in {ContentKind.HTML, ContentKind.JSON, ContentKind.TEXT}


class CostCertainty(StrEnum):
    """How well we know what a call cost.

    The three levels are not degrees of confidence — they are three different
    epistemic situations, and collapsing any pair of them loses money:

    ``EXACT``
        The provider reported the actual spend for this call. The number is
        theirs, not ours.
    ``PROVISIONAL``
        The provider did not report a per-call figure, but a **documented,
        conservative upper bound** exists — a published tariff we can defend.
        The recorded amount is that ceiling, so the true cost is at most this.
    ``UNKNOWN``
        No safe upper bound can be established at all. Spending stops.

    ``PROVISIONAL`` is the dangerous one, because it is the level that lets a
    batch keep running. It is legitimate *only* when a documented bound exists.
    Introducing it because a run would otherwise halt turns an honest "we do not
    know" into a fabricated number, which is the exact failure the ``UNKNOWN``
    level was created to prevent.
    """

    EXACT = "EXACT"
    PROVISIONAL = "PROVISIONAL"
    UNKNOWN = "UNKNOWN"

    @property
    def is_bounded(self) -> bool:
        """Can this cost be given a defensible upper bound?"""

        return self is not CostCertainty.UNKNOWN


@dataclass(frozen=True)
class Cost:
    """What something cost, in the provider's own unit and in canonical money.

    Four states that must never be collapsed into each other:

    - :meth:`free` — no paid call happened. A *measured* zero.
    - :meth:`of` — the provider reported a number. Known spend, ``EXACT``.
    - :meth:`provisional` — not reported, but bounded by a documented tariff.
    - :meth:`unknown` — a paid call happened and no safe bound exists.

    The last two exist because reporting unbounded spend as ``0`` understates
    the bill, and an understated bill is how a budget is quietly exceeded: every
    downstream total looks affordable while real money has already left.

    ``estimated_usd`` is the canonical unit. Provider credits are not a shared
    currency — one Scrape.do credit, one Firecrawl credit and one Bright Data
    request are three different things — so any comparison across vendors has to
    happen in money. It is ``None`` when no pricing snapshot covered this
    provider, which is itself worth surfacing rather than defaulting to zero.
    """

    credits: Decimal | None = Decimal("0")
    certainty: CostCertainty = CostCertainty.EXACT
    native_unit: str = "credits"
    estimated_usd: Decimal | None = None

    def __post_init__(self) -> None:
        # One fact, expressed once. An "exact unknown" would read as a known
        # zero to every caller that only looks at the amount.
        if (self.credits is None) is not (self.certainty is CostCertainty.UNKNOWN):
            raise ValueError("credits is None if and only if the certainty is UNKNOWN")

    # -- constructors ------------------------------------------------------

    @classmethod
    def free(cls) -> Cost:
        """No paid call. Zero is the truth here, not a placeholder."""

        return cls(credits=Decimal("0"), certainty=CostCertainty.EXACT, estimated_usd=Decimal("0"))

    @classmethod
    def of(cls, credits: Any, *, unit: str = "credits", usd: Decimal | None = None) -> Cost:
        """A reported cost. Anything unparseable is unknown, never zero."""

        try:
            return cls(
                credits=Decimal(str(credits)),
                certainty=CostCertainty.EXACT,
                native_unit=unit,
                estimated_usd=usd,
            )
        except (InvalidOperation, ValueError, TypeError):
            return cls.unknown(unit=unit)

    @classmethod
    def provisional(
        cls, upper_bound: Any, *, unit: str = "credits", usd: Decimal | None = None
    ) -> Cost:
        """A documented upper bound, used when the provider reports nothing.

        The caller must have an actual published tariff behind this. There is no
        way for this type to check that, which is why the rule is stated at every
        call site instead: a provisional cost without a documented bound is a
        guess wearing a number's clothes.
        """

        try:
            return cls(
                credits=Decimal(str(upper_bound)),
                certainty=CostCertainty.PROVISIONAL,
                native_unit=unit,
                estimated_usd=usd,
            )
        except (InvalidOperation, ValueError, TypeError):
            return cls.unknown(unit=unit)

    @classmethod
    def unknown(cls, *, unit: str = "credits") -> Cost:
        """Spend happened; no defensible bound exists. Never worth zero."""

        return cls(credits=None, certainty=CostCertainty.UNKNOWN, native_unit=unit)

    # -- queries -----------------------------------------------------------

    @property
    def attributed(self) -> bool:
        """Derived, not stored: two fields for one fact can disagree."""

        return self.certainty.is_bounded

    @property
    def is_known(self) -> bool:
        return self.attributed

    @property
    def is_exact(self) -> bool:
        return self.certainty is CostCertainty.EXACT

    @property
    def known_credits(self) -> Decimal:
        """For summing. Callers MUST also carry the unknown count alongside."""

        return self.credits if self.credits is not None else Decimal("0")

    @property
    def known_usd(self) -> Decimal:
        """Canonical money, or zero when no pricing snapshot covered it."""

        return self.estimated_usd if self.estimated_usd is not None else Decimal("0")

    def priced(self, usd: Decimal | None) -> Cost:
        """The same cost, with canonical money attached."""

        return Cost(
            credits=self.credits,
            certainty=self.certainty,
            native_unit=self.native_unit,
            estimated_usd=usd,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "credits": str(self.credits) if self.credits is not None else None,
            "certainty": self.certainty.value,
            "native_unit": self.native_unit,
            "estimated_usd": str(self.estimated_usd) if self.estimated_usd is not None else None,
            # Kept so existing consumers of the report keep working.
            "attributed": self.attributed,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Cost:
        if data is None:
            return cls.free()
        if isinstance(data, Mapping):
            certainty_raw = data.get("certainty")
            if certainty_raw is None:
                # Pre-certainty form: attributed True/False only.
                certainty = (
                    CostCertainty.EXACT if data.get("attributed", True) else CostCertainty.UNKNOWN
                )
            else:
                certainty = CostCertainty(certainty_raw)
            unit = str(data.get("native_unit", "credits"))
            usd_raw = data.get("estimated_usd")
            usd = Decimal(str(usd_raw)) if usd_raw is not None else None
            if certainty is CostCertainty.UNKNOWN:
                return cls.unknown(unit=unit)
            if certainty is CostCertainty.PROVISIONAL:
                return cls.provisional(data.get("credits", "0"), unit=unit, usd=usd)
            return cls.of(data.get("credits", "0"), unit=unit, usd=usd)
        # A bare scalar is the oldest form; treat it as reported.
        return cls.of(data)

    def __str__(self) -> str:
        if self.credits is None:
            return "unknown"
        if self.certainty is CostCertainty.PROVISIONAL:
            return f"<= {self.credits} {self.native_unit}"
        return f"{self.credits} {self.native_unit}"


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
