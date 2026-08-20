"""Two whole lifetimes of a profile, from a blank directory to a decision.

Unit tests prove each piece behaves. These prove the pieces add up to the
property the system exists for: a site can change underneath a profile without
the dataset quietly becoming wrong.

The first scenario is the good outcome — the markup is rewritten and nothing is
lost, because the critical field was never resting on markup alone. The second
is the bad one — the structure itself changes, the field really is gone, and
what has to happen then is that production keeps running the version that
works while a candidate waits for a human.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import ContentKind
from web_scraper.profile_engineering.acceptance import (
    load_fixture,
    run_case,
    run_corpus,
    run_profile_mutations,
)
from web_scraper.profile_engineering.builder import (
    DiscoveredRoute,
    ObservedPage,
    build_draft,
)
from web_scraper.profile_engineering.certification import (
    MutationOutcome,
    Verdict,
    certify,
)
from web_scraper.profile_engineering.corpus import CaseKind, load_corpus
from web_scraper.profile_engineering.health import RunSample, assess_health
from web_scraper.profile_engineering.model import LastKnownGood, ProfileState, transition
from web_scraper.profile_engineering.registry import ProfileRegistry, RegistryEntry
from web_scraper.profile_engineering.repair import (
    BrokenField,
    RepairKind,
    may_replace_last_known_good,
    propose_repairs,
)
from web_scraper.profiles.model import load_profile

PACKAGE = ROOT / "site_profiles" / "example.test"
FIXTURES = PACKAGE / "fixtures"


class ProfileCreationScenario(unittest.TestCase):
    """From "add this site" to a package somebody can review."""

    def test_a_draft_is_built_from_observation_and_stays_a_draft(self) -> None:
        pages = [
            ObservedPage(
                url="https://shop.example/products/1",
                content_kind=ContentKind.HTML,
                status=200,
                body_bytes=40_000,
                available_sources=("json_ld", "css"),
                canary_candidates=("<article",),
            ),
            ObservedPage(
                url="https://shop.example/products/2",
                content_kind=ContentKind.HTML,
                status=200,
                body_bytes=41_000,
                available_sources=("json_ld", "css"),
                canary_candidates=("<article",),
            ),
        ]
        draft = build_draft(
            "shop.example", pages, wanted_fields=("title", "price"), critical_fields=("price",)
        )
        payload = draft.to_mapping()
        self.assertEqual(payload["status"], ProfileState.DRAFT.value)
        self.assertIn("products", payload["url_classes"])

    def test_a_validated_endpoint_is_preferred_over_the_markup_that_shows_it(self) -> None:
        """The routing preference, exercised rather than described."""

        pages = [
            ObservedPage(
                url="https://shop.example/rankings",
                content_kind=ContentKind.HTML,
                status=200,
                body_bytes=3_000,
                requires_javascript=True,
            )
        ]
        draft = build_draft(
            "shop.example",
            pages,
            wanted_fields=("score",),
            critical_fields=("score",),
            routes=[
                DiscoveredRoute(
                    id="rankings-api",
                    url="https://shop.example/api/rankings",
                    state="VALIDATED",
                    distinct_pages=4,
                    fields=("score",),
                )
            ],
        )
        route = draft.to_mapping()["url_classes"]["rankings"]["routes"]["primary"]
        self.assertEqual(route["type"], "json_api")
        self.assertEqual(route["level"], "L0")

    def test_an_endpoint_seen_once_is_reported_as_not_yet_a_route(self) -> None:
        draft = build_draft(
            "shop.example",
            [
                ObservedPage(
                    url="https://shop.example/rankings",
                    content_kind=ContentKind.HTML,
                    status=200,
                    body_bytes=3_000,
                )
            ],
            routes=[
                DiscoveredRoute(id="maybe-api", url="https://shop.example/api/x", distinct_pages=1)
            ],
        )
        self.assertTrue(any("not validated" in q for q in draft.open_questions))

    def test_a_draft_never_invents_a_selector(self) -> None:
        """A guessed selector is worse than an empty one: it gets trusted."""

        draft = build_draft(
            "shop.example",
            [
                ObservedPage(
                    url="https://shop.example/api/rankings",
                    content_kind=ContentKind.JSON,
                    status=200,
                    body_bytes=1_000,
                )
            ],
            wanted_fields=("score",),
        )
        rendered = draft.render()
        self.assertIn("TODO", rendered)


class DomRedesignSurvivalScenario(unittest.TestCase):
    """The markup is rewritten. Nothing is lost. That is the whole design."""

    def setUp(self) -> None:
        self.profile = load_profile(PACKAGE / "profile.yaml")
        self.corpus = load_corpus(PACKAGE / "corpus.yaml")

    def test_the_shipped_profile_passes_its_own_corpus(self) -> None:
        outcomes = run_corpus(self.profile, self.corpus, fixtures_root=FIXTURES)
        failed = [o for o in outcomes if not o.passed]
        self.assertEqual(failed, [], f"failing: {[(o.case_id, o.detail) for o in failed]}")

    def test_a_renamed_css_class_does_not_lose_the_critical_field(self) -> None:
        """The layout-variant fixture renames the class; JSON-LD carries on."""

        case = next(c for c in self.corpus.cases if c.id == "article-layout-variant")
        outcome = run_case(
            case, load_fixture(FIXTURES / case.fixture), self.profile.url_classes["article"]
        )
        self.assertTrue(outcome.passed)
        self.assertTrue(outcome.fields_found["title"])

    def test_breaking_the_dom_entirely_still_leaves_the_json_route_working(self) -> None:
        """Two classes, one broken source: the JSON class is untouched."""

        broken_html = b"<html><body><div>nothing recognisable</div></body></html>"
        article = self.profile.url_classes["article"]
        from web_scraper.triage import classify_response

        html_verdict = classify_response(
            status=200,
            body=broken_html,
            headers={"Content-Type": "text/html"},
            rules=article.content_rules(),
        )
        self.assertIsNot(html_verdict.verdict.value, "OK")

        rankings_case = next(c for c in self.corpus.cases if c.id == "rankings-normal")
        rankings = run_case(
            rankings_case,
            load_fixture(FIXTURES / rankings_case.fixture),
            self.profile.url_classes["rankings"],
        )
        self.assertTrue(rankings.passed, "the JSON route must be unaffected by the markup")

    def test_the_package_certifies_and_records_what_it_was_certified_on(self) -> None:
        outcomes = run_corpus(self.profile, self.corpus, fixtures_root=FIXTURES)
        mutations = [
            MutationOutcome(
                name=run.mutation.name,
                expectation=run.mutation.expectation.value,
                observed=run.observed.value,
                passed=run.passed,
                advisory=run.is_advisory,
            )
            for run in run_profile_mutations(self.profile, self.corpus, fixtures_root=FIXTURES)
        ]
        report = certify(self.profile, self.corpus, outcomes, mutations=mutations)
        self.assertTrue(report.verdict.may_activate, report.describe())
        # And it says out loud what it could not prove.
        self.assertTrue(report.warnings)


class SchemaChangeScenario(unittest.TestCase):
    """The structure itself changes. The field really is gone."""

    def setUp(self) -> None:
        self.profile = load_profile(PACKAGE / "profile.yaml")
        self.corpus = load_corpus(PACKAGE / "corpus.yaml")

    def _degraded_registry(self, tmp: str) -> ProfileRegistry:
        registry = ProfileRegistry(root=Path(tmp))
        registry.upsert(
            RegistryEntry(
                domain="example.test",
                path="example.test/profile.yaml",
                state=ProfileState.CERTIFIED,
                profile_version=1,
                last_known_good=LastKnownGood(
                    profile_version=1,
                    profile_hash="sha256:known-good",
                    certified_at="2026-08-20T00:00:00Z",
                    evidence_hash="sha256:evidence",
                    verdict=Verdict.CERTIFIED.value,
                    warnings=0,
                ),
            )
        )
        return registry

    def test_a_critical_field_disappearing_from_the_api_fails_the_case(self) -> None:
        renamed = json.dumps([{"specialisation": "frost", "score": 91.2}]).encode()
        case = next(c for c in self.corpus.cases if c.id == "rankings-normal")
        fixture = load_fixture(FIXTURES / case.fixture)
        from web_scraper.profile_engineering.acceptance import Fixture

        outcome = run_case(
            case,
            Fixture(name=case.fixture, status=200, headers=fixture.headers, body=renamed),
            self.profile.url_classes["rankings"],
        )
        self.assertFalse(outcome.passed)

    def test_sustained_loss_degrades_the_profile_but_not_on_the_first_run(self) -> None:
        one_bad_run = [RunSample("r1", 100, 40, 100, 40)]
        self.assertFalse(assess_health("example.test", one_bad_run).should_degrade_profile)

        sustained = [RunSample(f"r{i}", 100, 40, 100, 40) for i in range(4)]
        report = assess_health("example.test", sustained)
        self.assertTrue(report.should_degrade_profile)

    def test_a_repair_candidate_is_generated_and_does_not_activate(self) -> None:
        candidate = propose_repairs(
            "example.test",
            1,
            [BrokenField("rankings", "spec", "critical", "json", "[*].spec", 0.0)],
            validated_routes=[
                {
                    "id": "rankings-v2",
                    "state": "VALIDATED",
                    "distinct_pages": 5,
                    "fields": ["spec", "score"],
                }
            ],
        )
        self.assertIs(candidate.proposals[0].kind, RepairKind.MIGRATE_TO_STRUCTURED_ROUTE)
        self.assertFalse(candidate.is_activatable)

    def test_the_trusted_version_stays_active_until_the_candidate_earns_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._degraded_registry(tmp)
            entry = registry.get("example.test")
            assert entry is not None

            # The candidate has not been certified at all.
            allowed, why = may_replace_last_known_good(None, entry.last_known_good)
            self.assertFalse(allowed)
            self.assertIn("not been certified", why)

            # Degrading the profile does NOT touch what is trusted.
            registry.upsert(
                RegistryEntry(
                    domain=entry.domain,
                    path=entry.path,
                    state=transition(entry.state, ProfileState.DEGRADED),
                    profile_version=entry.profile_version,
                    last_known_good=entry.last_known_good,
                )
            )
            registry.save()

            reloaded = ProfileRegistry.load(tmp).get("example.test")
            assert reloaded is not None and reloaded.last_known_good is not None
            self.assertIs(reloaded.state, ProfileState.DEGRADED)
            self.assertEqual(reloaded.last_known_good.profile_hash, "sha256:known-good")

    def test_a_worse_candidate_never_replaces_the_trusted_version(self) -> None:
        from web_scraper.profile_engineering.certification import (
            CertificationReport,
            Check,
            Severity,
        )

        incumbent = LastKnownGood(1, "h", "t", "e", Verdict.CERTIFIED.value, warnings=0)
        candidate = CertificationReport(
            domain="example.test",
            verdict=Verdict.CERTIFIED_WITH_WARNINGS,
            checks=(Check("quorum", False, "no second source", Severity.WARNING),),
        )
        allowed, why = may_replace_last_known_good(candidate, incumbent)
        self.assertFalse(allowed)
        self.assertIn("regression", why)


class PolicyScenario(unittest.TestCase):
    def test_a_corpus_declares_why_pagination_is_not_tested(self) -> None:
        corpus = load_corpus(PACKAGE / "corpus.yaml")
        excused = corpus.excused("article", CaseKind.PAGINATION)
        self.assertIsNotNone(excused)
        assert excused is not None
        self.assertIn("single page", excused.reason)

    def test_every_class_in_the_shipped_package_has_a_negative_case(self) -> None:
        corpus = load_corpus(PACKAGE / "corpus.yaml")
        profile = load_profile(PACKAGE / "profile.yaml")
        for name in profile.url_classes:
            self.assertTrue(corpus.negative_cases(name), f"{name} has no negative case")


if __name__ == "__main__":
    unittest.main()
