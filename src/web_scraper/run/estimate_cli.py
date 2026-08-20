"""``ws-run --estimate-cost``: price the paid work without doing any of it.

Assembling the fleet here rather than inside the estimator is deliberate. The
providers are built exactly as a real run would build them — same environment
variables, same zones, same strategy tables — so an estimate produced on a box
missing ``BRIGHTDATA_ZONE`` correctly shows that Bright Data is not available,
instead of quoting a price for a vendor that would fail on every call.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from web_scraper.budget import BudgetLedger
from web_scraper.contracts import Verdict
from web_scraper.finops.estimate import CostEstimate, UnresolvedUrl, estimate_run_cost
from web_scraper.providers.base import Provider
from web_scraper.providers.breaker import BreakerStore, ProviderBreakers
from web_scraper.providers.bright_data import BrightDataProvider
from web_scraper.providers.firecrawl import FirecrawlProvider
from web_scraper.providers.multi_router import MultiProviderRouter
from web_scraper.providers.scrape_do import ScrapeDoProvider
from web_scraper.providers.stats import ProviderStatsStore
from web_scraper.providers.zenrows import ZenRowsProvider
from web_scraper.providers.zyte import ZyteProvider
from web_scraper.queue.store import QueueStore

#: Verdicts that leave a URL as paid work. Anything else is either resolved or
#: something no provider can fix.
UNRESOLVED_VERDICTS = frozenset({Verdict.BLOCKED.value, Verdict.SOFT_BLOCK.value})


_PROVIDER_FACTORIES: dict[str, tuple[Callable[[], bool], Callable[[], Provider]]] = {
    "scrape.do": (lambda: bool(os.environ.get("SCRAPE_DO_TOKEN")), ScrapeDoProvider),
    "firecrawl": (lambda: bool(os.environ.get("FIRECRAWL_API_KEY")), FirecrawlProvider),
    "brightdata": (
        lambda: bool(os.environ.get("BRIGHTDATA_API_KEY") and os.environ.get("BRIGHTDATA_ZONE")),
        BrightDataProvider,
    ),
    "zenrows": (lambda: bool(os.environ.get("ZENROWS_API_KEY")), ZenRowsProvider),
    "zyte": (lambda: bool(os.environ.get("ZYTE_API_KEY")), ZyteProvider),
}


def configured_providers(allowed: Sequence[str] = ()) -> list[Provider]:
    """Build only explicitly allowed vendors, preserving operator order."""

    unknown = sorted(set(allowed) - set(_PROVIDER_FACTORIES))
    if unknown:
        raise ValueError(f"unknown paid providers: {', '.join(unknown)}")
    providers: list[Provider] = []
    for name in allowed:
        configured, factory = _PROVIDER_FACTORIES[name]
        if configured():
            providers.append(factory())
    return providers


def unresolved_from_queue(
    queue: QueueStore, *, profile_site: str, url_class_for: Any
) -> list[UnresolvedUrl]:
    """URLs the free layer could not resolve, with the verdict that stopped them."""

    from urllib.parse import urlsplit

    latest: dict[str, str] = {}
    for record in queue.attempts():
        verdict = str(record.get("verdict") or "")
        url = str(record.get("url") or "")
        if url:
            latest[url] = verdict

    out: list[UnresolvedUrl] = []
    for url, verdict in latest.items():
        if verdict not in UNRESOLVED_VERDICTS:
            continue
        url_class = url_class_for(url)
        out.append(
            UnresolvedUrl(
                url=url,
                domain=urlsplit(url).netloc or profile_site,
                url_class=url_class.name if url_class else "unknown",
                verdict=Verdict(verdict),
            )
        )
    return out


def build_estimate(
    unresolved: Sequence[UnresolvedUrl],
    *,
    state_dir: Path,
    daily_credit_limit: str | None,
    free_url_count: int = 0,
    providers: Sequence[Provider] | None = None,
    allowed_providers: Sequence[str] = (),
) -> CostEstimate:
    """Price the work using the same router the run would use."""

    fleet = list(providers) if providers is not None else configured_providers(allowed_providers)
    router = MultiProviderRouter(
        providers=fleet,
        stats=ProviderStatsStore(state_dir / "provider_stats.sqlite3"),
        breakers=ProviderBreakers(store=BreakerStore(state_dir / "breakers.sqlite3")),
        # Estimation must be deterministic: a shadow probe is a real decision the
        # run may take, but quoting a different price on each invocation would
        # make the estimate useless for approval.
        _rng=lambda: 1.0,
    )

    remaining: Decimal | None = None
    if daily_credit_limit is not None:
        ledger = BudgetLedger(state_dir / "budget.sqlite3", daily_credit_limit=daily_credit_limit)
        usage = ledger.usage()
        remaining = Decimal(daily_credit_limit) - usage.credits - ledger.held_credits()

    return estimate_run_cost(
        unresolved,
        router=router,
        free_url_count=free_url_count,
        budget_remaining=remaining,
    )
