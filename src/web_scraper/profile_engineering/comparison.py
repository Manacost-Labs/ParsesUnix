"""Offline, fixture-for-fixture comparison of two Site Profiles.

This is deliberately a profile comparison, not a replay of two application
builds: both profiles receive the same acceptance corpus and bytes, and no
network, registry, or profile package state is touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web_scraper.profile_engineering.acceptance import load_fixture_snapshots, run_corpus
from web_scraper.profile_engineering.certification import CaseOutcome
from web_scraper.profile_engineering.corpus import AcceptanceCorpus
from web_scraper.profiles.model import SiteProfile


@dataclass(frozen=True)
class ComparisonReport:
    """A fail-closed report for one corpus exercised by two profiles."""

    cases: tuple[dict[str, Any], ...]
    integrity_errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.integrity_errors and all(case["candidate"]["passed"] for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "integrity_errors": list(self.integrity_errors),
            "cases": list(self.cases),
        }


def compare_corpus(
    baseline: SiteProfile,
    candidate: SiteProfile,
    corpus: AcceptanceCorpus,
    *,
    fixtures_root: str | Path,
) -> ComparisonReport:
    """Run the identical local corpus through baseline and candidate profiles."""

    integrity_errors = _integrity_errors(baseline, candidate, corpus)
    try:
        fixtures = load_fixture_snapshots(corpus, fixtures_root=fixtures_root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return ComparisonReport(cases=(), integrity_errors=(f"invalid fixture snapshot: {exc}",))
    baseline_outcomes = run_corpus(baseline, corpus, fixtures_root=fixtures_root, fixtures=fixtures)
    candidate_outcomes = run_corpus(
        candidate, corpus, fixtures_root=fixtures_root, fixtures=fixtures
    )
    baseline_by_case = {outcome.case_id: outcome for outcome in baseline_outcomes}
    candidate_by_case = {outcome.case_id: outcome for outcome in candidate_outcomes}

    cases: list[dict[str, Any]] = []
    for case in corpus.cases:
        before = baseline_by_case.get(case.id)
        after = candidate_by_case.get(case.id)
        if before is None or after is None:
            integrity_errors.append(f"case {case.id!r} did not produce one outcome per profile")
            continue
        cases.append(_case_delta(before, after))
    return ComparisonReport(
        cases=tuple(cases), integrity_errors=tuple(sorted(set(integrity_errors)))
    )


def _integrity_errors(
    baseline: SiteProfile, candidate: SiteProfile, corpus: AcceptanceCorpus
) -> list[str]:
    errors: list[str] = []
    identifiers = [case.id for case in corpus.cases]
    duplicates = sorted({case_id for case_id in identifiers if identifiers.count(case_id) > 1})
    errors.extend(f"duplicate case id: {case_id!r}" for case_id in duplicates)
    if not corpus.cases:
        errors.append("corpus has no cases")

    corpus_classes = {case.url_class for case in corpus.cases}
    for label, profile in (("baseline", baseline), ("candidate", candidate)):
        profile_classes = set(profile.url_classes)
        for name in sorted(corpus_classes - profile_classes):
            errors.append(f"{label} profile is missing corpus url_class {name!r}")
        for name in sorted(profile_classes - corpus_classes):
            errors.append(f"corpus is missing cases for {label} url_class {name!r}")
    return errors


def _case_delta(baseline: CaseOutcome, candidate: CaseOutcome) -> dict[str, Any]:
    before = baseline.to_dict()
    after = candidate.to_dict()
    return {
        "case_id": candidate.case_id,
        "url_class": candidate.url_class,
        "baseline": before,
        "candidate": after,
        "delta": {
            field: {
                "baseline": before[field],
                "candidate": after[field],
                "changed": before[field] != after[field],
            }
            for field in ("verdict", "passed", "fields_found", "records", "conflicts")
        },
    }
