"""CLI for regression detection: ``ws-regress``.

Three modes:

* ``--fixture DIR``      compare one saved fixture against the live page;
* ``--fixtures-dir DIR`` do that for every fixture in a directory (batch);
* ``--baseline F --current F --url U``  fully offline comparison of two bodies.

Exit code is 1 when a *critical* regression is found, so it can gate CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from web_scraper.contracts import ContentRules
from web_scraper.fetchers.transports import UrllibTransport
from web_scraper.profiles.model import ProfileError, UrlClass, load_profile
from web_scraper.regression.detect import (
    SEVERITY_CRITICAL,
    RegressionReport,
    compare_bodies,
    compare_saved_to_current,
)
from web_scraper.storage.fixtures import (
    SavedResponse,
    iter_saved_responses,
    load_saved_response,
)


def _class_for(profile_path: Path | None, url: str, url_class: str | None) -> UrlClass | None:
    if profile_path is None:
        return None
    profile = load_profile(profile_path)
    if url_class:
        selected = profile.url_classes.get(url_class)
        if selected is None:
            raise KeyError(f"url class {url_class!r} not in profile {profile.site!r}")
        return selected
    return profile.class_for_url(url)


def _extraction_args(url_class: UrlClass | None) -> tuple[list[dict[str, Any]], list[str]]:
    if url_class is None:
        return [], []
    fields = list(url_class.quorum_fields or url_class.required_fields)
    return [dict(extractor) for extractor in url_class.extractors], fields


def _compare_live(
    saved: SavedResponse, url_class: UrlClass | None, timeout: float
) -> RegressionReport:
    transport = UrllibTransport(timeout=timeout)
    current = transport.fetch(saved.url)
    extractors, fields = _extraction_args(url_class)
    return compare_saved_to_current(
        saved,
        current_body=current.body,
        current_status=current.status,
        current_headers=current.headers,
        extractors=extractors,
        fields=fields,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", type=Path, help="One saved-fixture directory.")
    source.add_argument("--fixtures-dir", type=Path, help="Directory of saved fixtures.")
    source.add_argument("--baseline", type=Path, help="Baseline body file (offline mode).")

    parser.add_argument("--current", type=Path, help="Current body file (offline mode).")
    parser.add_argument("--url", help="URL the bodies belong to (offline mode).")
    parser.add_argument("--content-type", default="text/html", help="Offline mode content type.")
    parser.add_argument("--profile", type=Path, help="Site Profile supplying extractors/fields.")
    parser.add_argument("--url-class", help="Force a specific URL class from the profile.")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args(argv)

    reports: list[RegressionReport] = []
    try:
        if args.baseline is not None:
            if not args.current or not args.url:
                parser.error("--baseline requires --current and --url")
            url_class = _class_for(args.profile, args.url, args.url_class)
            extractors, fields = _extraction_args(url_class)
            headers = {"Content-Type": args.content_type}
            reports.append(
                compare_bodies(
                    url=args.url,
                    baseline_body=args.baseline.read_bytes(),
                    current_body=args.current.read_bytes(),
                    baseline_headers=headers,
                    current_headers=headers,
                    rules=url_class.content_rules()
                    if url_class
                    else ContentRules(min_body_bytes=1),
                    extractors=extractors,
                    fields=fields,
                )
            )
        else:
            saved_list = (
                [load_saved_response(args.fixture)]
                if args.fixture is not None
                else list(iter_saved_responses(args.fixtures_dir))
            )
            if not saved_list:
                parser.error("no fixtures found")
            for saved in saved_list:
                url_class = _class_for(args.profile, saved.url, args.url_class)
                reports.append(_compare_live(saved, url_class, args.timeout))
    except (ProfileError, KeyError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    payload = {
        "ok": True,
        "checked": len(reports),
        "regressions": sum(1 for r in reports if r.regressed),
        "critical": sum(1 for r in reports if r.severity == SEVERITY_CRITICAL),
        "reports": [r.to_dict() for r in reports],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_text(reports)
    return 1 if payload["critical"] else 0


def _print_text(reports: Sequence[RegressionReport]) -> None:
    for report in reports:
        marker = {"critical": "CRITICAL", "warning": "warning", "none": "ok"}[report.severity]
        print(f"[{marker}] {report.url}")
        print(f"  verdict:   {report.baseline_verdict} -> {report.current_verdict}")
        print(f"  rendering: {report.baseline_rendering} -> {report.current_rendering}")
        print(f"  {report.summary}")
        for field_change in report.field_changes:
            sources = f"{field_change.before_source or '-'} -> {field_change.after_source or '-'}"
            print(f"    field {field_change.field}: {field_change.kind} ({sources})")
        for structure_change in report.structure_changes:
            hint = (
                f"  [try {structure_change.replacement_hint}]"
                if structure_change.replacement_hint
                else ""
            )
            print(f"    {structure_change.kind}: {structure_change.detail}{hint}")
        print()


if __name__ == "__main__":
    raise SystemExit(main())
