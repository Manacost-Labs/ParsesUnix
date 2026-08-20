"""Running the acceptance corpus: the step between "a profile exists" and "it works".

Everything here goes through the production pieces — canonical triage for the
verdict, the real extraction chain for the fields — because a benchmark that
judges with its own private definition of success measures the benchmark. If
triage would call a page THIN_CONTENT in a run, it calls it THIN_CONTENT here,
and the corpus case fails.

Fixtures rather than live URLs, by default. Not for speed: a live corpus makes
every CI run depend on somebody else's uptime, and a failure six weeks from now
is only reproducible if the bytes that caused it were kept.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web_scraper.contracts import ContentKind, Verdict
from web_scraper.extract import detect_content_kind, extract_response, run_quorum
from web_scraper.profile_engineering.certification import CaseOutcome
from web_scraper.profile_engineering.corpus import AcceptanceCorpus, CaseKind, CorpusCase
from web_scraper.profile_engineering.mutation import (
    Expectation,
    Mutation,
    MutationRun,
    default_mutations,
    run_mutations,
)
from web_scraper.profiles.model import SiteProfile, UrlClass
from web_scraper.triage import classify_response


@dataclass(frozen=True)
class Fixture:
    """One recorded response a corpus case points at."""

    name: str
    status: int | None
    headers: dict[str, str]
    body: bytes
    url: str = ""

    @property
    def content_kind(self) -> ContentKind:
        return detect_content_kind(self.body, self.headers)


def load_fixture(directory: str | Path) -> Fixture:
    """Read a saved response in this project's own fixture layout."""

    root = Path(directory)
    meta_path = root / "meta.json"
    meta: Mapping[str, Any] = {}
    if meta_path.exists():
        parsed = json.loads(meta_path.read_text(encoding="utf-8"))
        meta = parsed if isinstance(parsed, Mapping) else {}
    body_path = next(
        (
            root / name
            for name in ("body.html", "body.json", "body.txt", "body.bin")
            if (root / name).exists()
        ),
        None,
    )
    if body_path is None:
        raise FileNotFoundError(f"{root} has no body file")
    headers = meta.get("headers") or {}
    return Fixture(
        name=root.name,
        status=meta.get("status"),
        headers={str(k): str(v) for k, v in headers.items()}
        if isinstance(headers, Mapping)
        else {},
        body=body_path.read_bytes(),
        url=str(meta.get("url", "")),
    )


def run_case(
    case: CorpusCase,
    fixture: Fixture,
    url_class: UrlClass,
) -> CaseOutcome:
    """Judge one recorded page exactly as a run would judge it live."""

    triage = classify_response(
        status=fixture.status,
        body=fixture.body,
        headers=fixture.headers,
        rules=url_class.content_rules(),
    )
    verdict_matches = triage.verdict.value == case.expect_verdict

    fields_found: dict[str, bool] = {}
    conflicts: tuple[str, ...] = ()
    comparisons = 0
    declared = tuple(url_class.field_importance)

    if triage.verdict is Verdict.OK and declared:
        result, _ = extract_response(
            fixture.body,
            headers=fixture.headers,
            extractors=list(url_class.extractors),
            fields=list(declared),
            base_url=fixture.url or case.url or None,
        )
        fields_found = {name: bool(result.data.get(name)) for name in declared}
        if url_class.quorum_fields:
            quorum = run_quorum(
                fixture.body,
                extractors=list(url_class.extractors),
                quorum_fields=list(url_class.quorum_fields),
                base_url=fixture.url or case.url or None,
            )
            comparisons = len(url_class.quorum_fields)
            conflicts = tuple(quorum.conflicts)

    expected_present = all(fields_found.get(name, False) for name in case.expect_fields)
    expected_absent = all(not fields_found.get(name, False) for name in case.expect_absent_fields)
    passed = verdict_matches and expected_present and expected_absent

    detail = ""
    if not verdict_matches:
        detail = (
            f"expected {case.expect_verdict}, triage said {triage.verdict.value}: {triage.reason}"
        )
    elif not expected_present:
        missing = [f for f in case.expect_fields if not fields_found.get(f, False)]
        detail = f"missing expected field(s): {', '.join(missing)}"
    elif not expected_absent:
        present = [f for f in case.expect_absent_fields if fields_found.get(f, False)]
        detail = f"field(s) that should have been absent: {', '.join(present)}"

    return CaseOutcome(
        case_id=case.id,
        url_class=case.url_class,
        kind=case.kind,
        verdict=triage.verdict.value,
        passed=passed,
        fields_found=fields_found,
        conflicts=conflicts,
        quorum_comparisons=comparisons,
        detail=detail,
    )


def run_corpus(
    profile: SiteProfile,
    corpus: AcceptanceCorpus,
    *,
    fixtures_root: str | Path,
) -> list[CaseOutcome]:
    """Run every case that has a fixture, and say plainly which had none.

    A case with no fixture is not skipped silently. It comes back as a failure
    whose reason is that nobody recorded the page — because a corpus entry that
    never runs looks, from every summary, exactly like one that passes.
    """

    root = Path(fixtures_root)
    outcomes: list[CaseOutcome] = []
    for case in corpus.cases:
        url_class = profile.url_classes.get(case.url_class)
        if url_class is None:
            outcomes.append(
                CaseOutcome(
                    case_id=case.id,
                    url_class=case.url_class,
                    kind=case.kind,
                    verdict="",
                    passed=False,
                    detail=f"the profile has no url_class named {case.url_class!r}",
                )
            )
            continue
        if not case.fixture:
            outcomes.append(
                CaseOutcome(
                    case_id=case.id,
                    url_class=case.url_class,
                    kind=case.kind,
                    verdict="",
                    passed=False,
                    detail="no fixture recorded; a case that never runs is not a passing case",
                )
            )
            continue
        directory = root / case.fixture
        if not directory.exists():
            outcomes.append(
                CaseOutcome(
                    case_id=case.id,
                    url_class=case.url_class,
                    kind=case.kind,
                    verdict="",
                    passed=False,
                    detail=f"fixture {case.fixture!r} is missing from {root}",
                )
            )
            continue
        outcomes.append(run_case(case, load_fixture(directory), url_class))
    return outcomes


def evaluate_mutation(
    body: bytes,
    *,
    url_class: UrlClass,
    headers: Mapping[str, str],
    status: int | None,
    mutation_kind: str,
) -> Expectation:
    """Read what the profile did to a damaged page, in the vocabulary of expectations.

    The mapping is the whole contract between "a page changed" and "somebody
    finds out". It is written here once, rather than per mutation, so a new
    mutation cannot quietly invent a lenient reading of its own result.
    """

    triage = classify_response(
        status=status, body=body, headers=headers, rules=url_class.content_rules()
    )
    if triage.verdict is not Verdict.OK:
        # A JSON path that stopped resolving is the shape changing under us,
        # which needs re-validation rather than a patch.
        if mutation_kind in {"rename_json_key", "change_field_type"}:
            return Expectation.DRIFT
        if mutation_kind == "empty_collection":
            return Expectation.EMPTY
        if mutation_kind == "remove_pagination_cursor":
            return Expectation.INCOMPLETE
        return Expectation.FAILS

    declared = tuple(url_class.field_importance)
    if not declared:
        return Expectation.SURVIVES
    result, _ = extract_response(
        body,
        headers=headers,
        extractors=list(url_class.extractors),
        fields=list(declared),
    )
    missing = {name for name in declared if not result.data.get(name)}
    if not missing:
        return Expectation.SURVIVES
    if any(url_class.field_importance[name].value == "critical" for name in missing):
        return Expectation.FAILS
    return Expectation.WARNS


def run_profile_mutations(
    profile: SiteProfile,
    corpus: AcceptanceCorpus,
    *,
    fixtures_root: str | Path,
) -> list[MutationRun]:
    """Damage each class's healthy fixture and record how the profile reacted.

    Only the NORMAL case is mutated. Mutating the 404 fixture would prove that a
    broken page stays broken, which nobody doubted.
    """

    root = Path(fixtures_root)
    runs: list[MutationRun] = []
    for name, url_class in sorted(profile.url_classes.items()):
        case = next(
            (c for c in corpus.for_class(name) if c.kind is CaseKind.NORMAL and c.fixture),
            None,
        )
        if case is None:
            continue
        directory = root / case.fixture
        if not directory.exists():
            continue
        fixture = load_fixture(directory)
        is_json = fixture.content_kind is ContentKind.JSON
        critical = [n for n, i in url_class.field_importance.items() if i.value == "critical"]
        optional = [n for n, i in url_class.field_importance.items() if i.value == "optional"]
        mutations = default_mutations(
            critical_fields=critical,
            optional_fields=optional,
            css_classes=_css_classes(url_class),
            has_pagination=url_class.declares_pagination,
            is_json=is_json,
        )

        def judge(
            damaged: bytes, mutation: Mutation, uc: UrlClass = url_class, f: Fixture = fixture
        ) -> Expectation:
            return evaluate_mutation(
                damaged,
                url_class=uc,
                headers=f.headers,
                status=f.status,
                mutation_kind=mutation.kind.value,
            )

        runs.extend(run_mutations(fixture.body, mutations, is_json=is_json, evaluate=judge))
    return runs


def _css_classes(url_class: UrlClass) -> list[str]:
    """Class names the profile's own CSS selectors depend on."""

    out: list[str] = []
    for extractor in url_class.extractors:
        if str(extractor.get("kind")) != "css":
            continue
        fields = extractor.get("fields")
        if not isinstance(fields, Mapping):
            continue
        for selector in fields.values():
            for token in str(selector).split():
                if token.startswith("."):
                    out.append(token.lstrip(".").split("::")[0])
    return sorted(set(out))


def summarise(outcomes: Sequence[CaseOutcome]) -> str:
    """The short table an operator reads after `ws-profile test`."""

    if not outcomes:
        return "no case was run"
    by_class: dict[str, list[CaseOutcome]] = {}
    for outcome in outcomes:
        by_class.setdefault(outcome.url_class, []).append(outcome)

    lines: list[str] = []
    for name, results in sorted(by_class.items()):
        lines.append(f"{name}:")
        for result in results:
            mark = "PASS" if result.passed else "FAIL"
            suffix = f"  {result.detail}" if result.detail else ""
            lines.append(f"  {result.case_id:<28}{mark}{suffix}")
    failed = [o for o in outcomes if not o.passed]
    lines.append("")
    lines.append(f"{len(outcomes) - len(failed)}/{len(outcomes)} case(s) passed")
    return "\n".join(lines)
