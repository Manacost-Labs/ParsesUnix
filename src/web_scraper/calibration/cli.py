"""``ws-benchmark providers`` — calibrate the fleet, then explain the ranking.

Two defaults are deliberate and both are about money:

* **Planning, not calling.** Without ``--live`` nothing touches a network. The
  command prints the matrix it would run and the worst case it could cost, so
  the first thing an operator sees is the bill they are agreeing to.
* **Evidence stays in its own database.** A session writes to the calibration
  directory. Production statistics change only through ``promote``, after a
  preview, with ``--yes``.

The ranking is not computed here. It is asked of
:class:`~web_scraper.providers.multi_router.MultiProviderRouter` — the class the
run itself uses — so the recommendation in the report is a rehearsal of the real
decision rather than a second opinion about it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from web_scraper.calibration.caps import PROVIDER_CAP_ENV, SpendCaps
from web_scraper.calibration.corpora import EXAMPLE_CORPUS
from web_scraper.calibration.corpus import Corpus, load_corpus
from web_scraper.calibration.harness import CalibrationHarness
from web_scraper.calibration.promote import apply_promotion, plan_promotion
from web_scraper.calibration.report import CalibrationReport, session_id
from web_scraper.calibration.store import CalibrationStore
from web_scraper.contracts import Verdict
from web_scraper.providers.base import Provider
from web_scraper.providers.breaker import BreakerStore, ProviderBreakers
from web_scraper.providers.pricing import (
    BRIGHT_DATA_CPM_ENV,
    ZENROWS_CPM_ENV,
    ZYTE_BROWSER_MAX_ENV,
    ZYTE_CAPTURE_MAX_ENV,
    ZYTE_HTTP_MAX_ENV,
    PricingBook,
)
from web_scraper.providers.stats import ProviderStatsStore

DEFAULT_STATE_DIR = Path(".calibration")
DEFAULT_ARTIFACTS = Path("artifacts")


def _providers(names: Sequence[str]) -> list[Provider]:
    """The fleet this machine can actually call, filtered to what was asked."""

    from web_scraper.run.estimate_cli import configured_providers

    fleet = configured_providers()
    if not names:
        return fleet
    wanted = set(names)
    return [p for p in fleet if p.name in wanted]


def _caps(args: argparse.Namespace) -> SpendCaps:
    caps = SpendCaps.from_env(allow_unbounded=bool(args.allow_unbounded))
    if args.max_usd is not None:
        caps.total_usd = args.max_usd
    for provider, flag in _CAP_FLAGS.items():
        value = getattr(args, flag, None)
        if value is not None:
            caps.per_provider_usd[provider] = value
    return caps


_CAP_FLAGS = {provider: f"max_{provider.replace('.', '_')}_usd" for provider in PROVIDER_CAP_ENV}

#: What an operator would have to supply to make a vendor priceable. Printed
#: with the refusal, because "unpriceable" without the remedy reads as a bug in
#: the tool rather than a missing figure only the account holder has.
PRICING_ENV: dict[str, tuple[str, ...]] = {
    "brightdata": (BRIGHT_DATA_CPM_ENV,),
    "zenrows": (ZENROWS_CPM_ENV,),
    "zyte": (ZYTE_HTTP_MAX_ENV, ZYTE_BROWSER_MAX_ENV, ZYTE_CAPTURE_MAX_ENV),
}


def _decimal(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("a cap cannot be negative")
    return value


def _worst_case(harness: CalibrationHarness, pricing: PricingBook) -> dict[str, Any]:
    """What the planned matrix could cost if every call billed its ceiling."""

    per_provider: dict[str, Decimal] = {}
    unbounded: list[str] = []
    for call in harness.plan():
        if harness.skip_inapplicable and not call.applicable:
            continue
        bound = pricing.upper_bound_usd(call.provider, call.strategy.id)
        if bound is None:
            ref = call.ref
            if ref not in unbounded:
                unbounded.append(ref)
            continue
        per_provider[call.provider] = per_provider.get(call.provider, Decimal("0")) + bound
    return {
        "per_provider_usd": {k: str(v) for k, v in sorted(per_provider.items())},
        "total_usd": str(sum(per_provider.values(), Decimal("0"))),
        "unpriceable_strategies": unbounded,
    }


def _recommendations(
    args: argparse.Namespace, store: CalibrationStore, providers: Sequence[Provider]
) -> list[dict[str, Any]]:
    from web_scraper.calibration.metrics import recommendation

    out: list[dict[str, Any]] = []
    for spec in args.recommend or []:
        parts = spec.split(":")
        if len(parts) not in (2, 3):
            raise SystemExit(f"--recommend wants domain:url_class[:verdict], got {spec!r}")
        domain, url_class = parts[0], parts[1]
        verdict = Verdict(parts[2]) if len(parts) == 3 else Verdict.BLOCKED
        out.append(
            dict(
                recommendation(
                    store.stats,
                    providers=providers,
                    domain=domain,
                    url_class=url_class,
                    verdict=verdict,
                )
            )
        )
    return out


def _run_providers(args: argparse.Namespace) -> int:
    # No --corpus means the bundled one: the command has to be runnable, and
    # safe, without an operator first authoring a manifest.
    corpus: Corpus = load_corpus(args.corpus) if args.corpus else EXAMPLE_CORPUS
    providers = _providers(args.provider)
    if not providers:
        print(
            "no provider is configured on this machine — set the vendors' API keys "
            "and try again; nothing was called",
            file=sys.stderr,
        )
        return 2

    pricing = PricingBook()
    store = CalibrationStore(args.state_dir)
    caps = _caps(args)
    session = args.session or session_id()
    breakers = (
        ProviderBreakers(store=BreakerStore(Path(args.state_dir) / "breakers.sqlite3"))
        if args.use_breakers
        else None
    )

    harness = CalibrationHarness(
        corpus=corpus,
        providers=providers,
        caps=caps,
        store=store,
        session=session,
        pricing=pricing,
        breakers=breakers,
        skip_inapplicable=not args.include_inapplicable,
        early_stop_successes=args.early_stop,
        capture_discovery=not args.no_capture,
    )

    worst = _worst_case(harness, pricing)
    if not args.live:
        print(f"PLAN ONLY — nothing was called. Session would be {session}.\n")
        print(f"corpus:    {corpus.name} ({corpus.fingerprint})")
        print(f"targets:   {len(corpus.targets)} across {', '.join(corpus.domains)}")
        print(f"providers: {', '.join(p.name for p in providers)}")
        print(f"fairness:  {harness.fairness()}")
        print(f"\nworst case if every planned call bills its ceiling: ${worst['total_usd']}")
        for name, amount in worst["per_provider_usd"].items():
            print(f"  {name:<14} ${amount}")
        if worst["unpriceable_strategies"]:
            print(
                "\nunpriceable (no tariff ceiling; refused unless --allow-unbounded): "
                + ", ".join(worst["unpriceable_strategies"])
            )
            for provider in sorted({s.split(":")[0] for s in worst["unpriceable_strategies"]}):
                names = PRICING_ENV.get(provider)
                if names:
                    print(f"  price {provider} by setting {' or '.join(names)}")
        print(f"\ncaps: total ${caps.total_usd}, per provider {caps.per_provider_usd or '(none)'}")
        print("\nAdd --live to run it.")
        return 0

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    outcomes = harness.run()
    finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    report = CalibrationReport(
        session=session,
        corpus=corpus,
        outcomes=outcomes,
        pricing=pricing,
        caps=caps.to_dict(),
        fairness=harness.fairness(),
        live=True,
        minimum_confidence=args.min_confidence,
        recommendations=_recommendations(args, store, providers),
        started_at=started,
        finished_at=finished,
        notes=tuple(args.note or ()),
    )
    json_path, md_path = report.write(args.artifacts)
    print(report.describe())
    print(f"\nwrote {json_path}\nwrote {md_path}")
    print(
        "\nEvidence is in the calibration store only. "
        "Run `ws-benchmark promote` to review moving it into production statistics."
    )
    return 0


def _run_promote(args: argparse.Namespace) -> int:
    calibration = CalibrationStore(args.state_dir).stats
    production = ProviderStatsStore(args.production_stats)
    plan = plan_promotion(
        calibration,
        production,
        providers=tuple(args.provider or ()),
        domains=tuple(args.domain or ()),
        min_scored_attempts=args.min_scored,
    )
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
    else:
        print(plan.describe())
    if not args.yes:
        return 0
    if not plan.items:
        print("\nnothing to apply")
        return 0
    result = apply_promotion(plan, production)
    print(f"\npromoted {result['promoted_keys']} key(s) into {result['destination']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ws-benchmark", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    providers = sub.add_parser("providers", help="calibrate the provider fleet on one corpus")
    providers.add_argument("--corpus", default=None)
    providers.add_argument(
        "--live",
        action="store_true",
        help="actually call the providers. Without this nothing is spent.",
    )
    providers.add_argument("--provider", action="append", default=[])
    providers.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    providers.add_argument("--artifacts", default=str(DEFAULT_ARTIFACTS))
    providers.add_argument("--session", default=None)
    providers.add_argument("--max-usd", type=_decimal, default=None)
    for provider, flag in _CAP_FLAGS.items():
        providers.add_argument(
            f"--{flag.replace('_', '-')}", type=_decimal, default=None, help=f"cap for {provider}"
        )
    providers.add_argument(
        "--allow-unbounded",
        action="store_true",
        help="permit strategies whose cost cannot be bounded (operator-initiated only)",
    )
    providers.add_argument("--include-inapplicable", action="store_true")
    providers.add_argument("--no-capture", action="store_true")
    providers.add_argument("--use-breakers", action="store_true")
    providers.add_argument("--early-stop", type=int, default=3)
    providers.add_argument("--min-confidence", type=float, default=0.7)
    providers.add_argument("--recommend", action="append", default=[])
    providers.add_argument("--note", action="append", default=[])
    providers.set_defaults(func=_run_providers)

    promote = sub.add_parser("promote", help="review, and only then import, calibration evidence")
    promote.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    promote.add_argument("--production-stats", required=True)
    promote.add_argument("--provider", action="append", default=[])
    promote.add_argument("--domain", action="append", default=[])
    promote.add_argument("--min-scored", type=int, default=1)
    promote.add_argument("--json", action="store_true")
    promote.add_argument(
        "--yes", action="store_true", help="apply the plan. Without it nothing is written."
    )
    promote.set_defaults(func=_run_promote)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
