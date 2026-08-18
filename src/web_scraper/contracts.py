"""Shared data contracts for the web-scraper core.

Every layer (triage, probe, profiles, fetchers, storage, reporting)
communicates through the types defined here. Scripts and adapters must
not invent their own verdicts, levels, or result shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Verdict(str, Enum):
    OK = "OK"
    DEAD_URL = "DEAD_URL"
    ORIGIN_DOWN = "ORIGIN_DOWN"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ACCESS_DENIED = "ACCESS_DENIED"
    BLOCKED = "BLOCKED"
    SOFT_BLOCK = "SOFT_BLOCK"
    THIN_CONTENT = "THIN_CONTENT"
    NOT_MODIFIED = "NOT_MODIFIED"  # 304 to a conditional request: unchanged, keep prior data
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PARSE_FAIL = "PARSE_FAIL"


#: Verdicts that may unlock the next *free* level (L1/L2 browser retry).
FREE_ESCALATION_VERDICTS = frozenset({Verdict.BLOCKED, Verdict.SOFT_BLOCK})

#: The only verdicts that may ever justify a *paid* (L3/L4) escalation.
#: A strict subset of the free set: reaching a browser is cheap, spending money
#: is not, so THIN_CONTENT and a bare-403 BLOCKED-by-status never pay on their own.
PAID_ESCALATION_VERDICTS = frozenset({Verdict.BLOCKED, Verdict.SOFT_BLOCK})


class Level(str, Enum):
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


class RouteType(str, Enum):
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

    def __post_init__(self) -> None:
        allowed = ROUTE_LEVELS[self.type]
        if self.level not in allowed:
            raise ValueError(
                f"route type {self.type.value!r} is not allowed at level "
                f"{self.level.value}; allowed: {sorted(l.value for l in allowed)}"
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
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Route":
        return cls(
            type=RouteType(data["type"]),
            level=Level(data["level"]),
            url=data.get("url"),
            mode=data.get("mode", "single"),
            provider=data.get("provider"),
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
    cost_credits: str = "0"
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
            "cost_credits": self.cost_credits,
            "request_id": self.request_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Attempt":
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
            cost_credits=data.get("cost_credits", "0"),
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
    def from_dict(cls, data: Mapping[str, Any]) -> "Result":
        return cls(
            url=data["url"],
            verdict=Verdict(data["verdict"]),
            attempts=tuple(Attempt.from_dict(item) for item in data.get("attempts", ())),
            data=data.get("data"),
            extractor_source=data.get("extractor_source"),
        )
