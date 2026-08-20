"""Deciding whether a profile has earned production traffic — by checking, not judging.

Every check here is a function of evidence that already exists: the parsed
profile, the acceptance corpus and its results, what discovery observed, how
fragile the selectors are. Nothing consults an opinion, and there is no argument
that means "approve anyway". That is the entire design: the expensive failure
in this system is not a profile that fails certification, it is a profile that
passes it because somebody was confident.

The verdicts are named rather than scored:

``CERTIFIED``
    Every check passed. May be activated.
``CERTIFIED_WITH_WARNINGS``
    Nothing blocking, something worth reading first. May be activated by an
    operator who has read the warnings.
``NOT_CERTIFIED``
    A blocking check failed. The profile may not carry production traffic.
``INSUFFICIENT_EVIDENCE``
    The checks could not be *run* — no corpus, no results, nothing to judge.
    Deliberately distinct from failure: "we do not know" and "it is broken" call
    for different work, and collapsing them into one number is how a profile
    with three samples ends up described as 99% reliable.

A percentage is never produced. There is no honest way to turn six pages into a
reliability figure, and the figure would be quoted long after the six pages were
forgotten.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from web_scraper.contracts import FieldImportance
from web_scraper.profile_engineering.corpus import AcceptanceCorpus, CaseKind
from web_scraper.profile_engineering.fragility import (
    ExtractorJudgement,
    judge_extractor,
)
from web_scraper.profiles.model import SiteProfile, UrlClass

#: How many DISTINCT pages an internal API endpoint must have answered before a
#: profile may route through it. One page is a coincidence: the endpoint existed
#: during one render, and nothing yet says it is a route rather than a detail of
#: that page.
MIN_DISTINCT_PAGES_FOR_API = 3

#: Share of quorum comparisons that may disagree before a critical field is
#: considered unresolved. Not zero: two sources rounding differently is normal.
#: Not generous either — a field where the sources disagree one time in ten is a
#: field nobody can trust.
MAX_CRITICAL_CONFLICT_RATE = 0.02


class Verdict(StrEnum):
    CERTIFIED = "CERTIFIED"
    CERTIFIED_WITH_WARNINGS = "CERTIFIED_WITH_WARNINGS"
    NOT_CERTIFIED = "NOT_CERTIFIED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

    @property
    def may_activate(self) -> bool:
        return self in {Verdict.CERTIFIED, Verdict.CERTIFIED_WITH_WARNINGS}


class Severity(StrEnum):
    BLOCKER = "BLOCKER"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass(frozen=True)
class Check:
    """One question, its answer, and why the answer matters."""

    name: str
    passed: bool
    detail: str
    severity: Severity = Severity.BLOCKER
    url_class: str = ""

    @property
    def blocks(self) -> bool:
        return not self.passed and self.severity is Severity.BLOCKER

    @property
    def warns(self) -> bool:
        return not self.passed and self.severity is Severity.WARNING

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "severity": self.severity.value,
            "url_class": self.url_class,
        }


@dataclass(frozen=True)
class CaseOutcome:
    """What running one corpus case actually produced."""

    case_id: str
    url_class: str
    kind: CaseKind
    verdict: str
    passed: bool
    fields_found: Mapping[str, bool] = field(default_factory=dict)
    conflicts: tuple[str, ...] = ()
    quorum_comparisons: int = 0
    records: int | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "url_class": self.url_class,
            "kind": self.kind.value,
            "verdict": self.verdict,
            "passed": self.passed,
            "fields_found": dict(self.fields_found),
            "conflicts": list(self.conflicts),
            "quorum_comparisons": self.quorum_comparisons,
            "records": self.records,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ApiRouteEvidence:
    """What is known about an internal endpoint the profile wants to use."""

    route_id: str
    url_class: str
    distinct_pages: int
    schema_stable: bool
    critical_fields_found: tuple[str, ...] = ()
    state: str = "PROMISING"

    @property
    def is_validated(self) -> bool:
        return (
            self.state == "VALIDATED"
            and self.distinct_pages >= MIN_DISTINCT_PAGES_FOR_API
            and self.schema_stable
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "url_class": self.url_class,
            "distinct_pages": self.distinct_pages,
            "schema_stable": self.schema_stable,
            "critical_fields_found": list(self.critical_fields_found),
            "state": self.state,
            "validated": self.is_validated,
        }


@dataclass(frozen=True)
class MutationOutcome:
    """One mutation, and whether the profile reacted the way it must."""

    name: str
    expectation: str
    observed: str
    passed: bool
    #: Advisory failures are findings, not blockers. A profile cannot notice a
    #: number quietly becoming a string unless it declares field types, and a
    #: check that refuses certification over that is a check people delete.
    advisory: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expected": self.expectation,
            "observed": self.observed,
            "passed": self.passed,
            "advisory": self.advisory,
        }


@dataclass
class CertificationReport:
    """The whole answer: verdict, every check, and the reasoning behind it."""

    domain: str
    verdict: Verdict
    checks: tuple[Check, ...] = ()
    fragility: tuple[ExtractorJudgement, ...] = ()
    outcomes: tuple[CaseOutcome, ...] = ()
    mutations: tuple[MutationOutcome, ...] = ()
    api_routes: tuple[ApiRouteEvidence, ...] = ()

    @property
    def blockers(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.blocks)

    @property
    def warnings(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.warns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "verdict": self.verdict.value,
            "may_activate": self.verdict.may_activate,
            "blockers": [c.to_dict() for c in self.blockers],
            "warnings": [c.to_dict() for c in self.warnings],
            "checks": [c.to_dict() for c in self.checks],
            "fragility": [j.to_dict() for j in self.fragility],
            "case_outcomes": [o.to_dict() for o in self.outcomes],
            "mutations": [m.to_dict() for m in self.mutations],
            "api_routes": [r.to_dict() for r in self.api_routes],
        }

    def describe(self) -> str:
        lines = [f"{self.domain}: {self.verdict.value}", ""]
        for check in self.checks:
            mark = "PASS" if check.passed else check.severity.value
            scope = f"[{check.url_class}] " if check.url_class else ""
            lines.append(f"  {mark:<8}{scope}{check.name}: {check.detail}")
        if self.fragility:
            lines += ["", "selectors:"]
            for judgement in self.fragility:
                lines.append(
                    f"  {judgement.judgement.reliability.value:<8}"
                    f"{judgement.field:<18}{judgement.kind:<12}{judgement.locator}"
                )
        if not self.verdict.may_activate:
            lines += ["", "This profile may not carry production traffic."]
        return "\n".join(lines)


def certify(
    profile: SiteProfile,
    corpus: AcceptanceCorpus | None,
    outcomes: Sequence[CaseOutcome] = (),
    *,
    api_routes: Sequence[ApiRouteEvidence] = (),
    mutations: Sequence[MutationOutcome] = (),
    raw_profile: Mapping[str, Any] | None = None,
) -> CertificationReport:
    """Run every check and return the verdict the evidence supports.

    Deterministic by construction: same inputs, same answer, no clock, no
    network, no randomness. That is what makes a certification something two
    people can disagree about productively.
    """

    checks: list[Check] = []
    judgements: list[ExtractorJudgement] = []

    if corpus is None or not corpus.cases:
        return CertificationReport(
            domain=profile.site,
            verdict=Verdict.INSUFFICIENT_EVIDENCE,
            checks=(
                Check(
                    "acceptance_corpus",
                    False,
                    "no corpus: there is nothing to certify against, which is not "
                    "the same as failing",
                    Severity.BLOCKER,
                ),
            ),
        )
    if not outcomes:
        return CertificationReport(
            domain=profile.site,
            verdict=Verdict.INSUFFICIENT_EVIDENCE,
            checks=(
                Check(
                    "corpus_results",
                    False,
                    f"{len(corpus.cases)} case(s) declared and none run",
                    Severity.BLOCKER,
                ),
            ),
        )

    checks.append(_secrets_check(raw_profile))

    by_class: dict[str, list[CaseOutcome]] = {}
    for outcome in outcomes:
        by_class.setdefault(outcome.url_class, []).append(outcome)

    for name, url_class in sorted(profile.url_classes.items()):
        results = by_class.get(name, [])
        checks.extend(_class_checks(name, url_class, corpus, results))
        judgements.extend(_judge_fields(url_class))

    checks.append(_fragility_check(judgements))
    checks.extend(_api_checks(profile, api_routes))
    checks.append(_mutation_check(mutations, judgements))

    blocking = any(c.blocks for c in checks)
    warning = any(c.warns for c in checks)
    verdict = (
        Verdict.NOT_CERTIFIED
        if blocking
        else (Verdict.CERTIFIED_WITH_WARNINGS if warning else Verdict.CERTIFIED)
    )
    return CertificationReport(
        domain=profile.site,
        verdict=verdict,
        checks=tuple(checks),
        fragility=tuple(judgements),
        outcomes=tuple(outcomes),
        mutations=tuple(mutations),
        api_routes=tuple(api_routes),
    )


# -- individual checks -----------------------------------------------------


def _class_checks(
    name: str,
    url_class: UrlClass,
    corpus: AcceptanceCorpus,
    results: Sequence[CaseOutcome],
) -> list[Check]:
    checks: list[Check] = []

    checks.append(
        Check(
            "cases_run",
            bool(results),
            f"{len(results)} case(s) run" if results else "no case was run for this class",
            Severity.BLOCKER,
            name,
        )
    )
    if not results:
        return checks

    failed = [r for r in results if not r.passed]
    checks.append(
        Check(
            "cases_pass",
            not failed,
            "every case passed"
            if not failed
            else f"{len(failed)} failing: {', '.join(sorted(r.case_id for r in failed))}",
            Severity.BLOCKER,
            name,
        )
    )

    negatives = [r for r in results if r.kind.is_negative or r.verdict != "OK"]
    checks.append(
        Check(
            "negative_case",
            bool(negatives),
            f"{len(negatives)} negative case(s): {', '.join(sorted(r.case_id for r in negatives))}"
            if negatives
            else (
                "no negative case. A suite where everything is expected to succeed "
                "cannot tell a working profile from one that accepts anything"
            ),
            Severity.BLOCKER,
            name,
        )
    )

    critical = url_class.critical_fields
    checks.append(
        Check(
            "critical_fields_declared",
            bool(critical),
            f"critical: {', '.join(critical)}"
            if critical
            else "no field is declared critical, so nothing here can fail for being absent",
            Severity.BLOCKER,
            name,
        )
    )

    if critical:
        positives = [r for r in results if not r.kind.is_negative and r.verdict == "OK"]
        missing: dict[str, list[str]] = {}
        for result in positives:
            for field_name in critical:
                if not result.fields_found.get(field_name, False):
                    missing.setdefault(field_name, []).append(result.case_id)
        checks.append(
            Check(
                "critical_fields_present",
                not missing,
                "every critical field was extracted on every positive case"
                if not missing
                else "; ".join(
                    f"{f} missing on {', '.join(sorted(cases))}"
                    for f, cases in sorted(missing.items())
                ),
                Severity.BLOCKER,
                name,
            )
        )

    comparisons = sum(r.quorum_comparisons for r in results)
    conflicts = sum(len(r.conflicts) for r in results)
    if comparisons:
        rate = conflicts / comparisons
        checks.append(
            Check(
                "quorum_conflicts",
                rate <= MAX_CRITICAL_CONFLICT_RATE,
                f"{conflicts}/{comparisons} comparisons disagreed ({rate:.1%})",
                Severity.BLOCKER,
                name,
            )
        )
    elif url_class.quorum_fields:
        checks.append(
            Check(
                "quorum_conflicts",
                False,
                "quorum fields are declared but no comparison was made — a second "
                "source that is never consulted proves nothing",
                Severity.WARNING,
                name,
            )
        )
    else:
        checks.append(
            Check(
                "quorum",
                False,
                "no field has a second independent source; a silent extractor "
                "change would not be visible",
                Severity.WARNING,
                name,
            )
        )

    checks.append(_pagination_check(name, url_class, corpus, results))
    checks.append(
        Check(
            "freshness_policy",
            bool(url_class.freshness),
            "declared"
            if url_class.freshness
            else "no freshness policy: re-fetch cadence is undefined",
            Severity.WARNING,
            name,
        )
    )
    checks.append(
        Check(
            "promotion_policy",
            bool(url_class.promote),
            "declared"
            if url_class.promote
            else "no promotion thresholds: a bad run could replace good data",
            Severity.WARNING,
            name,
        )
    )
    return checks


def _pagination_check(
    name: str,
    url_class: UrlClass,
    corpus: AcceptanceCorpus,
    results: Sequence[CaseOutcome],
) -> Check:
    """Pagination is only required where pagination exists.

    Demanding a pagination case from a class that has one page is how a
    checklist teaches people to write fake tests.
    """

    declared = url_class.declares_pagination
    excused = corpus.excused(name, CaseKind.PAGINATION)
    tested = any(r.kind is CaseKind.PAGINATION for r in results)

    if not declared:
        return Check(
            "pagination",
            True,
            "NOT APPLICABLE: this class declares no pagination",
            Severity.INFO,
            name,
        )
    if tested:
        complete = all(r.passed for r in results if r.kind is CaseKind.PAGINATION)
        return Check(
            "pagination_completeness",
            complete,
            "pagination case passed" if complete else "the pagination case did not complete",
            Severity.BLOCKER,
            name,
        )
    if excused is not None:
        return Check(
            "pagination_completeness",
            True,
            f"NOT APPLICABLE: {excused.reason}",
            Severity.INFO,
            name,
        )
    return Check(
        "pagination_completeness",
        False,
        "pagination is declared but no case tests it; an incomplete crawl looks "
        "exactly like a complete one",
        Severity.BLOCKER,
        name,
    )


def _judge_fields(url_class: UrlClass) -> list[ExtractorJudgement]:
    """Judge every declared field against every extractor that could supply it."""

    out: list[ExtractorJudgement] = []
    for field_name, importance in sorted(url_class.field_importance.items()):
        for extractor in url_class.extractors:
            kind = str(extractor.get("kind", ""))
            fields = extractor.get("fields")
            locator = ""
            if isinstance(fields, Mapping) and field_name in fields:
                locator = str(fields[field_name])
            elif isinstance(fields, Mapping):
                continue  # this extractor does not claim to supply the field
            out.append(
                judge_extractor(
                    field_name=field_name,
                    kind=kind,
                    locator=locator,
                    importance=importance.value,
                )
            )
    return out


def _fragility_check(judgements: Sequence[ExtractorJudgement]) -> Check:
    """A critical field may not rest ONLY on something fragile.

    Per field, not per selector: a critical field with a fragile CSS path and a
    solid JSON path is fine — that is what having two sources is for.
    """

    critical = [j for j in judgements if j.importance == FieldImportance.CRITICAL.value]
    if not critical:
        return Check("selector_fragility", True, "no critical field to judge", Severity.INFO)

    by_field: dict[str, list[ExtractorJudgement]] = {}
    for judgement in critical:
        by_field.setdefault(judgement.field, []).append(judgement)

    unsupported = [
        name
        for name, items in by_field.items()
        if not any(j.judgement.reliability.is_acceptable_for_critical for j in items)
    ]
    if unsupported:
        return Check(
            "selector_fragility",
            False,
            "critical field(s) resting only on fragile or heuristic extraction: "
            + ", ".join(sorted(unsupported)),
            Severity.BLOCKER,
        )
    return Check(
        "selector_fragility",
        True,
        f"{len(by_field)} critical field(s), each with at least one non-fragile source",
        Severity.INFO,
    )


def _api_checks(profile: SiteProfile, routes: Sequence[ApiRouteEvidence]) -> list[Check]:
    """An endpoint seen once is not a route.

    The profile may reference a discovered API only if discovery validated it
    across several distinct pages with a stable schema. Anything less is a lead.
    """

    if not routes:
        return [
            Check(
                "api_routes",
                True,
                "no internal API route is used by this profile",
                Severity.INFO,
            )
        ]
    checks: list[Check] = []
    for route in routes:
        if route.is_validated:
            checks.append(
                Check(
                    "api_route_validated",
                    True,
                    f"{route.route_id}: {route.distinct_pages} distinct pages, stable schema",
                    Severity.INFO,
                    route.url_class,
                )
            )
            continue
        reasons = []
        if route.distinct_pages < MIN_DISTINCT_PAGES_FOR_API:
            reasons.append(
                f"seen on {route.distinct_pages} distinct page(s), "
                f"{MIN_DISTINCT_PAGES_FOR_API} required"
            )
        if not route.schema_stable:
            reasons.append("the schema changed between observations")
        if route.state != "VALIDATED":
            reasons.append(f"discovery state is {route.state}")
        checks.append(
            Check(
                "api_route_validated",
                False,
                f"{route.route_id}: " + "; ".join(reasons),
                Severity.BLOCKER,
                route.url_class,
            )
        )
    return checks


def _mutation_check(
    mutations: Sequence[MutationOutcome], judgements: Sequence[ExtractorJudgement]
) -> Check:
    """Critical extraction paths must have been broken on purpose at least once.

    A test suite that has never seen the extractor fail cannot claim the
    extractor's failure would be noticed.
    """

    critical_fields = {
        j.field for j in judgements if j.importance == FieldImportance.CRITICAL.value
    }
    if not critical_fields:
        return Check("mutation_coverage", True, "no critical field to mutate", Severity.INFO)
    if not mutations:
        return Check(
            "mutation_coverage",
            False,
            "no mutation was run: nothing has ever confirmed that breaking a "
            "critical extractor would be noticed",
            Severity.WARNING,
        )
    blocking = [m for m in mutations if not m.passed and not m.advisory]
    if blocking:
        return Check(
            "mutation_coverage",
            False,
            "mutations the profile did not react to correctly: "
            + ", ".join(sorted(m.name for m in blocking)),
            Severity.BLOCKER,
        )
    advisory = [m for m in mutations if not m.passed]
    if advisory:
        return Check(
            "mutation_coverage",
            False,
            f"{len(mutations)} mutation(s) run; the profile would not notice: "
            + ", ".join(sorted(m.name for m in advisory)),
            Severity.WARNING,
        )
    return Check(
        "mutation_coverage",
        True,
        f"{len(mutations)} mutation(s), all reacted to as expected",
        Severity.INFO,
    )


def _secrets_check(raw_profile: Mapping[str, Any] | None) -> Check:
    """A profile package is committed, printed, and pasted into tickets."""

    if raw_profile is None:
        return Check(
            "no_secrets",
            True,
            "raw profile not supplied; the parser's own secret scan still applies",
            Severity.INFO,
        )
    from web_scraper.profiles.model import ProfileError, parse_profile

    try:
        parse_profile(raw_profile)
    except ProfileError as exc:
        problems = [str(e) for e in exc.args[0]] if exc.args else []
        leaks = [p for p in problems if "secret" in p.lower() or "token" in p.lower()]
        if leaks:
            return Check("no_secrets", False, "; ".join(leaks), Severity.BLOCKER)
        return Check(
            "profile_schema",
            False,
            "; ".join(problems[:3]) or "profile did not parse",
            Severity.BLOCKER,
        )
    return Check("no_secrets", True, "profile parses and carries no credential", Severity.INFO)
