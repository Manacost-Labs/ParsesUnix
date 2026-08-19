"""Provider tariffs, versioned, and the conversion into canonical money.

One Scrape.do credit, one Firecrawl credit and one Bright Data request are three
different things. Ranking them against each other in "credits" is a category
error that happens to produce a number, and the number decides where money goes.
Everything comparable therefore goes through USD.

Every rate here is **configuration, not measurement**. Vendors publish list
prices per plan and change them without telling us, so each snapshot carries the
date its source was read and the source itself. A snapshot older than the
staleness window is reported rather than silently trusted: a pricing assumption
that can no longer be defended is exactly the kind of thing that turns into an
unexplained invoice.

The distinction that carries the most weight:

``deterministic``
    The vendor documents what one call of this strategy costs, and there is no
    undocumented multiplier that could raise it. Such a call may settle as
    :class:`~web_scraper.contracts.CostCertainty.PROVISIONAL` when the response
    itself reports nothing — we have a defensible ceiling.

not ``deterministic``
    Pricing depends on factors the vendor does not publish per call — premium
    domains, plan tiers, undocumented feature multipliers. No safe ceiling
    exists, so an unreported cost is ``UNKNOWN`` and spending stops.

Marking a strategy deterministic to keep a batch running is the one change in
this file that can cost real money without any test failing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from web_scraper.contracts import Cost, CostCertainty

#: Beyond this, a tariff is treated as stale and reported.
DEFAULT_PRICING_STALENESS_DAYS = 90


@dataclass(frozen=True)
class StrategyRate:
    """What one call of one strategy costs, and whether that is defensible."""

    #: Native units the vendor bills for a single call at list price.
    native_per_call: Decimal
    #: Price of one native unit in USD.
    usd_per_native_unit: Decimal
    #: True when the vendor documents this figure with no undocumented
    #: multiplier that could exceed it. Only these may settle as PROVISIONAL.
    deterministic: bool = False
    #: Native units to assume when the vendor may charge more than list price.
    #: Never below ``native_per_call``; used for holds and for provisional
    #: settlement, so it must be the WORST case, not the typical one.
    native_upper_bound: Decimal | None = None
    note: str = ""

    @property
    def upper_bound(self) -> Decimal:
        if self.native_upper_bound is None:
            return self.native_per_call
        return max(self.native_upper_bound, self.native_per_call)

    def usd(self, native_amount: Decimal) -> Decimal:
        return (native_amount * self.usd_per_native_unit).quantize(Decimal("0.000001"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "native_per_call": str(self.native_per_call),
            "native_upper_bound": str(self.upper_bound),
            "usd_per_native_unit": str(self.usd_per_native_unit),
            "deterministic": self.deterministic,
            "note": self.note,
        }


@dataclass(frozen=True)
class PricingSnapshot:
    """One vendor's tariff as read on one date."""

    provider: str
    native_unit: str
    pricing_source: str
    docs_verified_at: str
    effective_at: str
    currency: str = "USD"
    rates: Mapping[str, StrategyRate] = field(default_factory=dict)
    version: str = "1"

    def rate_for(self, strategy_id: str) -> StrategyRate | None:
        return self.rates.get(strategy_id)

    def age_days(self, *, today: dt.date | None = None) -> int:
        now = today or dt.datetime.now(tz=dt.UTC).date()
        try:
            verified = dt.date.fromisoformat(self.docs_verified_at)
        except ValueError:  # pragma: no cover - guarded by the tests below
            return 10**6
        return (now - verified).days

    def is_stale(
        self, *, max_age_days: int = DEFAULT_PRICING_STALENESS_DAYS, today: dt.date | None = None
    ) -> bool:
        return self.age_days(today=today) > max_age_days

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "version": self.version,
            "native_unit": self.native_unit,
            "currency": self.currency,
            "pricing_source": self.pricing_source,
            "docs_verified_at": self.docs_verified_at,
            "effective_at": self.effective_at,
            "age_days": self.age_days(),
            "rates": {k: v.to_dict() for k, v in sorted(self.rates.items())},
        }


# ---------------------------------------------------------------------------
# The snapshots themselves. Every figure is a list price read from the vendor's
# published documentation on the stated date, converted at the stated rate.
# ---------------------------------------------------------------------------

#: Scrape.do: costs were MEASURED against the live API, not read from a page.
#: The USD rate is the published starter-plan list price and is configuration.
SCRAPE_DO = PricingSnapshot(
    provider="scrape.do",
    native_unit="credits",
    pricing_source="live measurement of Scrape.do-Request-Cost + published plan pricing",
    docs_verified_at="2026-08-19",
    effective_at="2026-08-19",
    rates={
        # Measured: normal=1, render=5, super=10, super_render=15.
        # Deterministic because the response reports the actual cost every time,
        # so a provisional bound is never needed for this vendor.
        "normal": StrategyRate(Decimal("1"), Decimal("0.00029"), deterministic=True),
        "render": StrategyRate(Decimal("5"), Decimal("0.00029"), deterministic=True),
        "super": StrategyRate(Decimal("10"), Decimal("0.00029"), deterministic=True),
        "super_render": StrategyRate(Decimal("15"), Decimal("0.00029"), deterministic=True),
    },
)

#: Firecrawl: MEASURED against the live API on 2026-08-19 on an easy target
#: (books.toscrape.com). Every mode reported exactly 1 credit, including
#: ``enhanced``, which served the request from the "stealth" pool. Cost arrives
#: in the response BODY at ``data.metadata.creditsUsed``, never in a header.
#:
#: The measurement does NOT generalise. One easy page says nothing about what
#: the stealth pool charges on a target that actually fights back, and the
#: vendor publishes no per-mode table. ``auto`` and ``enhanced`` therefore stay
#: non-deterministic: their reported cost is used when present, and an
#: unreported one is UNKNOWN rather than assumed to be 1.
FIRECRAWL = PricingSnapshot(
    provider="firecrawl",
    native_unit="credits",
    pricing_source="docs.firecrawl.dev/api-reference/endpoint/scrape + firecrawl.dev/pricing",
    docs_verified_at="2026-08-19",
    effective_at="2026-08-19",
    rates={
        "basic": StrategyRate(
            Decimal("1"),
            Decimal("0.00083"),
            deterministic=True,
            note="measured 1 credit on 2026-08-19; documented 1 credit/page",
        ),
        "cached": StrategyRate(
            Decimal("1"),
            Decimal("0.00083"),
            deterministic=True,
            note="measured 1 credit on a cache hit; ~2x faster than a live fetch",
        ),
        "auto": StrategyRate(
            Decimal("1"),
            Decimal("0.00083"),
            deterministic=False,
            native_upper_bound=Decimal("2"),
            note=(
                "measured 1 credit on an easy target, where it did not escalate; "
                "the escalation multiplier is not published"
            ),
        ),
        "enhanced": StrategyRate(
            Decimal("1"),
            Decimal("0.00083"),
            deterministic=False,
            native_upper_bound=Decimal("2"),
            note=(
                "measured 1 credit on an easy target via the stealth pool; what a "
                "hard target costs is not published and was not measured"
            ),
        ),
    },
)

#: Bright Data: billed CPM — per 1000 successful requests — so the native unit
#: is a request, not a credit.
#:
#: MEASURED 2026-08-19 with a live zone: the response carries no cost figure of
#: any kind. Without a rate this adapter therefore settles every call as
#: UNKNOWN, which correctly halts paid work — and correctly makes the provider
#: unusable, because one call per run is not a provider.
#:
#: The way out is not to weaken the rule. It is for the OPERATOR to supply the
#: CPM from their own account, which is a documented figure they can read off
#: their pricing page. ``BRIGHTDATA_CPM_USD`` does that, and it should be the
#: PREMIUM rate wherever premium domains are enabled: the bound has to be the
#: worst case, not the typical one.
BRIGHT_DATA = PricingSnapshot(
    provider="brightdata",
    native_unit="requests",
    pricing_source="docs.brightdata.com web-unlocker features + published CPM pricing",
    docs_verified_at="2026-08-19",
    effective_at="2026-08-19",
    rates={
        "unlocker": StrategyRate(
            Decimal("1"),
            Decimal("0.0015"),
            deterministic=False,
            note="CPM billing; premium domains cost more with no published multiplier",
        ),
        "unlocker_render": StrategyRate(
            Decimal("1"),
            Decimal("0.0015"),
            deterministic=False,
            note="as above; rendering is not separately priced in the docs",
        ),
        "browser": StrategyRate(
            Decimal("1"),
            Decimal("0.003"),
            deterministic=False,
            note="Browser API is a distinct product with its own tariff",
        ),
    },
)

#: ZenRows publishes credit multipliers but the USD value of a credit depends on
#: the plan. The operator supplies the CPM for BASIC requests; the documented
#: multipliers do the rest.
ZENROWS_CPM_ENV = "ZENROWS_BASE_CPM_USD"

#: Zyte's price depends on the website tier, the mode and the features, and none
#: of that is knowable from a request. Treating the cheapest published figure as
#: a bound would understate the bill on exactly the hard domains where the paid
#: layer gets used, so each mode takes its own operator-supplied ceiling.
ZYTE_HTTP_MAX_ENV = "ZYTE_HTTP_MAX_USD"
ZYTE_BROWSER_MAX_ENV = "ZYTE_BROWSER_MAX_USD"
ZYTE_CAPTURE_MAX_ENV = "ZYTE_CAPTURE_MAX_USD"


def zenrows_snapshot(base_cpm_usd: Decimal | str | None) -> PricingSnapshot:
    """ZenRows priced from documented multipliers and the operator's plan rate.

    The multipliers — 1x, 5x, 10x, 25x — are published and were read from the
    live documentation, so they are deterministic: an unreported cost can be
    bounded. What is NOT published is what a credit costs on a given plan, which
    is why the base rate comes from the operator.

    ``auto`` is the exception. The vendor decides which mode to use, so its
    nominal is the cheapest it might pick and its bound is the dearest. Marking
    it deterministic would let an unreported cost settle at the optimistic
    figure, which is the wrong direction to be wrong in.
    """

    documented = "docs.zenrows.com pricing: 1x basic, 5x js, 10x premium, 25x both"
    if base_cpm_usd is None:
        return PricingSnapshot(
            provider="zenrows",
            native_unit="credits",
            pricing_source=f"{documented}; no plan rate supplied",
            docs_verified_at="2026-08-19",
            effective_at="2026-08-19",
            rates={
                # Multipliers are known; the money is not. A reported cost is
                # used when it arrives, and an unreported one is UNKNOWN.
                "basic": StrategyRate(Decimal("1"), Decimal("0"), deterministic=False),
                "js": StrategyRate(Decimal("5"), Decimal("0"), deterministic=False),
                "premium": StrategyRate(Decimal("10"), Decimal("0"), deterministic=False),
                "js_premium": StrategyRate(Decimal("25"), Decimal("0"), deterministic=False),
                "auto": StrategyRate(
                    Decimal("1"),
                    Decimal("0"),
                    deterministic=False,
                    native_upper_bound=Decimal("25"),
                ),
            },
        )

    try:
        cpm = Decimal(str(base_cpm_usd))
    except (InvalidOperation, ValueError, TypeError):
        return zenrows_snapshot(None)
    if cpm <= 0:
        return zenrows_snapshot(None)

    per_credit = (cpm / Decimal("1000")).quantize(Decimal("0.00000001"))
    note = f"operator plan rate {cpm} USD/1000 basic requests"
    return PricingSnapshot(
        provider="zenrows",
        native_unit="credits",
        pricing_source=f"{documented}; {ZENROWS_CPM_ENV} supplied",
        docs_verified_at="2026-08-19",
        effective_at="2026-08-19",
        version="2",
        rates={
            "basic": StrategyRate(Decimal("1"), per_credit, deterministic=True, note=note),
            "js": StrategyRate(Decimal("5"), per_credit, deterministic=True, note=note),
            "premium": StrategyRate(Decimal("10"), per_credit, deterministic=True, note=note),
            "js_premium": StrategyRate(Decimal("25"), per_credit, deterministic=True, note=note),
            "auto": StrategyRate(
                Decimal("1"),
                per_credit,
                # The vendor picks the mode, so the cost is not ours to predict.
                # A reported cost settles it; an unreported one is UNKNOWN
                # rather than optimistically 1x.
                deterministic=False,
                native_upper_bound=Decimal("25"),
                note=f"{note}; vendor selects the mode, so 1x-25x is the range",
            ),
        },
    )


def zyte_snapshot(
    http_max: Decimal | str | None,
    browser_max: Decimal | str | None = None,
    capture_max: Decimal | str | None = None,
) -> PricingSnapshot:
    """Zyte priced from ceilings the operator reads off their own account.

    There is deliberately no default. Zyte's rate varies by website tier, and a
    figure that is safe on an easy domain is an understatement on a hard one —
    which is where the paid layer actually gets used. A missing ceiling means
    UNKNOWN and a halt, which is the correct answer to "we cannot bound this".
    """

    def rate(ceiling: Decimal | str | None) -> StrategyRate:
        if ceiling is None:
            return StrategyRate(Decimal("1"), Decimal("0"), deterministic=False)
        try:
            usd = Decimal(str(ceiling))
        except (InvalidOperation, ValueError, TypeError):
            return StrategyRate(Decimal("1"), Decimal("0"), deterministic=False)
        if usd <= 0:
            return StrategyRate(Decimal("1"), Decimal("0"), deterministic=False)
        return StrategyRate(
            Decimal("1"),
            usd,
            deterministic=True,
            note=f"operator ceiling {usd} USD/request",
        )

    return PricingSnapshot(
        provider="zyte",
        native_unit="requests",
        pricing_source="operator-supplied per-request ceilings; Zyte prices by website tier",
        docs_verified_at="2026-08-19",
        effective_at="2026-08-19",
        rates={
            "http": rate(http_max),
            "browser": rate(browser_max if browser_max is not None else http_max),
            "browser_capture": rate(capture_max if capture_max is not None else browser_max),
        },
    )


#: Environment variable carrying the operator's own Bright Data CPM, in USD per
#: 1000 successful requests. Absent means we cannot bound a call and every one
#: settles UNKNOWN.
BRIGHT_DATA_CPM_ENV = "BRIGHTDATA_CPM_USD"


def bright_data_snapshot(cpm_usd: Decimal | str | None) -> PricingSnapshot:
    """Bright Data priced from the operator's own tariff.

    With a CPM the per-request cost is ``cpm / 1000`` and the rates become
    *deterministic*: an unreported cost can be settled provisionally at that
    bound, because the operator read the figure off their own account rather
    than us guessing it.

    Without one, nothing changes: every call is UNKNOWN and spending stops.
    That is the honest default, not a broken one.
    """

    if cpm_usd is None:
        return BRIGHT_DATA
    try:
        cpm = Decimal(str(cpm_usd))
    except (InvalidOperation, ValueError, TypeError):
        return BRIGHT_DATA
    if cpm <= 0:
        return BRIGHT_DATA

    per_request = (cpm / Decimal("1000")).quantize(Decimal("0.000001"))
    note = f"operator-supplied CPM {cpm} USD/1000 successful requests"
    return PricingSnapshot(
        provider="brightdata",
        native_unit="requests",
        pricing_source=f"{BRIGHT_DATA_CPM_ENV} set by the operator",
        docs_verified_at=BRIGHT_DATA.docs_verified_at,
        effective_at=BRIGHT_DATA.effective_at,
        version="2",
        rates={
            "unlocker": StrategyRate(Decimal("1"), per_request, deterministic=True, note=note),
            "unlocker_render": StrategyRate(
                Decimal("1"), per_request, deterministic=True, note=note
            ),
            # The Browser API is a distinct product on a distinct tariff, so one
            # CPM does not cover it. It stays unbounded until priced separately.
            "browser": StrategyRate(
                Decimal("1"),
                per_request * Decimal("2"),
                deterministic=False,
                note="Browser API is a separate product; the unlocker CPM does not price it",
            ),
        },
    )


DEFAULT_SNAPSHOTS: tuple[PricingSnapshot, ...] = (SCRAPE_DO, FIRECRAWL, BRIGHT_DATA)


class PricingBook:
    """Every tariff in one place, with the conversions the router needs."""

    def __init__(self, snapshots: tuple[PricingSnapshot, ...] | None = None) -> None:
        if snapshots is None:
            # Bright Data is priced from the operator's own account when they
            # have told us the rate, and left unbounded when they have not.
            import os

            snapshots = (
                SCRAPE_DO,
                FIRECRAWL,
                bright_data_snapshot(os.environ.get(BRIGHT_DATA_CPM_ENV)),
                zenrows_snapshot(os.environ.get(ZENROWS_CPM_ENV)),
                zyte_snapshot(
                    os.environ.get(ZYTE_HTTP_MAX_ENV),
                    os.environ.get(ZYTE_BROWSER_MAX_ENV),
                    os.environ.get(ZYTE_CAPTURE_MAX_ENV),
                ),
            )
        self._by_provider = {snapshot.provider: snapshot for snapshot in snapshots}

    def snapshot(self, provider: str) -> PricingSnapshot | None:
        return self._by_provider.get(provider)

    def rate(self, provider: str, strategy_id: str) -> StrategyRate | None:
        snapshot = self._by_provider.get(provider)
        return snapshot.rate_for(strategy_id) if snapshot else None

    def expected_usd(self, provider: str, strategy_id: str) -> Decimal | None:
        """List price of one call in canonical money. ``None`` when unpriced."""

        rate = self.rate(provider, strategy_id)
        return None if rate is None else rate.usd(rate.native_per_call)

    def upper_bound_usd(self, provider: str, strategy_id: str) -> Decimal | None:
        """The most one call can cost, in canonical money."""

        rate = self.rate(provider, strategy_id)
        return None if rate is None else rate.usd(rate.upper_bound)

    def settle(self, provider: str, strategy_id: str, reported: Decimal | None) -> Cost:
        """Turn what the provider said — or did not say — into a Cost.

        This is the single place the PROVISIONAL rule is applied, so the
        condition can be read in one sitting: a provider that reports nothing
        gets a provisional ceiling only when its tariff is documented and
        deterministic. Otherwise the answer is UNKNOWN and spending stops.
        """

        rate = self.rate(provider, strategy_id)
        unit = (
            self._by_provider[provider].native_unit if provider in self._by_provider else "credits"
        )

        if reported is not None:
            usd = rate.usd(reported) if rate else None
            return Cost.of(reported, unit=unit, usd=usd)

        if rate is not None and rate.deterministic:
            bound = rate.upper_bound
            return Cost.provisional(bound, unit=unit, usd=rate.usd(bound))

        return Cost.unknown(unit=unit)

    def price(self, provider: str, strategy_id: str, cost: Cost) -> Cost:
        """Attach canonical money to a cost that already has its native amount."""

        rate = self.rate(provider, strategy_id)
        if rate is None or cost.credits is None:
            return cost
        return cost.priced(rate.usd(cost.credits))

    def stale_snapshots(
        self, *, max_age_days: int = DEFAULT_PRICING_STALENESS_DAYS, today: dt.date | None = None
    ) -> list[PricingSnapshot]:
        return [
            snapshot
            for snapshot in self._by_provider.values()
            if snapshot.is_stale(max_age_days=max_age_days, today=today)
        ]

    def detect_drift(self, provider: str, strategy_id: str, actual_native: Decimal) -> str | None:
        """Did a call cost more than its documented ceiling?

        A vendor changing its pricing is invisible at runtime; it shows up as
        costs that stop adding up. Comparing every settlement against the bound
        we planned with turns that into an alert on the first occurrence.
        """

        rate = self.rate(provider, strategy_id)
        if rate is None:
            return None
        if actual_native > rate.upper_bound:
            return (
                f"{provider}:{strategy_id} billed {actual_native} {self._by_provider[provider].native_unit}, "
                f"above the documented ceiling of {rate.upper_bound} — the tariff may have changed"
            )
        return None

    def to_dict(self) -> dict[str, Any]:
        return {name: snapshot.to_dict() for name, snapshot in sorted(self._by_provider.items())}


def certainty_of(book: PricingBook, provider: str, strategy_id: str) -> CostCertainty:
    """What the best achievable certainty is for this strategy, before calling."""

    rate = book.rate(provider, strategy_id)
    if rate is None:
        return CostCertainty.UNKNOWN
    return CostCertainty.PROVISIONAL if rate.deterministic else CostCertainty.UNKNOWN
