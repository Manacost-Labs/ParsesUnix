"""CLI for the run loop: ``ws-run <run-config.json>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from web_scraper.observability.alerts import LoggingAlerter
from web_scraper.profiles.model import ProfileError
from web_scraper.run.config import RunConfig
from web_scraper.run.runner import Runner


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a scheduled free-core (L0-L2) crawl.")
    parser.add_argument("config", type=Path, help="Path to a run-config JSON file.")
    parser.add_argument("--full-review", action="store_true", help="Ignore freshness intervals.")
    parser.add_argument("--report", type=Path, help="Write the run report JSON to this path.")
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

    outcome = runner.run()
    payload = {"ok": True, "processed": outcome.processed, "report": outcome.report}
    if args.report:
        args.report.write_text(json.dumps(payload["report"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
