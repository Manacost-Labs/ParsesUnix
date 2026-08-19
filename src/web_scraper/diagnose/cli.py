"""CLI for failure diagnosis: ``ws-diagnose``.

Reads a run's queue (or a JSON list of attempts) and prints the failure
breakdown with a policy-correct remedy per group.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from web_scraper.diagnose.analyze import Diagnosis, diagnose_attempts, diagnose_queue
from web_scraper.queue import QueueStore


def _compare_routes(args: argparse.Namespace) -> int:
    """Answer "should this class move off the browser?" from stored evidence.

    Reads only. A comparison that fetched would be measuring today's network
    rather than the evidence the runs actually accumulated.
    """

    from web_scraper.diagnose.routes import compare_routes, describe_comparisons
    from web_scraper.discovery import DiscoveryStore

    state_dir = args.state_dir or (args.queue.parent if args.queue else None)
    if state_dir is None:
        print(
            json.dumps({"ok": False, "error": "--routes needs --state-dir"}),
            file=sys.stderr,
        )
        return 2

    path = Path(state_dir) / "discovery.sqlite3"
    if not path.exists():
        print(
            json.dumps(
                {
                    "ok": True,
                    "comparisons": [],
                    "note": (
                        "no discovery evidence yet; it accumulates across runs that render pages"
                    ),
                }
            )
        )
        return 0

    store = DiscoveryStore(path)
    comparisons = compare_routes(store.all_evidence())
    if args.json:
        print(
            json.dumps(
                {"ok": True, "comparisons": [c.to_dict() for c in comparisons]},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(describe_comparisons(comparisons))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--queue", type=Path, help="Path to a run's queue.sqlite3.")
    source.add_argument("--state-dir", type=Path, help="Run state dir (uses its queue.sqlite3).")
    source.add_argument("--attempts-json", type=Path, help="JSON list of attempt records.")

    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument(
        "--fingerprints",
        type=Path,
        help="Fingerprint database to report known failure shapes from.",
    )
    parser.add_argument("--samples", type=int, default=3, help="Sample URLs per group.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--routes",
        action="store_true",
        help=(
            "Compare the current route against validated API candidates discovered "
            "in previous runs. Reads stored evidence; makes no fetches."
        ),
    )
    args = parser.parse_args(argv)

    if args.routes:
        return _compare_routes(args)

    try:
        if args.attempts_json is not None:
            records = json.loads(args.attempts_json.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                raise ValueError("attempts JSON must be a list of attempt objects")
            diagnosis = diagnose_attempts(records, sample_urls=args.samples)
        else:
            path = args.queue or (args.state_dir / "queue.sqlite3")
            if not path.exists():
                raise FileNotFoundError(f"queue database not found: {path}")
            diagnosis = diagnose_queue(QueueStore(path), limit=args.limit, sample_urls=args.samples)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps({"ok": True, "diagnosis": diagnosis.to_dict()}, ensure_ascii=False, indent=2)
        )
    else:
        _print_text(diagnosis)
    return 0


def _print_text(diagnosis: Diagnosis) -> None:
    print(f"Attempts: {diagnosis.total_attempts}   failures: {diagnosis.failures}")
    print(f"{diagnosis.headline}\n")
    if not diagnosis.groups:
        return
    print("Failure breakdown")
    for group in diagnosis.groups:
        flag = "paid OK" if group.may_escalate_to_paid else "NO paid"
        level = f" @{group.level}" if group.level else ""
        print(f"  {group.share:>5.0%}  {group.verdict}{level}  ({group.count})  [{flag}]")
        print(f"         cause:  {group.root_cause}")
        print(f"         action: {group.remedy}")
        for url in group.sample_urls:
            print(f"         e.g. {url}")
    if diagnosis.by_domain:
        print("\nFailures by domain")
        for domain, count in sorted(diagnosis.by_domain.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>5}  {domain}")


if __name__ == "__main__":
    raise SystemExit(main())
