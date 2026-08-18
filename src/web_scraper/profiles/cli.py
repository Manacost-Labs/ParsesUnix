"""CLI entry point for validating and drafting Site Profiles."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from web_scraper.profiles.draft import draft_profile_from_probe
from web_scraper.profiles.model import ProfileError, load_profile


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate or draft a Site Profile without touching the network."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate a profile file (YAML or JSON)."
    )
    validate_parser.add_argument("profile", type=Path)

    draft_parser = subparsers.add_parser("draft", help="Draft a profile from a saved probe report.")
    draft_parser.add_argument("--probe-report", type=Path, required=True)
    draft_parser.add_argument("--url-class", default="page")
    draft_parser.add_argument("--required-field", action="append", default=[])
    draft_parser.add_argument("--out", type=Path)

    args = parser.parse_args(argv)

    if args.command == "validate":
        try:
            profile = load_profile(args.profile)
        except ProfileError as exc:
            print(json.dumps({"ok": False, "errors": exc.errors}, ensure_ascii=False, indent=2))
            return 2
        except (OSError, ValueError) as exc:
            print(
                json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False), file=sys.stderr
            )
            return 2
        print(
            json.dumps(
                {"ok": True, "site": profile.site, "url_classes": sorted(profile.url_classes)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    raw = json.loads(args.probe_report.read_text(encoding="utf-8"))
    report = raw.get("report", raw)  # accept both the CLI envelope and a bare report
    draft = draft_profile_from_probe(
        report,
        url_class=args.url_class,
        required_fields=tuple(args.required_field) or ("title",),
    )
    rendered = json.dumps(draft, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.write_text(rendered, encoding="utf-8")
        print(json.dumps({"ok": True, "draft_profile_path": str(args.out)}, ensure_ascii=False))
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
