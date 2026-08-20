"""``ws-profile`` — the operator's face on a profile's whole life.

One command per question somebody actually asks:

.. code-block:: text

    ws-profile list                 what exists, and what may be trusted
    ws-profile create <url>         a DRAFT built from evidence, nothing more
    ws-profile test <site>          run the acceptance corpus
    ws-profile certify <site>       run every check and record the verdict
    ws-profile explain <site>       why this route, this selector, this field
    ws-profile diff <site>          what changed that matters
    ws-profile health <site>        what production says about it now
    ws-profile repair <site>        propose a candidate; activate nothing

Two behaviours are not configurable. ``certify`` is the only command that can
move a profile to CERTIFIED, and it does so from the checks rather than from a
flag. ``repair`` never writes an active profile — its output is a candidate that
has to earn the same certification as anything else.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from web_scraper.profile_engineering.acceptance import (
    run_corpus,
    run_profile_mutations,
    summarise,
)
from web_scraper.profile_engineering.certification import (
    ApiRouteEvidence,
    MutationOutcome,
    certify,
)
from web_scraper.profile_engineering.corpus import load_corpus
from web_scraper.profile_engineering.health import (
    HealthThresholds,
    RunSample,
    assess_health,
)
from web_scraper.profile_engineering.model import (
    LastKnownGood,
    ProfileState,
    transition,
    utc_now,
)
from web_scraper.profile_engineering.registry import ProfileRegistry, RegistryEntry
from web_scraper.profile_engineering.repair import may_replace_last_known_good
from web_scraper.profiles.model import ProfileError, load_profile

DEFAULT_ROOT = Path("site_profiles")


def _registry(args: argparse.Namespace) -> ProfileRegistry:
    return ProfileRegistry.load(args.root)


def _package_paths(args: argparse.Namespace, domain: str) -> tuple[Path, Path, Path]:
    root = Path(args.root) / domain
    return root / "profile.yaml", root / "corpus.yaml", root


def _load(args: argparse.Namespace, domain: str) -> tuple[Any, Any, Path] | None:
    profile_path, corpus_path, root = _package_paths(args, domain)
    if not profile_path.exists():
        print(f"no profile at {profile_path}", file=sys.stderr)
        return None
    try:
        profile = load_profile(profile_path)
    except ProfileError as exc:
        print(f"{profile_path} does not parse:", file=sys.stderr)
        for error in exc.errors:
            print(f"  {error}", file=sys.stderr)
        return None
    corpus = load_corpus(corpus_path) if corpus_path.exists() else None
    return profile, corpus, root


# -- commands --------------------------------------------------------------


def _cmd_list(args: argparse.Namespace) -> int:
    registry = _registry(args)
    if not len(registry):
        print(f"no profiles registered under {args.root}")
        return 0
    print(f"{'site':<28}{'state':<16}{'version':>8}  last verified")
    for entry in registry:
        print(
            f"{entry.domain:<28}{entry.state.value:<16}{entry.profile_version:>8}  "
            f"{entry.last_verified or '-'}"
        )
    attention = registry.needing_attention()
    if attention:
        print()
        print("needing attention: " + ", ".join(sorted(e.domain for e in attention)))
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    loaded = _load(args, args.site)
    if loaded is None:
        return 2
    profile, corpus, root = loaded
    if corpus is None:
        print("no corpus.yaml: there is nothing to test against", file=sys.stderr)
        return 2
    outcomes = run_corpus(profile, corpus, fixtures_root=root / "fixtures")
    print(summarise(outcomes))
    return 0 if all(o.passed for o in outcomes) else 1


def _cmd_certify(args: argparse.Namespace) -> int:
    loaded = _load(args, args.site)
    if loaded is None:
        return 2
    profile, corpus, root = loaded
    outcomes = run_corpus(profile, corpus, fixtures_root=root / "fixtures") if corpus else []

    api_routes = [
        ApiRouteEvidence(
            route_id=str(item.get("route_id", "")),
            url_class=str(item.get("url_class", "")),
            distinct_pages=int(item.get("distinct_pages", 0)),
            schema_stable=bool(item.get("schema_stable", False)),
            critical_fields_found=tuple(item.get("critical_fields_found", ())),
            state=str(item.get("state", "PROMISING")),
        )
        for item in _json_list(args.api_evidence)
    ]
    # Mutations are RUN, not supplied. Accepting them as a file would let the
    # one check that proves breakage is noticed be satisfied by a file that says
    # it was.
    mutations = [
        MutationOutcome(
            name=run.mutation.name,
            expectation=run.mutation.expectation.value,
            observed=run.observed.value,
            passed=run.passed,
            advisory=run.is_advisory,
        )
        for run in (
            run_profile_mutations(profile, corpus, fixtures_root=root / "fixtures")
            if corpus
            else []
        )
    ]

    report = certify(
        profile,
        corpus,
        outcomes,
        api_routes=api_routes,
        mutations=mutations,
        raw_profile=_raw_profile(root / "profile.yaml"),
    )
    print(report.describe())

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))

    registry = _registry(args)
    entry = registry.get(args.site)
    if entry is None:
        print(f"\n{args.site} is not in the registry; nothing was recorded.", file=sys.stderr)
        return 0 if report.verdict.may_activate else 1

    if not report.verdict.may_activate:
        # A failed certification does not silently downgrade a profile that is
        # currently trusted: the LKG stays where it is, and the operator decides.
        print(f"\n{args.site} stays {entry.state.value}; the last known good version is unchanged.")
        return 1

    allowed, why = may_replace_last_known_good(report, entry.last_known_good)
    if not allowed:
        print(f"\nnot promoted: {why}")
        return 1

    new_state = transition(entry.state, ProfileState.CERTIFIED, certified_by_checks=True)
    registry.upsert(
        RegistryEntry(
            domain=entry.domain,
            path=entry.path,
            state=new_state,
            profile_version=entry.profile_version,
            last_verified=utc_now(),
            last_known_good=LastKnownGood(
                profile_version=entry.profile_version,
                profile_hash=_hash_file(root / "profile.yaml"),
                certified_at=utc_now(),
                evidence_hash=_hash_file(root / "evidence.json"),
                verdict=report.verdict.value,
                warnings=len(report.warnings),
            ),
            notes=entry.notes,
        )
    )
    registry.save()
    print(f"\n{args.site} is {new_state.value}: {why}")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    """Answer the questions a reviewer asks, from the profile and its evidence."""

    loaded = _load(args, args.site)
    if loaded is None:
        return 2
    profile, corpus, root = loaded
    evidence = _read_json(root / "evidence.json")
    entry = _registry(args).get(args.site)

    print(f"# {profile.site}")
    print()
    print(f"state: {entry.state.value if entry else 'not registered'}")
    if entry and entry.last_known_good:
        lkg = entry.last_known_good
        print(f"trusted version: v{lkg.profile_version}, certified {lkg.certified_at}")
    print()
    for name, url_class in sorted(profile.url_classes.items()):
        print(f"## {name}")
        print(f"  matches:   {url_class.match_pattern}")
        route = url_class.primary_route
        print(f"  route:     {route.type.value} at {route.level.value}")
        print("             why: it is the cheapest route the profile declares for this class")
        if url_class.alternative_routes:
            print(
                "  fallbacks: "
                + ", ".join(f"{r.type.value}@{r.level.value}" for r in url_class.alternative_routes)
            )
        print(
            f"  proof:     {', '.join(url_class.canaries) or url_class.required_json_paths or '-'}"
        )
        for field_name, importance in sorted(url_class.field_importance.items()):
            reason = {
                "critical": "the record is not a record without it",
                "important": "the record is usable but poorer without it",
                "optional": "nice to have",
            }[importance.value]
            print(f"  field {field_name:<14} {importance.value:<10} {reason}")
        if url_class.quorum_fields:
            print(f"  quorum:    {', '.join(url_class.quorum_fields)}")
        else:
            print("  quorum:    none — a silent extractor change would not be visible")
        cases = corpus.for_class(name) if corpus else ()
        negatives = [c for c in cases if c.is_negative]
        print(f"  corpus:    {len(cases)} case(s), {len(negatives)} negative")
        print()
    if evidence:
        print("## evidence")
        for key in ("tested_pages", "distinct_entities", "generated_at", "state"):
            if key in evidence:
                print(f"  {key}: {evidence[key]}")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    """Semantic differences only — a YAML diff is a different tool."""

    old = _raw_profile(Path(args.old))
    new = _raw_profile(Path(args.new))
    if old is None or new is None:
        print("both profiles must exist and parse", file=sys.stderr)
        return 2
    changes = semantic_diff(old, new)
    if not changes:
        print("no semantic change: the two profiles would behave the same")
        return 0
    for change in changes:
        print(change)
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    samples = [
        RunSample(
            run_id=str(item.get("run_id", f"run-{index}")),
            urls=int(item.get("urls", 0)),
            validated=int(item.get("validated", 0)),
            critical_fields_expected=int(item.get("critical_fields_expected", 0)),
            critical_fields_found=int(item.get("critical_fields_found", 0)),
            quorum_comparisons=int(item.get("quorum_comparisons", 0)),
            quorum_conflicts=int(item.get("quorum_conflicts", 0)),
            browser_escalations=int(item.get("browser_escalations", 0)),
            paid_escalations=int(item.get("paid_escalations", 0)),
            schema_drift_events=int(item.get("schema_drift_events", 0)),
            pagination_incomplete=int(item.get("pagination_incomplete", 0)),
        )
        for index, item in enumerate(_json_list(args.runs))
    ]
    report = assess_health(args.site, samples, thresholds=HealthThresholds())
    print(report.describe())
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    if not report.should_degrade_profile:
        return 0

    registry = _registry(args)
    entry = registry.get(args.site)
    if entry is not None and entry.state is ProfileState.CERTIFIED:
        registry.upsert(
            RegistryEntry(
                domain=entry.domain,
                path=entry.path,
                state=transition(entry.state, ProfileState.DEGRADED),
                profile_version=entry.profile_version,
                last_verified=entry.last_verified,
                last_known_good=entry.last_known_good,
                notes=f"degraded by health check: {report.signals[0] if report.signals else ''}",
            )
        )
        registry.save()
        print(f"\n{args.site} moved to DEGRADED. The trusted version is unchanged.")
    return 1


def _cmd_validate(args: argparse.Namespace) -> int:
    from web_scraper.profiles.cli import main as legacy

    return legacy(["validate", args.profile])


def _cmd_draft(args: argparse.Namespace) -> int:
    from web_scraper.profiles.cli import main as legacy

    argv = ["draft", "--probe-report", args.probe_report, "--url-class", args.url_class]
    for field_name in args.required_field:
        argv += ["--required-field", field_name]
    if args.out:
        argv += ["--out", args.out]
    return legacy(argv)


# -- helpers ---------------------------------------------------------------


def semantic_diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What changed that would change behaviour.

    Reformatting, comments and key order are invisible here on purpose: a diff
    that reports them buries the one line that matters — the route that moved
    from an API to a browser, or the field that quietly stopped being critical.
    """

    changes: list[str] = []
    old_classes = old.get("url_classes", {}) or {}
    new_classes = new.get("url_classes", {}) or {}

    for name in sorted(set(old_classes) - set(new_classes)):
        changes.append(f"- url_class removed: {name}")
    for name in sorted(set(new_classes) - set(old_classes)):
        changes.append(f"+ url_class added: {name}")

    for name in sorted(set(old_classes) & set(new_classes)):
        before, after = old_classes[name] or {}, new_classes[name] or {}
        b_route = (before.get("routes", {}) or {}).get("primary", {}) or {}
        a_route = (after.get("routes", {}) or {}).get("primary", {}) or {}
        if b_route != a_route:
            changes.append(
                f"~ [{name}] primary route: {b_route.get('type')}@{b_route.get('level')} "
                f"-> {a_route.get('type')}@{a_route.get('level')}"
            )
        b_fields = _importances(before)
        a_fields = _importances(after)
        for field_name in sorted(set(b_fields) | set(a_fields)):
            was, now = b_fields.get(field_name), a_fields.get(field_name)
            if was != now:
                changes.append(
                    f"~ [{name}] field {field_name}: {was or 'absent'} -> {now or 'absent'}"
                )
        b_ex = [e.get("kind") for e in before.get("extractors", []) or []]
        a_ex = [e.get("kind") for e in after.get("extractors", []) or []]
        if b_ex != a_ex:
            changes.append(f"~ [{name}] extractor chain: {b_ex} -> {a_ex}")
        if (before.get("freshness") or {}) != (after.get("freshness") or {}):
            changes.append(f"~ [{name}] freshness policy changed")
        if (before.get("pagination") or {}) != (after.get("pagination") or {}):
            changes.append(f"~ [{name}] pagination strategy changed")
    return changes


def _importances(url_class: dict[str, Any]) -> dict[str, str]:
    validation = url_class.get("validation", {}) or {}
    out = dict.fromkeys(validation.get("required_fields", []) or [], "critical")
    fields = validation.get("fields", {}) or {}
    for name, spec in fields.items():
        if isinstance(spec, str):
            out[name] = spec
        elif isinstance(spec, dict):
            out[name] = str(spec.get("importance", "optional"))
    return out


def _raw_profile(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        parsed = json.loads(text)
    else:
        try:
            import yaml

            parsed = yaml.safe_load(text)
        except ImportError:
            from web_scraper.profiles.yamlish import loads

            parsed = loads(text)
    return parsed if isinstance(parsed, dict) else None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _json_list(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _hash_file(path: Path) -> str:
    from web_scraper.observability.manifest import stable_hash

    if not path.exists():
        return ""
    return stable_hash(path.read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ws-profile", description="Create, test, certify and repair Site Profiles."
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="profile packages directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="what exists and what may be trusted").set_defaults(func=_cmd_list)

    test = sub.add_parser("test", help="run the acceptance corpus")
    test.add_argument("site")
    test.set_defaults(func=_cmd_test)

    cert = sub.add_parser("certify", help="run every check and record the verdict")
    cert.add_argument("site")
    cert.add_argument("--api-evidence", default=None, help="JSON list of discovered route evidence")
    cert.add_argument("--json", action="store_true")
    cert.set_defaults(func=_cmd_certify)

    explain = sub.add_parser("explain", help="why this route, this field, this verdict")
    explain.add_argument("site")
    explain.set_defaults(func=_cmd_explain)

    diff = sub.add_parser("diff", help="semantic differences between two profiles")
    diff.add_argument("old")
    diff.add_argument("new")
    diff.set_defaults(func=_cmd_diff)

    health = sub.add_parser("health", help="what production runs say about this profile")
    health.add_argument("site")
    health.add_argument("--runs", required=True, help="JSON list of run samples")
    health.add_argument("--json", action="store_true")
    health.set_defaults(func=_cmd_health)

    # The two older commands keep working, as subcommands of the same facade
    # rather than as a second entry point. `ws-profile validate` is in the
    # web-scraper skill's own instructions and in people's shell history; moving
    # it would break both to no purpose.
    validate = sub.add_parser("validate", help="validate one profile file")
    validate.add_argument("profile")
    validate.set_defaults(func=_cmd_validate)

    draft = sub.add_parser("draft", help="draft a profile from a saved probe report")
    draft.add_argument("--probe-report", required=True)
    draft.add_argument("--url-class", default="page")
    draft.add_argument("--required-field", action="append", default=[])
    draft.add_argument("--out", default=None)
    draft.set_defaults(func=_cmd_draft)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
