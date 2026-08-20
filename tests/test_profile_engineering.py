"""What has to be true before a Site Profile is allowed to carry traffic.

The tests are written against the failure they prevent rather than against the
function they call, because the failures here are all the quiet kind: a profile
that extracts nothing does not crash, it returns empty fields, at scale, until
somebody looks at the data.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import FieldImportance
from web_scraper.profile_engineering.certification import (
    ApiRouteEvidence,
    CaseOutcome,
    MutationOutcome,
    Severity,
    Verdict,
    certify,
)
from web_scraper.profile_engineering.corpus import (
    AcceptanceCorpus,
    CaseKind,
    CorpusCase,
    NotApplicable,
    corpus_from_mapping,
)
from web_scraper.profile_engineering.fragility import (
    Reliability,
    judge_extractor,
    judge_json_path,
    judge_selector,
)
from web_scraper.profile_engineering.health import (
    HealthState,
    HealthThresholds,
    RunSample,
    assess_health,
)
from web_scraper.profile_engineering.model import (
    PROFILE_SCHEMA_VERSION,
    LastKnownGood,
    LifecycleError,
    ProfileState,
    transition,
)
from web_scraper.profile_engineering.mutation import (
    Expectation,
    Mutation,
    MutationKind,
    MutationRun,
    mutate,
)
from web_scraper.profile_engineering.registry import ProfileRegistry, RegistryEntry
from web_scraper.profile_engineering.repair import (
    BrokenField,
    RepairKind,
    may_replace_last_known_good,
    propose_repairs,
)
from web_scraper.profiles.model import ProfileError, parse_profile

PACKAGE = ROOT / "site_profiles" / "example.test"


def profile_mapping(**overrides):
    """A minimal valid profile, with one knob per test."""

    validation = {
        "min_body_bytes": 100,
        "canary": "<article",
        "fields": {"title": {"importance": "critical"}, "note": {"importance": "optional"}},
    }
    validation.update(overrides.pop("validation", {}))
    url_class = {
        "match": r"^https://site\.example/a",
        "expected_content_type": "html",
        "validation": validation,
        "routes": {"primary": {"type": "direct_http", "level": "L1"}},
        "extractors": [{"kind": "json_ld"}],
        "freshness": {"max_age_hours": 24},
        "promote": {"min_completeness": 0.9},
    }
    url_class.update(overrides.pop("url_class", {}))
    payload = {
        "site": "site.example",
        "authorization": {"public_data_only": True},
        "url_classes": {"article": url_class},
    }
    payload.update(overrides)
    return payload


def corpus_with(*cases, not_applicable=()):
    return AcceptanceCorpus(
        domain="site.example", cases=tuple(cases), not_applicable=tuple(not_applicable)
    )


def case(case_id="normal", kind=CaseKind.NORMAL, url_class="article", **kw):
    return CorpusCase(id=case_id, url_class=url_class, kind=kind, fixture=case_id, **kw)


def outcome(case_id="normal", *, kind=CaseKind.NORMAL, passed=True, verdict="OK", **kw):
    return CaseOutcome(
        case_id=case_id, url_class="article", kind=kind, verdict=verdict, passed=passed, **kw
    )


class LifecycleTests(unittest.TestCase):
    def test_a_profile_starts_as_a_draft_and_cannot_jump_to_certified(self) -> None:
        with self.assertRaises(LifecycleError):
            transition(ProfileState.DRAFT, ProfileState.CERTIFIED, certified_by_checks=True)

    def test_certified_is_unreachable_without_the_checks(self) -> None:
        """The one edge that matters: 'it looks right' is not a state change."""

        with self.assertRaises(LifecycleError) as caught:
            transition(ProfileState.VALIDATING, ProfileState.CERTIFIED)
        self.assertIn("passing certification", str(caught.exception))

    def test_the_legal_path_works(self) -> None:
        state = ProfileState.DRAFT
        for target in (ProfileState.PROBING, ProfileState.VALIDATING):
            state = transition(state, target)
        state = transition(state, ProfileState.CERTIFIED, certified_by_checks=True)
        self.assertIs(state, ProfileState.CERTIFIED)

    def test_a_regression_degrades_and_a_repair_certifies_back(self) -> None:
        degraded = transition(ProfileState.CERTIFIED, ProfileState.DEGRADED)
        quarantined = transition(degraded, ProfileState.QUARANTINED)
        validating = transition(quarantined, ProfileState.VALIDATING)
        self.assertIs(
            transition(validating, ProfileState.CERTIFIED, certified_by_checks=True),
            ProfileState.CERTIFIED,
        )

    def test_staying_put_is_not_an_error(self) -> None:
        self.assertIs(
            transition(ProfileState.CERTIFIED, ProfileState.CERTIFIED), ProfileState.CERTIFIED
        )


class RegistryTests(unittest.TestCase):
    def test_the_shipped_registry_loads_and_names_the_example(self) -> None:
        registry = ProfileRegistry.load(ROOT / "site_profiles")
        self.assertIn("example.test", registry.entries)

    def test_a_newer_schema_version_is_refused_rather_than_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.yaml"
            path.write_text(f"profile_schema_version: {PROFILE_SCHEMA_VERSION + 1}\nsites: {{}}\n")
            with self.assertRaises(ValueError):
                ProfileRegistry.load(tmp)

    def test_a_registry_entry_may_not_carry_a_credential(self) -> None:
        """The registry is committed, printed and pasted into tickets."""

        with tempfile.TemporaryDirectory() as tmp:
            registry = ProfileRegistry(root=Path(tmp))
            registry.upsert(
                RegistryEntry(
                    domain="x.example", path="x.example/profile.yaml", notes="api_key=abc"
                )
            )
            with self.assertRaises(ValueError):
                registry.save()

    def test_a_round_trip_keeps_the_trusted_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = ProfileRegistry(root=Path(tmp))
            registry.upsert(
                RegistryEntry(
                    domain="x.example",
                    path="x.example/profile.yaml",
                    state=ProfileState.CERTIFIED,
                    profile_version=3,
                    last_known_good=LastKnownGood(
                        3, "sha256:abc", "2026-08-20", "sha256:def", "CERTIFIED", 0
                    ),
                )
            )
            registry.save()
            reloaded = ProfileRegistry.load(tmp)
            entry = reloaded.get("x.example")
            assert entry is not None and entry.last_known_good is not None
            self.assertEqual(entry.last_known_good.profile_version, 3)
            self.assertIs(entry.state, ProfileState.CERTIFIED)


class FieldImportanceTests(unittest.TestCase):
    def test_required_fields_still_mean_critical(self) -> None:
        """The older spelling keeps working; it is not translated away."""

        profile = parse_profile(
            profile_mapping(validation={"required_fields": ["price"], "fields": {}})
        )
        self.assertIn("price", profile.url_classes["article"].critical_fields)

    def test_one_field_cannot_have_two_severities(self) -> None:
        with self.assertRaises(ProfileError) as caught:
            parse_profile(
                profile_mapping(
                    validation={
                        "required_fields": ["title"],
                        "fields": {"title": {"importance": "optional"}},
                    }
                )
            )
        self.assertTrue(any("two severities" in e for e in caught.exception.errors))

    def test_the_three_levels_are_kept_apart(self) -> None:
        profile = parse_profile(profile_mapping())
        url_class = profile.url_classes["article"]
        self.assertEqual(url_class.fields_of(FieldImportance.CRITICAL), ("title",))
        self.assertEqual(url_class.fields_of(FieldImportance.OPTIONAL), ("note",))


class UrlClassMatchingTests(unittest.TestCase):
    def test_a_url_resolves_to_the_class_that_claims_it(self) -> None:
        profile = parse_profile(profile_mapping())
        found = profile.class_for_url("https://site.example/articles/1")
        assert found is not None
        self.assertEqual(found.name, "article")

    def test_a_url_no_class_claims_resolves_to_nothing(self) -> None:
        """Better than a default class quietly applying the wrong extractor."""

        profile = parse_profile(profile_mapping())
        self.assertIsNone(profile.class_for_url("https://site.example/other/1"))


class FragilityTests(unittest.TestCase):
    def test_positional_selectors_are_fragile(self) -> None:
        judged = judge_selector("div > div:nth-child(3) > span")
        self.assertIs(judged.reliability, Reliability.FRAGILE)

    def test_generated_class_names_are_fragile(self) -> None:
        """A build artefact is not a contract."""

        self.assertIs(judge_selector(".css-1x2y3z4").reliability, Reliability.FRAGILE)

    def test_ids_and_data_attributes_are_stable(self) -> None:
        self.assertIs(judge_selector("#main-content .price").reliability, Reliability.STABLE)
        self.assertIs(judge_selector("[data-spec-id] .score").reliability, Reliability.STABLE)

    def test_a_json_index_is_fragile_and_a_wildcard_is_not(self) -> None:
        self.assertIs(judge_json_path("rows.0.name").reliability, Reliability.FRAGILE)
        self.assertIs(judge_json_path("rows[*].name").reliability, Reliability.STABLE)

    def test_a_heuristic_cannot_hold_a_critical_field(self) -> None:
        judged = judge_extractor(field_name="title", kind="heuristic", importance="critical")
        self.assertTrue(judged.blocks_certification)

    def test_the_same_heuristic_is_fine_for_an_optional_field(self) -> None:
        judged = judge_extractor(field_name="note", kind="heuristic", importance="optional")
        self.assertFalse(judged.blocks_certification)


class CorpusTests(unittest.TestCase):
    def test_a_not_applicable_kind_is_declared_rather_than_omitted(self) -> None:
        corpus = corpus_with(
            case(),
            not_applicable=(
                NotApplicable("article", CaseKind.PAGINATION, "an article is one page"),
            ),
        )
        coverage = corpus.coverage("article", expected=(CaseKind.PAGINATION,))
        self.assertEqual(coverage["pagination"], "NOT_APPLICABLE")

    def test_an_omitted_kind_reads_as_missing_not_as_excused(self) -> None:
        coverage = corpus_with(case()).coverage("article", expected=(CaseKind.PAGINATION,))
        self.assertEqual(coverage["pagination"], "MISSING")

    def test_a_corpus_round_trips_through_its_mapping(self) -> None:
        corpus = corpus_with(
            case(), case("gone", kind=CaseKind.NOT_FOUND, expect_verdict="DEAD_URL")
        )
        rebuilt = corpus_from_mapping(corpus.to_dict())
        self.assertEqual(len(rebuilt.cases), 2)
        self.assertEqual(len(rebuilt.negative_cases()), 1)


class CertificationTests(unittest.TestCase):
    def _certify(self, *, profile=None, corpus=None, outcomes=None, **kw):
        mapping = profile or profile_mapping()
        return certify(
            parse_profile(mapping),
            corpus
            if corpus is not None
            else corpus_with(case(), case("gone", kind=CaseKind.NOT_FOUND)),
            outcomes
            if outcomes is not None
            else [
                outcome(fields_found={"title": True, "note": True}),
                outcome("gone", kind=CaseKind.NOT_FOUND, verdict="DEAD_URL"),
            ],
            raw_profile=mapping,
            **kw,
        )

    def test_no_corpus_is_insufficient_evidence_not_failure(self) -> None:
        """'We do not know' and 'it is broken' call for different work."""

        report = certify(parse_profile(profile_mapping()), None, [])
        self.assertIs(report.verdict, Verdict.INSUFFICIENT_EVIDENCE)

    def test_a_declared_corpus_that_never_ran_is_also_insufficient(self) -> None:
        report = certify(parse_profile(profile_mapping()), corpus_with(case()), [])
        self.assertIs(report.verdict, Verdict.INSUFFICIENT_EVIDENCE)

    def test_a_missing_critical_field_blocks_certification(self) -> None:
        report = self._certify(
            outcomes=[
                outcome(fields_found={"title": False}),
                outcome("gone", kind=CaseKind.NOT_FOUND, verdict="DEAD_URL"),
            ]
        )
        self.assertIs(report.verdict, Verdict.NOT_CERTIFIED)
        self.assertTrue(any(c.name == "critical_fields_present" for c in report.blockers))

    def test_a_missing_optional_field_does_not(self) -> None:
        report = self._certify(
            outcomes=[
                outcome(fields_found={"title": True, "note": False}),
                outcome("gone", kind=CaseKind.NOT_FOUND, verdict="DEAD_URL"),
            ]
        )
        self.assertTrue(report.verdict.may_activate)

    def test_a_suite_of_happy_paths_cannot_certify(self) -> None:
        """The check that stops a profile which says yes to everything."""

        report = self._certify(
            corpus=corpus_with(case()),
            outcomes=[outcome(fields_found={"title": True})],
        )
        self.assertIs(report.verdict, Verdict.NOT_CERTIFIED)
        self.assertTrue(any(c.name == "negative_case" for c in report.blockers))

    def test_quorum_conflicts_above_the_threshold_block(self) -> None:
        report = self._certify(
            profile=profile_mapping(url_class={"quorum_fields": ["title"]}),
            outcomes=[
                outcome(
                    fields_found={"title": True},
                    quorum_comparisons=10,
                    conflicts=("title", "title"),
                ),
                outcome("gone", kind=CaseKind.NOT_FOUND, verdict="DEAD_URL"),
            ],
        )
        self.assertIs(report.verdict, Verdict.NOT_CERTIFIED)

    def test_a_critical_field_on_a_heuristic_alone_blocks(self) -> None:
        report = self._certify(
            profile=profile_mapping(url_class={"extractors": [{"kind": "heuristic"}]})
        )
        self.assertIs(report.verdict, Verdict.NOT_CERTIFIED)
        self.assertTrue(any(c.name == "selector_fragility" for c in report.blockers))

    def test_an_api_route_seen_once_is_not_a_route(self) -> None:
        report = self._certify(
            api_routes=[
                ApiRouteEvidence(
                    "rankings-api",
                    "article",
                    distinct_pages=1,
                    schema_stable=True,
                    state="PROMISING",
                )
            ]
        )
        self.assertIs(report.verdict, Verdict.NOT_CERTIFIED)
        self.assertTrue(any("distinct page" in c.detail for c in report.blockers))

    def test_an_api_route_validated_across_pages_is_accepted(self) -> None:
        report = self._certify(
            api_routes=[
                ApiRouteEvidence(
                    "rankings-api",
                    "article",
                    distinct_pages=4,
                    schema_stable=True,
                    state="VALIDATED",
                )
            ]
        )
        self.assertTrue(report.verdict.may_activate)

    def test_pagination_declared_but_untested_blocks(self) -> None:
        report = self._certify(
            profile=profile_mapping(
                url_class={"pagination": {"expected_count_selector": ".total", "max_depth": 10}}
            )
        )
        self.assertTrue(any(c.name == "pagination_completeness" for c in report.blockers))

    def test_pagination_that_does_not_exist_is_not_demanded(self) -> None:
        report = self._certify()
        pagination = next(c for c in report.checks if c.name == "pagination")
        self.assertTrue(pagination.passed)
        self.assertIn("NOT APPLICABLE", pagination.detail)

    def test_a_mutation_the_profile_ignored_blocks(self) -> None:
        report = self._certify(
            mutations=[
                MutationOutcome("remove_critical_field:title", "FAILS", "SURVIVES", passed=False)
            ]
        )
        self.assertIs(report.verdict, Verdict.NOT_CERTIFIED)

    def test_an_advisory_mutation_only_warns(self) -> None:
        report = self._certify(
            mutations=[
                MutationOutcome(
                    "change_field_type:score", "DRIFT", "SURVIVES", passed=False, advisory=True
                )
            ]
        )
        self.assertIs(report.verdict, Verdict.CERTIFIED_WITH_WARNINGS)

    def test_a_profile_carrying_a_secret_is_refused(self) -> None:
        mapping = profile_mapping()
        mapping["url_classes"]["article"]["routes"]["primary"]["url"] = (
            "https://site.example/a?api_key=sk-live-1234567890abcdef"
        )
        report = certify(
            parse_profile(profile_mapping()),
            corpus_with(case(), case("gone", kind=CaseKind.NOT_FOUND)),
            [
                outcome(fields_found={"title": True}),
                outcome("gone", kind=CaseKind.NOT_FOUND, verdict="DEAD_URL"),
            ],
            raw_profile=mapping,
        )
        self.assertIs(report.verdict, Verdict.NOT_CERTIFIED)

    def test_no_percentage_is_ever_produced(self) -> None:
        """There is no honest way to turn six pages into a reliability figure."""

        report = self._certify()
        payload = json.dumps(report.to_dict())
        self.assertNotIn("reliability_score", payload)
        self.assertNotIn("confidence", payload)


class LastKnownGoodTests(unittest.TestCase):
    def _report(self, verdict: Verdict, warnings: int = 0):
        from web_scraper.profile_engineering.certification import CertificationReport, Check

        checks = tuple(Check(f"w{i}", False, "warning", Severity.WARNING) for i in range(warnings))
        return CertificationReport(domain="x", verdict=verdict, checks=checks)

    def test_nothing_trusted_yet_means_the_candidate_becomes_the_baseline(self) -> None:
        allowed, _ = may_replace_last_known_good(self._report(Verdict.CERTIFIED), None)
        self.assertTrue(allowed)

    def test_a_candidate_with_more_warnings_does_not_replace_the_incumbent(self) -> None:
        """A regression with a reassuring name is still a regression."""

        incumbent = LastKnownGood(
            1, "h", "t", "e", Verdict.CERTIFIED_WITH_WARNINGS.value, warnings=1
        )
        allowed, why = may_replace_last_known_good(
            self._report(Verdict.CERTIFIED_WITH_WARNINGS, warnings=3), incumbent
        )
        self.assertFalse(allowed)
        self.assertIn("warning", why)

    def test_a_clean_incumbent_is_not_replaced_by_a_warning_candidate(self) -> None:
        incumbent = LastKnownGood(1, "h", "t", "e", Verdict.CERTIFIED.value, warnings=0)
        allowed, _ = may_replace_last_known_good(
            self._report(Verdict.CERTIFIED_WITH_WARNINGS), incumbent
        )
        self.assertFalse(allowed)

    def test_an_uncertified_candidate_never_replaces_anything(self) -> None:
        allowed, _ = may_replace_last_known_good(self._report(Verdict.NOT_CERTIFIED), None)
        self.assertFalse(allowed)


class HealthTests(unittest.TestCase):
    def _sample(self, **kw):
        base = {
            "run_id": "r",
            "urls": 100,
            "validated": 100,
            "critical_fields_expected": 100,
            "critical_fields_found": 100,
        }
        base.update(kw)
        return RunSample(**base)

    def test_one_bad_night_does_not_degrade_a_profile(self) -> None:
        report = assess_health("x", [self._sample(critical_fields_found=10)])
        self.assertIs(report.state, HealthState.HEALTHY)
        self.assertFalse(report.should_degrade_profile)

    def test_sustained_critical_field_loss_degrades(self) -> None:
        samples = [self._sample(critical_fields_found=80) for _ in range(3)]
        report = assess_health("x", samples)
        self.assertIs(report.state, HealthState.DEGRADED)

    def test_a_fall_from_the_certified_baseline_is_louder_than_the_level(self) -> None:
        samples = [self._sample(critical_fields_found=93) for _ in range(3)]
        alone = assess_health("x", samples)
        against_baseline = assess_health("x", samples, baseline={"critical_rate": 0.998})
        self.assertIs(alone.state, HealthState.WATCH)
        self.assertIs(against_baseline.state, HealthState.DEGRADED)

    def test_a_healthy_profile_stays_healthy(self) -> None:
        report = assess_health("x", [self._sample() for _ in range(5)])
        self.assertIs(report.state, HealthState.HEALTHY)

    def test_thresholds_are_configuration_not_constants(self) -> None:
        samples = [self._sample(critical_fields_found=95) for _ in range(3)]
        strict = assess_health("x", samples, thresholds=HealthThresholds(critical_degraded=0.99))
        self.assertIs(strict.state, HealthState.DEGRADED)


class RepairTests(unittest.TestCase):
    def test_a_validated_api_beats_a_cleverer_selector(self) -> None:
        candidate = propose_repairs(
            "x.example",
            2,
            [BrokenField("article", "score", "critical", "css", ".score::text", 0.0)],
            validated_routes=[
                {
                    "id": "rankings-api",
                    "state": "VALIDATED",
                    "distinct_pages": 5,
                    "fields": ["score"],
                }
            ],
        )
        proposal = candidate.proposals[0]
        self.assertIs(proposal.kind, RepairKind.MIGRATE_TO_STRUCTURED_ROUTE)
        self.assertTrue(proposal.improves_reliability)

    def test_a_repair_candidate_is_never_activatable_on_its_own(self) -> None:
        candidate = propose_repairs(
            "x.example", 2, [BrokenField("article", "score", "critical", "css", ".s", 0.0)]
        )
        self.assertFalse(candidate.is_activatable)
        self.assertIn("proposal, not a profile in use", candidate.describe())

    def test_a_field_nobody_can_find_is_escalated_rather_than_patched(self) -> None:
        candidate = propose_repairs(
            "x.example", 1, [BrokenField("article", "gone", "critical", "css", ".g", 0.0)]
        )
        self.assertIs(candidate.proposals[0].kind, RepairKind.NEEDS_INVESTIGATION)

    def test_the_candidate_version_is_always_next(self) -> None:
        candidate = propose_repairs("x.example", 7, [])
        self.assertEqual(candidate.candidate_version, 8)


class MutationTests(unittest.TestCase):
    HTML = (
        b'<html><head><script type="application/ld+json">'
        b'{"headline": "T", "author": "A"}</script></head>'
        b'<body><article><h1 class="article-title">T</h1></article></body></html>'
    )
    JSON = b'[{"spec": "frost", "score": 91.2}]'

    def test_removing_a_critical_field_removes_it_from_every_source(self) -> None:
        """Deleting the DOM node while JSON-LD keeps the value proves nothing."""

        damaged = mutate(
            self.HTML, Mutation(MutationKind.REMOVE_CRITICAL_FIELD, "title"), is_json=False
        )
        self.assertNotIn(b"headline", damaged)
        self.assertNotIn(b"<h1", damaged)

    def test_renaming_a_class_leaves_the_structured_source_intact(self) -> None:
        damaged = mutate(
            self.HTML, Mutation(MutationKind.RENAME_CSS_CLASS, "article-title"), is_json=False
        )
        self.assertIn(b"headline", damaged)
        self.assertNotIn(b'"article-title"', damaged)

    def test_a_json_key_rename_changes_the_shape(self) -> None:
        damaged = mutate(self.JSON, Mutation(MutationKind.RENAME_JSON_KEY, "score"), is_json=True)
        self.assertIn(b"score_v2", damaged)

    def test_a_type_change_keeps_the_key_and_changes_the_value(self) -> None:
        damaged = json.loads(
            mutate(self.JSON, Mutation(MutationKind.CHANGE_FIELD_TYPE, "score"), is_json=True)
        )
        self.assertIsInstance(damaged[0]["score"], str)

    def test_a_cursor_removal_is_expected_to_be_noticed(self) -> None:
        body = b'{"items": [1], "next_cursor": "p2"}'
        damaged = mutate(body, Mutation(MutationKind.REMOVE_PAGINATION_CURSOR), is_json=True)
        self.assertNotIn(b"next_cursor", damaged)

    def test_surviving_a_class_rename_is_the_point_and_failing_is_not(self) -> None:
        rename = Mutation(MutationKind.RENAME_CSS_CLASS, "x")
        self.assertTrue(MutationRun(rename, Expectation.SURVIVES).passed)
        self.assertFalse(MutationRun(rename, Expectation.FAILS).passed)

    def test_reacting_more_loudly_than_required_still_passes(self) -> None:
        """A renamed critical key that fails the record beat the requirement."""

        renamed = Mutation(MutationKind.RENAME_JSON_KEY, "score")
        self.assertTrue(MutationRun(renamed, Expectation.FAILS).passed)

    def test_an_optional_field_found_elsewhere_is_not_a_failure(self) -> None:
        removal = Mutation(MutationKind.REMOVE_OPTIONAL_FIELD, "note")
        self.assertTrue(MutationRun(removal, Expectation.SURVIVES).passed)


class ShippedPackageTests(unittest.TestCase):
    """The example package is a test, not decoration."""

    def test_it_has_all_four_required_files(self) -> None:
        for name in ("profile.yaml", "corpus.yaml", "evidence.json", "README.md"):
            self.assertTrue((PACKAGE / name).exists(), name)

    def test_its_evidence_carries_no_urls_and_no_bodies(self) -> None:
        payload = (PACKAGE / "evidence.json").read_text()
        self.assertNotIn("https://", payload)
        self.assertNotIn("<html", payload)

    def test_it_is_registered(self) -> None:
        registry = ProfileRegistry.load(ROOT / "site_profiles")
        entry = registry.get("example.test")
        assert entry is not None
        self.assertIs(entry.state, ProfileState.CERTIFIED)
        self.assertIsNotNone(entry.last_known_good)


if __name__ == "__main__":
    unittest.main()
