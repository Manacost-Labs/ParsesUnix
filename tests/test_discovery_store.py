"""Evidence that outlives the run that found it.

Within one run the collector already refuses to validate an endpoint seen once.
But a scheduled crawl is many runs, and evidence that dies with the process
means the threshold is approached and forgotten nightly — the system learns the
same thing over and over and never gets to act on it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.discovery import (
    CandidateVerdict,
    DiscoveryStore,
    EvidenceState,
    PaginationHint,
    RouteCandidate,
    SchemaSignature,
    evidence_to_candidate,
    page_fingerprint,
    profile_route_draft,
)

DOMAIN = "site.example"
SCHEMA = SchemaSignature.of({"data": {"players": [{"name": "x", "score": 1}]}})


def candidate(**kw) -> RouteCandidate:
    base = {
        "url": "https://site.example/api/stats?page=1",
        "method": "GET",
        "status": 200,
        "content_type": "application/json",
        "schema": SCHEMA,
        "observed_count": 1,
        "verdict": CandidateVerdict.PROMISING,
        "matched_fields": {"name": "data.players[*].name"},
    }
    base.update(kw)
    return RouteCandidate(**base)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.path = Path(tempdir.name) / "discovery.sqlite3"
        self.clock = [1_000_000.0]

    def store(self, **kw) -> DiscoveryStore:
        return DiscoveryStore(self.path, now=lambda: self.clock[0], **kw)

    def observe(self, store, pages, **kw):
        return store.record(candidate(**kw), domain=DOMAIN, url_class="page", source_pages=pages)


class AccumulationTests(StoreCase):
    def test_evidence_grows_across_separate_runs(self) -> None:
        # Each store instance is a separate process.
        first = self.observe(self.store(), ["https://site.example/a"])
        self.assertIs(first.state, EvidenceState.PROMISING)
        self.assertEqual(first.distinct_pages, 1)

        second = self.observe(self.store(), ["https://site.example/b"])
        self.assertEqual(second.distinct_pages, 2)
        self.assertIs(second.state, EvidenceState.PROMISING)

        third = self.observe(self.store(), ["https://site.example/c"])
        self.assertEqual(third.distinct_pages, 3)
        self.assertIs(third.state, EvidenceState.VALIDATED, "the threshold was reached")

    def test_a_single_run_cannot_validate_on_one_page(self) -> None:
        evidence = self.observe(self.store(), ["https://site.example/only"])
        self.assertIs(evidence.state, EvidenceState.PROMISING)

    def test_observation_counts_accumulate(self) -> None:
        self.observe(self.store(), ["https://site.example/a"], observed_count=4)
        second = self.observe(self.store(), ["https://site.example/b"], observed_count=6)
        self.assertEqual(second.observation_count, 10)


class DiversityTests(StoreCase):
    """Ten renders of one page are one piece of evidence."""

    def test_the_same_page_seen_repeatedly_never_validates(self) -> None:
        store = self.store()
        for _ in range(10):
            evidence = self.observe(store, ["https://site.example/same"])
        self.assertEqual(evidence.distinct_pages, 1)
        self.assertIs(evidence.state, EvidenceState.PROMISING)

    def test_distinct_pages_are_what_count(self) -> None:
        store = self.store()
        evidence = self.observe(
            store,
            ["https://site.example/a", "https://site.example/b", "https://site.example/c"],
        )
        self.assertEqual(evidence.distinct_pages, 3)
        self.assertIs(evidence.state, EvidenceState.VALIDATED)

    def test_the_threshold_is_configurable(self) -> None:
        store = self.store(min_distinct_pages=2)
        evidence = self.observe(store, ["https://site.example/a", "https://site.example/b"])
        self.assertIs(evidence.state, EvidenceState.VALIDATED)


class SchemaChangeTests(StoreCase):
    def test_a_changed_schema_retires_a_validated_verdict(self) -> None:
        # Carrying the old verdict forward is how a profile keeps reading a
        # field that moved.
        store = self.store()
        self.observe(
            store, ["https://site.example/a", "https://site.example/b", "https://site.example/c"]
        )
        self.assertIs(store.get(candidate().identity).state, EvidenceState.VALIDATED)

        changed = SchemaSignature.of({"totally": {"different": "shape"}})
        after = self.observe(store, ["https://site.example/d"], schema=changed)
        self.assertIs(after.state, EvidenceState.REVALIDATION_REQUIRED)
        self.assertEqual(after.schema_changes, 1)
        self.assertIn("schema changed", after.rejection_detail)

    def test_a_stable_schema_keeps_the_verdict(self) -> None:
        store = self.store()
        pages = ["https://site.example/a", "https://site.example/b", "https://site.example/c"]
        self.observe(store, pages)
        after = self.observe(store, ["https://site.example/d"])
        self.assertIs(after.state, EvidenceState.VALIDATED)
        self.assertEqual(after.schema_changes, 0)


class DecayTests(StoreCase):
    def test_confidence_falls_with_age(self) -> None:
        # An endpoint validated in March is not validated today because it once
        # was.
        store = self.store()
        pages = [f"https://site.example/{i}" for i in range(4)]
        self.observe(store, pages)
        evidence = store.get(candidate().identity)
        assert evidence is not None

        fresh = evidence.confidence(now=self.clock[0])
        aged = evidence.confidence(now=self.clock[0] + 365 * 86400)
        self.assertEqual(fresh, "HIGH")
        self.assertIn(aged, {"LOW", "MEDIUM"})
        self.assertLess(evidence.decay_factor(now=self.clock[0] + 365 * 86400), 0.05)

    def test_a_rejected_candidate_has_no_confidence_at_all(self) -> None:
        store = self.store()
        evidence = self.observe(
            store,
            ["https://site.example/a"],
            verdict=CandidateVerdict.REJECTED_AUTH,
            rejection_detail="needed a cookie",
        )
        self.assertIs(evidence.state, EvidenceState.REJECTED)
        self.assertEqual(evidence.confidence(now=self.clock[0]), "NONE")


class SecrecyTests(StoreCase):
    """The store is read by operators and copied into drafts."""

    def test_no_response_body_or_value_is_persisted(self) -> None:
        store = self.store()
        self.observe(store, ["https://site.example/a"])
        stored = Path(self.path).read_bytes()
        for secret in (b"Thrall", b"secret", b"Bearer", b"Cookie"):
            self.assertNotIn(secret, stored)

    def test_source_pages_are_stored_as_hashes(self) -> None:
        # A URL can carry a session id in its query.
        store = self.store()
        self.observe(store, ["https://site.example/user?session=SECRETVALUE"])
        stored = Path(self.path).read_bytes()
        self.assertNotIn(b"SECRETVALUE", stored)
        self.assertIn(
            page_fingerprint("https://site.example/user?session=SECRETVALUE").encode(), stored
        )

    def test_the_schema_signature_is_shape_only(self) -> None:
        store = self.store()
        self.observe(store, ["https://site.example/a"])
        evidence = store.get(candidate().identity)
        assert evidence is not None and evidence.schema_signature is not None
        self.assertIn("string", evidence.schema_signature)
        self.assertNotIn("Thrall", evidence.schema_signature)


class BoundsTests(StoreCase):
    def test_page_hashes_are_capped_per_candidate(self) -> None:
        from web_scraper.discovery.store import MAX_PAGE_HASHES

        store = self.store()
        self.observe(store, [f"https://site.example/{i}" for i in range(MAX_PAGE_HASHES + 50)])
        evidence = store.get(candidate().identity)
        assert evidence is not None
        self.assertLessEqual(evidence.distinct_pages, MAX_PAGE_HASHES)

    def test_candidates_are_capped_per_domain(self) -> None:
        # A chatty application must not fill the store on its own.
        from web_scraper.discovery.store import MAX_CANDIDATES_PER_DOMAIN

        store = self.store()
        for i in range(MAX_CANDIDATES_PER_DOMAIN + 20):
            store.record(
                candidate(url=f"https://site.example/api/e{i}"),
                domain=DOMAIN,
                source_pages=["https://site.example/a"],
            )
        self.assertLessEqual(len(store.all_evidence(domain=DOMAIN)), MAX_CANDIDATES_PER_DOMAIN)

    def test_stale_evidence_is_pruned(self) -> None:
        # A route nobody has seen in three months is history, not evidence.
        store = self.store()
        self.observe(store, ["https://site.example/a"])
        self.assertEqual(len(store.all_evidence()), 1)

        self.clock[0] += 200 * 86400
        report = self.store().prune()
        self.assertEqual(report["pruned"], 1)
        self.assertEqual(len(self.store().all_evidence()), 0)

    def test_recent_evidence_survives_pruning(self) -> None:
        store = self.store()
        self.observe(store, ["https://site.example/a"])
        self.clock[0] += 5 * 86400
        self.store().prune()
        self.assertEqual(len(self.store().all_evidence()), 1)


class DraftTests(StoreCase):
    def test_validated_evidence_becomes_a_reviewable_draft(self) -> None:
        store = self.store()
        self.observe(
            store,
            ["https://site.example/a", "https://site.example/b", "https://site.example/c"],
            pagination=PaginationHint(strategy="CURSOR", cursor_field="next"),
        )
        evidence = store.validated()[0]
        draft = profile_route_draft(evidence_to_candidate(evidence))
        self.assertEqual(draft["suggested_route"]["level"], "L0")
        self.assertEqual(draft["extractor"]["fields"]["name"], "data.players[*].name")
        self.assertIn("Proposed, not applied", draft["review"])

    def test_promising_evidence_cannot_become_a_draft(self) -> None:
        store = self.store()
        self.observe(store, ["https://site.example/only"])
        evidence = store.get(candidate().identity)
        assert evidence is not None
        with self.assertRaises(ValueError):
            profile_route_draft(evidence_to_candidate(evidence))


class ReportingTests(StoreCase):
    def test_the_summary_counts_what_an_operator_needs(self) -> None:
        store = self.store()
        self.observe(
            store, ["https://site.example/a", "https://site.example/b", "https://site.example/c"]
        )
        store.record(
            candidate(url="https://site.example/api/me", verdict=CandidateVerdict.REJECTED_AUTH),
            domain=DOMAIN,
            source_pages=["https://site.example/a"],
        )
        summary = store.summary(now=self.clock[0])
        self.assertEqual(summary["discovery_candidates_total"], 2)
        self.assertEqual(summary["discovery_validated_total"], 1)
        self.assertEqual(summary["discovery_rejected_total"], 1)
        self.assertEqual(len(summary["validated"]), 1)

    def test_evidence_survives_a_restart(self) -> None:
        self.observe(
            self.store(),
            ["https://site.example/a", "https://site.example/b", "https://site.example/c"],
        )
        restarted = self.store()
        self.assertEqual(len(restarted.validated()), 1)
        self.assertIs(restarted.get(candidate().identity).state, EvidenceState.VALIDATED)


if __name__ == "__main__":
    unittest.main()
