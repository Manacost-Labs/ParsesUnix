"""Check the environment before a run, not during it.

A configuration mistake found at minute forty of a paid run has already cost
money and left a half-finished dataset. Every check here is cheap, read-only,
and answerable before the first fetch.

The distinction between a failure and a warning is deliberate. A *failure* means
the run cannot do what it was asked to do — no profile, no writable state, a
funded run with no provider credentials. A *warning* means it can run but an
operator should know something: pricing that has gone stale, a budget in an
incident state, no browser for a profile that needs one. Warnings do not stop a
run, because a free run on a machine without Chromium is still a useful run.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from web_scraper.providers.pricing import PricingBook
from web_scraper.run.config import RunConfig


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "error"  # "error" | "warning"

    @property
    def blocks(self) -> bool:
        return not self.ok and self.severity == "error"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "severity": self.severity}


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple[Check, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not any(check.blocks for check in self.checks)

    @property
    def warnings(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.ok and c.severity == "warning")

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.blocks)

    def explain(self) -> str:
        lines = [f"preflight: {'OK' if self.ok else 'FAILED'}"]
        for check in self.checks:
            mark = "ok  " if check.ok else ("WARN" if check.severity == "warning" else "FAIL")
            lines.append(f"  {mark} {check.name}: {check.detail}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failures": [c.to_dict() for c in self.failures],
            "warnings": [c.to_dict() for c in self.warnings],
            "checks": [c.to_dict() for c in self.checks],
            "explanation": self.explain(),
        }


def preflight(config: RunConfig, *, pricing: PricingBook | None = None) -> PreflightReport:
    """Everything worth knowing before the first fetch."""

    checks: list[Check] = [
        _profile(config),
        _state_dir(config),
        _browser(),
    ]
    checks.extend(_budget_checks(config))
    checks.extend(_pricing_checks(pricing or PricingBook()))
    checks.extend(_provider_cost_safety())
    checks.extend(_provider_readiness(pricing or PricingBook()))
    return PreflightReport(tuple(checks))


def _provider_readiness(pricing: PricingBook) -> list[Check]:
    """One line per configured vendor: can it be trusted with money tonight?

    Four facts, and the reason each is here rather than in a wiki:

    * **configured** — a key exists, so the router may pick it;
    * **live verified** — somebody has watched it answer a real call. Five out
      of five adapters contained a defect that only a live call exposed, so an
      unverified adapter is a hypothesis;
    * **cost bounded** — a call whose price cannot be bounded settles UNKNOWN
      and halts paid work after the first one;
    * **tariff fresh** — a stale price is not a price.
    """

    from web_scraper.providers import LIVE_VERIFIED_AT
    from web_scraper.run.estimate_cli import configured_providers

    checks: list[Check] = []
    stale = {snapshot.provider for snapshot in pricing.stale_snapshots()}
    for provider in configured_providers():
        name = provider.name
        verified = LIVE_VERIFIED_AT.get(name)
        priced = [
            s.id for s in provider.strategies() if pricing.upper_bound_usd(name, s.id) is not None
        ]
        unpriced = [s.id for s in provider.strategies() if s.id not in priced]
        problems = []
        if verified is None:
            problems.append("never verified against a live call")
        if unpriced:
            problems.append(f"unpriceable strategies: {', '.join(sorted(unpriced))}")
        if name in stale:
            problems.append("tariff is stale")
        checks.append(
            Check(
                f"provider_{name}",
                not problems,
                (
                    f"configured, live verified {verified}, {len(priced)} priced strategies"
                    if not problems
                    else "; ".join(problems)
                ),
                severity="warning",
            )
        )
    return checks


def _provider_cost_safety() -> list[Check]:
    """A funded provider whose cost cannot be bounded stops after one call.

    That is correct behaviour, and it is also a surprise worth having before the
    run rather than forty minutes into it: the operator sees "budget halted"
    with no obvious cause unless someone says this out loud first.
    """

    checks: list[Check] = []
    needs_bound = (
        ("brightdata", "BRIGHTDATA_API_KEY", ("BRIGHTDATA_CPM_USD",)),
        ("zenrows", "ZENROWS_API_KEY", ("ZENROWS_BASE_CPM_USD",)),
        ("zyte", "ZYTE_API_KEY", ("ZYTE_HTTP_MAX_USD", "ZYTE_BROWSER_MAX_USD")),
    )
    for provider, key_env, bound_envs in needs_bound:
        if not os.environ.get(key_env):
            continue
        missing = [name for name in bound_envs if not os.environ.get(name)]
        checks.append(
            Check(
                f"{provider}_cost_bound",
                not missing,
                "bounded"
                if not missing
                else (
                    f"{key_env} is set but {', '.join(missing)} is not; every call will "
                    "settle UNKNOWN and halt paid work after the first one"
                ),
                severity="warning",
            )
        )
    return checks


def _profile(config: RunConfig) -> Check:
    from web_scraper.profiles import load_profile
    from web_scraper.profiles.model import ProfileError

    if not config.profile_path.exists():
        return Check("profile", False, f"not found: {config.profile_path}")
    try:
        profile = load_profile(config.profile_path)
    except (ProfileError, OSError, ValueError) as exc:
        return Check("profile", False, f"invalid: {exc}")
    return Check("profile", True, f"{profile.site}, {len(profile.url_classes)} url class(es)")


def _state_dir(config: RunConfig) -> Check:
    path = config.state_dir
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".preflight"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check("state_dir", False, f"not writable: {exc}")
    free_mb = shutil.disk_usage(path).free // (1024 * 1024)
    if free_mb < 100:
        return Check(
            "state_dir",
            False,
            f"only {free_mb} MB free; SQLite writes will start failing mid-run",
            severity="warning",
        )
    return Check("state_dir", True, f"writable, {free_mb} MB free")


def _browser() -> Check:
    """Playwright is optional, so this check can never block.

    The severity is "warning" on both branches: it describes what this check
    would do if it failed, and a free run on a machine without Chromium is
    still a useful run.
    """

    try:
        import playwright  # noqa: F401
    except ImportError:
        return Check(
            "browser",
            False,
            "Playwright not installed; L2 routes will be skipped, CSR pages stay unresolved",
            severity="warning",
        )
    return Check("browser", True, "Playwright available", severity="warning")


def _budget_checks(config: RunConfig) -> list[Check]:
    if config.daily_credit_limit is None:
        return [Check("budget", True, "no limit configured; this is a free run")]

    from web_scraper.budget import BudgetLedger

    try:
        ledger = BudgetLedger(config.budget_path, daily_credit_limit=config.daily_credit_limit)
    except (OSError, ValueError) as exc:
        return [Check("budget", False, f"ledger unusable: {exc}")]

    checks = []
    state = ledger.state()
    checks.append(
        Check(
            "budget_state",
            state.allows_paid_work,
            f"{state.value}"
            + ("" if state.allows_paid_work else "; paid work is blocked until reconciled"),
            severity="warning",
        )
    )
    remaining = Decimal(config.daily_credit_limit) - ledger.usage().credits - ledger.held_credits()
    checks.append(
        Check("budget_remaining", remaining > 0, f"{remaining} credits", severity="warning")
    )

    # A funded run with no credentials will simply never escalate. Better to say
    # so now than to report poor coverage afterwards.
    from web_scraper.run.estimate_cli import configured_providers

    providers = configured_providers()
    checks.append(
        Check(
            "provider_credentials",
            bool(providers),
            ", ".join(p.name for p in providers)
            if providers
            else "a paid budget is configured but no provider credentials are set",
        )
    )
    return checks


def _pricing_checks(book: PricingBook) -> list[Check]:
    stale = book.stale_snapshots()
    if not stale:
        return [Check("pricing", True, "all tariff snapshots are within the freshness window")]
    return [
        Check(
            "pricing",
            False,
            "stale tariffs: "
            + ", ".join(f"{s.provider} ({s.age_days()}d)" for s in stale)
            + "; cost estimates may be wrong",
            severity="warning",
        )
    ]


def secret_leak_check(payload: str) -> list[str]:
    """Patterns that must never appear in a report, manifest or log line."""

    found = []
    for marker in ("Bearer ", "api_key=", "apikey=", "token=", "Authorization:", "Cookie:"):
        if marker.lower() in payload.lower():
            found.append(marker)
    for name in ("SCRAPE_DO_TOKEN", "FIRECRAWL_API_KEY", "BRIGHTDATA_API_KEY"):
        value = os.environ.get(name)
        if value and value in payload:
            found.append(f"value of {name}")
    return found
