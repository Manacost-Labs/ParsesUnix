"""CLI entry point for the static probe (and optional browser recon)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from web_scraper.probe.browser import BrowserUnavailable, browser_recon
from web_scraper.probe.safety import UnsafeTarget
from web_scraper.probe.static import MAX_BODY_BYTES, probe
from web_scraper.profiles.draft import draft_profile_from_probe


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safe, bounded static reconnaissance for a public web target."
    )
    parser.add_argument("url")
    parser.add_argument(
        "--allow-private",
        action="store_true",
        help="Allow internal/private targets only when the user explicitly authorized them.",
    )
    parser.add_argument("--max-body-bytes", type=int, default=MAX_BODY_BYTES)
    parser.add_argument("--no-robots", action="store_true", help="Skip the robots.txt lookup.")
    parser.add_argument(
        "--draft-profile",
        type=Path,
        help="Write a draft Site Profile (JSON) built from the probe report to this path.",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="After the static probe, run browser recon if the page looks CSR (requires Playwright).",
    )
    parser.add_argument(
        "--force-browser",
        action="store_true",
        help="Run browser recon even when the static probe does not recommend it.",
    )
    parser.add_argument(
        "--target-field",
        action="append",
        default=[],
        help="Field name to look for in intercepted JSON during browser recon (repeatable).",
    )
    args = parser.parse_args(argv)

    if args.max_body_bytes < 1:
        parser.error("--max-body-bytes must be positive")

    try:
        report = probe(
            args.url,
            allow_private=args.allow_private,
            max_body_bytes=args.max_body_bytes,
            include_robots=not args.no_robots,
        )
    except UnsafeTarget as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    output: dict[str, Any] = {"ok": True, "report": report.to_dict()}

    if args.browser or args.force_browser:
        target_fields = args.target_field or ["title"]
        try:
            recon = browser_recon(
                args.url,
                target_fields=target_fields,
                static_report=report,
                force=args.force_browser,
                allow_private=args.allow_private,
            )
            output["browser_recon"] = recon.to_dict()
        except BrowserUnavailable as exc:
            output["browser_recon"] = {"executed": False, "error": str(exc)}

    if args.draft_profile:
        draft = draft_profile_from_probe(report)
        args.draft_profile.write_text(
            json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        output["draft_profile_path"] = str(args.draft_profile)

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
