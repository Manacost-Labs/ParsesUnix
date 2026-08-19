"""CLI for the run loop: ``ws-run <run-config.json>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from web_scraper.observability.alerts import LoggingAlerter
from web_scraper.profiles.model import ProfileError
from web_scraper.run.config import RunConfig
from web_scraper.run.runner import Runner


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a scheduled free-core (L0-L2) crawl.")
    parser.add_argument("config", type=Path, help="Path to a run-config JSON file.")
    parser.add_argument("--full-review", action="store_true", help="Ignore freshness intervals.")
    parser.add_argument("--report", type=Path, help="Write the run report JSON to this path.")
    parser.add_argument(
        "--estimate-cost",
        action="store_true",
        help="Report what the paid work would cost and exit. Makes no paid calls.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        config = RunConfig.from_file(args.config)
    except (OSError, KeyError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"bad run config: {exc}"}), file=sys.stderr)
        return 2
    if args.full_review:
        config = RunConfig(**{**config.__dict__, "full_review": True})

    try:
        runner = Runner(config, alerter=LoggingAlerter())
    except (ProfileError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2

    if args.estimate_cost:
        return _estimate(config, runner)

    outcome = runner.run()
    payload = {"ok": True, "processed": outcome.processed, "report": outcome.report}
    if args.report:
        args.report.write_text(
            json.dumps(payload["report"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _estimate(config: RunConfig, runner: Runner) -> int:
    """Answer "what would this cost?" without spending anything."""

    from web_scraper.run.estimate_cli import build_estimate, unresolved_from_queue

    unresolved = unresolved_from_queue(
        runner.queue,
        profile_site=runner.profile.site,
        url_class_for=runner.profile.class_for_url,
    )
    counts = runner.queue.counts_by_status()
    estimate = build_estimate(
        unresolved,
        state_dir=config.state_dir,
        daily_credit_limit=config.daily_credit_limit,
        free_url_count=sum(counts.values()),
    )
    print(json.dumps({"ok": True, "estimate": estimate.to_dict()}, ensure_ascii=False, indent=2))
    # A run that cannot afford its own holds should not be started by a cron job
    # that only checks the exit status.
    return 0 if estimate.fits_budget is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
