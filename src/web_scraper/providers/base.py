"""Vendor-neutral provider contract.

The core must never learn a vendor's response format. Adapters translate; the
gateway, the budget and triage see only these types.

Two separations carry the weight:

* **target status vs provider status.** A provider returning 200 while the site
  returned 404 is a dead URL, not a success; a provider returning 502 says
  nothing about the site at all. Conflating them is how a crawl quarantines live
  URLs and pays to retry dead ones.
* **actual cost vs assumed cost.** Cost comes from what the provider reported.
  When it reports nothing, the cost is *unattributed* — never zero, because
  silently free money is how a budget is overrun.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol


class ProviderErrorKind(StrEnum):
    """Why a provider call failed, separated from why a *target* failed."""

    AUTH = "AUTH"  # our credentials are wrong
    QUOTA = "QUOTA"  # we are out of credits or rate-limited by the provider
    TIMEOUT = "TIMEOUT"
    PROVIDER_FAULT = "PROVIDER_FAULT"  # 5xx from the provider itself
    BAD_REQUEST = "BAD_REQUEST"  # we built the call wrong
    TRANSPORT = "TRANSPORT"  # we never reached the provider


@dataclass(frozen=True)
class ProviderError(Exception):
    kind: ProviderErrorKind
    message: str
    provider: str
    status: int | None = None
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.provider}: {self.kind.value}: {self.message}"


@dataclass(frozen=True)
class ProviderCost:
    """What one call actually cost.

    ``attributed`` is False when the provider told us nothing. Such a call is
    still spend — it is reported, never counted as free.
    """

    credits: Decimal = Decimal("0")
    attributed: bool = True
    currency: str = "credits"
    remaining: Decimal | None = None

    @classmethod
    def unattributed(cls) -> ProviderCost:
        return cls(credits=Decimal("0"), attributed=False)

    @classmethod
    def parse(cls, raw: Any, *, remaining: Any = None) -> ProviderCost:
        try:
            credits = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError):
            return cls.unattributed()
        left: Decimal | None
        try:
            left = Decimal(str(remaining)) if remaining is not None else None
        except (InvalidOperation, ValueError, TypeError):
            left = None
        return cls(credits=credits, attributed=True, remaining=left)

    def to_dict(self) -> dict[str, Any]:
        return {
            "credits": str(self.credits),
            "attributed": self.attributed,
            "currency": self.currency,
            "remaining": str(self.remaining) if self.remaining is not None else None,
        }


@dataclass(frozen=True)
class ProviderStrategy:
    """One way a provider can be asked to fetch, with its price and powers."""

    id: str
    #: Typical cost, used only for *planning*. Billing always uses the reported cost.
    nominal_cost: Decimal
    #: What to HOLD before calling. A provider can bill more than the typical
    #: figure, and a hold of 1 against a charge of 10 breaches the limit while
    #: every individual check passed. Defaults to a margin over nominal.
    reservation_cost: Decimal | None = None
    renders_javascript: bool = False
    premium_network: bool = False
    description: str = ""

    @property
    def worst_case_cost(self) -> Decimal:
        """The amount to reserve. Never below the nominal cost."""

        if self.reservation_cost is not None:
            return max(self.reservation_cost, self.nominal_cost)
        # A conservative default: providers publish typical prices, and domain
        # profiles or plan tiers can push a single call above them.
        return (self.nominal_cost * Decimal("2")).quantize(Decimal("1"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "nominal_cost": str(self.nominal_cost),
            "reservation_cost": str(self.worst_case_cost),
            "renders_javascript": self.renders_javascript,
            "premium_network": self.premium_network,
            "description": self.description,
        }


@dataclass(frozen=True)
class ProviderRequest:
    url: str
    strategy_id: str
    timeout_seconds: float = 60.0
    #: Selector to wait for before capturing, when the strategy renders.
    wait_selector: str | None = None
    geo_code: str | None = None
    session_id: int | None = None


@dataclass(frozen=True)
class ProviderResponse:
    """One provider call, translated into terms the core understands."""

    provider: str
    strategy_id: str
    #: What the SITE returned. This is what triage judges with source="target".
    target_status: int | None
    #: What the PROVIDER returned. Provider health, never a verdict about the site.
    provider_status: int | None
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    final_url: str | None = None
    latency_ms: int | None = None
    cost: ProviderCost = field(default_factory=ProviderCost.unattributed)
    request_id: str | None = None
    #: Anti-bot vendor the provider reported seeing, when it says.
    detected_defense: str | None = None

    @property
    def provider_ok(self) -> bool:
        """Did the provider itself do its job? Says nothing about the content."""

        return self.provider_status is not None and 200 <= self.provider_status < 300

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "strategy_id": self.strategy_id,
            "target_status": self.target_status,
            "provider_status": self.provider_status,
            "final_url": self.final_url,
            "latency_ms": self.latency_ms,
            "cost": self.cost.to_dict(),
            "request_id": self.request_id,
            "detected_defense": self.detected_defense,
            "body_bytes": len(self.body),
        }


class Provider(Protocol):
    """What every adapter must offer the core."""

    name: str

    def strategies(self) -> tuple[ProviderStrategy, ...]: ...

    def fetch(self, request: ProviderRequest) -> ProviderResponse: ...
