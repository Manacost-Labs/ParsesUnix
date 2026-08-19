"""The gate that stops a 100k-URL run against a site that has been redesigned."""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Attempt, Level, Result, Verdict
from web_scraper.finops.free_canary import (
    CanaryUrl,
    FreeCanary,
    FreeCanaryStatus,
    stratified_sample,
)

STRATA = ("L0", "L1", "CSR", "pagination", "unstable")


def outcome_for(verdict: Verdict, *, level=Level.L1):
    attempt = Attempt(url="u", level=level, verdict=verdict, reason=verdict.value)

    class Outcome:
        result = Result(url="u", verdict=verdict, attempts=(attempt,))

    return Outcome()


class SamplingTests(unittest.TestCase):
    def candidates(self, per_stratum=10):
        return [
            CanaryUrl(f"https://s/{stratum}/{i}", stratum)
            for stratum in STRATA
            for i in range(per_stratum)
        ]

    def test_every_stratum_is_represented(self) -> None:
        # A queue head is usually one domain and one url_class; testing only the
        # easy corner grants confidence it did not earn.
        picked = stratified_sample(self.candidates(), per_stratum=3, rng=random.Random(1))
        self.assertEqual({c.stratum for c in picked}, set(STRATA))
        self.assertEqual(len(picked), 15)

    def test_a_thin_stratum_is_taken_whole(self) -> None:
        candidates = [CanaryUrl("https://s/a", "L0"), *self.candidates()]
        picked = stratified_sample(candidates, per_stratum=3, rng=random.Random(1))
        self.assertGreaterEqual(sum(1 for c in picked if c.stratum == "L0"), 3)

    def test_it_does_not_just_take_the_head(self) -> None:
        candidates = self.candidates(per_stratum=50)
        picked = stratified_sample(candidates, per_stratum=3, rng=random.Random(7))
        self.assertNotEqual([c.url for c in picked[:3]], [c.url for c in candidates[:3]])

    def test_sampling_is_reproducible_with_a_seed(self) -> None:
        first = stratified_sample(self.candidates(), per_stratum=2, rng=random.Random(42))
        second = stratified_sample(self.candidates(), per_stratum=2, rng=random.Random(42))
        self.assertEqual([c.url for c in first], [c.url for c in second])


class JudgementTests(unittest.TestCase):
    def canary(self, verdicts, **kw):
        candidates = [
            CanaryUrl(f"https://s/{i}", STRATA[i % len(STRATA)]) for i in range(len(verdicts))
        ]
        by_url = dict(zip([c.url for c in candidates], verdicts, strict=True))
        return FreeCanary(per_stratum=10, rng=random.Random(1), **kw).run(
            candidates, fetch=lambda url: outcome_for(by_url[url])
        )

    def test_a_healthy_site_passes(self) -> None:
        result = self.canary([Verdict.OK] * 10)
        self.assertEqual(result.status, FreeCanaryStatus.PASS)
        self.assertTrue(result.status.allows_run)

    def test_a_wholesale_failure_stops_the_run(self) -> None:
        result = self.canary([Verdict.BLOCKED] * 8 + [Verdict.OK] * 2)
        self.assertEqual(result.status, FreeCanaryStatus.BLOCK_RUN)
        self.assertFalse(result.status.allows_run)

    def test_a_redesign_stops_the_run_even_without_network_failures(self) -> None:
        # Every page fetched fine and none of them parsed. That is profile drift,
        # and a full run would burn the paid budget producing garbage.
        result = self.canary([Verdict.PARSE_FAIL] * 10)
        self.assertEqual(result.status, FreeCanaryStatus.BLOCK_RUN)
        self.assertIn("profile drift, not an outage", result.explain())

    def test_an_ssr_page_becoming_client_rendered_is_caught(self) -> None:
        result = self.canary([Verdict.CSR_REQUIRED] * 10)
        self.assertEqual(result.status, FreeCanaryStatus.BLOCK_RUN)

    def test_partial_degradation_warns_without_stopping(self) -> None:
        result = self.canary([Verdict.OK] * 7 + [Verdict.BLOCKED] * 3)
        self.assertEqual(result.status, FreeCanaryStatus.WARN)
        self.assertTrue(result.status.allows_run)

    def test_one_stratum_failing_wholesale_is_flagged(self) -> None:
        # Stronger signal than the same failure count spread thinly: something
        # specific broke, and it will break for every URL of that kind.
        candidates = [
            CanaryUrl("https://s/csr1", "CSR"),
            CanaryUrl("https://s/csr2", "CSR"),
            *[CanaryUrl(f"https://s/ok{i}", "L1") for i in range(8)],
        ]
        verdicts = {
            "https://s/csr1": Verdict.CSR_REQUIRED,
            "https://s/csr2": Verdict.CSR_REQUIRED,
            **{f"https://s/ok{i}": Verdict.OK for i in range(8)},
        }
        result = FreeCanary(per_stratum=10, rng=random.Random(1)).run(
            candidates, fetch=lambda url: outcome_for(verdicts[url])
        )
        self.assertEqual(result.status, FreeCanaryStatus.WARN)
        self.assertEqual(result.broken_strata, ("CSR",))
        self.assertIn("nothing resolved in: CSR", result.explain())

    def test_a_stale_seed_list_does_not_stop_a_run(self) -> None:
        # Dead URLs say the seed list is old, not that the site changed.
        result = self.canary([Verdict.DEAD_URL] * 6 + [Verdict.OK] * 4)
        self.assertEqual(result.status, FreeCanaryStatus.PASS)
        self.assertEqual(len(result.scored), 4, "the dead six were excluded")

    def test_learning_nothing_is_not_all_clear(self) -> None:
        result = self.canary([Verdict.DEAD_URL] * 6)
        self.assertEqual(result.status, FreeCanaryStatus.WARN)
        self.assertIn("never actually exercised", result.explain())

    def test_an_empty_candidate_list_vetoes_nothing(self) -> None:
        result = FreeCanary().run([], fetch=lambda url: outcome_for(Verdict.OK))
        self.assertEqual(result.status, FreeCanaryStatus.PASS)

    def test_the_report_names_which_urls_failed_and_why(self) -> None:
        result = self.canary([Verdict.OK] * 5 + [Verdict.BLOCKED] * 5)
        text = result.explain()
        self.assertIn("FAIL", text)
        self.assertIn("BLOCKED", text)


class ThresholdTests(unittest.TestCase):
    def test_thresholds_must_be_ordered(self) -> None:
        with self.assertRaises(ValueError):
            FreeCanary(pass_rate=0.4, block_rate=0.9)


class IsolationTests(unittest.TestCase):
    def test_the_free_canary_cannot_spend(self) -> None:
        # It takes a fetch callable and a URL list. There is no budget, no
        # provider and no escalator in its signature — it cannot pay by
        # construction, not by policy.
        import inspect

        signature = inspect.signature(FreeCanary.run)
        self.assertEqual(set(signature.parameters) - {"self"}, {"candidates", "fetch"})
        source = Path(ROOT / "src/web_scraper/finops/free_canary.py").read_text()
        for forbidden in ("Budget", "Escalator", "provider.fetch", "PaidAttempt"):
            self.assertNotIn(forbidden, source, f"{forbidden} has no business here")


if __name__ == "__main__":
    unittest.main()
