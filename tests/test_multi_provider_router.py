"""Choosing across vendors, and the arithmetic that makes it worth doing."""

from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.contracts import Cost, Verdict
from web_scraper.providers.base import ProviderStrategy
from web_scraper.providers.breaker import ProviderBreakers
from web_scraper.providers.multi_router import MultiProviderRouter
from web_scraper.providers.stats import ProviderStatsStore, ProviderStrategyKey

DOMAIN, URL_CLASS = "site.example", "page"


def strategy(sid, cost, *, render=False, premium=True):
    return ProviderStrategy(
        id=sid,
        nominal_cost=Decimal(str(cost)),
        renders_javascript=render,
        premium_network=premium,
    )


class FakeProvider:
    def __init__(self, name, strategies):
        self.name, self._strategies = name, strategies

    def strategies(self):
        return self._strategies

    def fetch(self, request):  # pragma: no cover - routing tests never call
        raise AssertionError("the router must not fetch")


class RouterCase(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.stats = ProviderStatsStore(Path(tempdir.name) / "p.sqlite3")

    def observe(self, provider, sid, *, ok: int, fail: int, cost="1") -> None:
        key = ProviderStrategyKey(
            provider=provider, strategy_id=sid, domain=DOMAIN, url_class=URL_CLASS
        )
        for _ in range(ok):
            self.stats.record(key, verdict=Verdict.OK, cost=Cost.of(cost))
        for _ in range(fail):
            self.stats.record(key, verdict=Verdict.BLOCKED, cost=Cost.of(cost))

    def router(self, providers, **kw) -> MultiProviderRouter:
        kw.setdefault("_rng", lambda: 1.0)  # no shadow probing unless asked
        return MultiProviderRouter(providers=providers, stats=self.stats, **kw)


class CostPerValidResultTests(RouterCase):
    """The brief's own example, asserted as arithmetic."""

    def test_the_cheaper_list_price_can_be_the_more_expensive_choice(self) -> None:
        # A: 1 credit, validates half the time -> 2 per usable result.
        # B: 1.5 credits, validates ~always    -> ~1.52 per usable result.
        cheap = FakeProvider("cheap", (strategy("normal", "1"),))
        steady = FakeProvider("steady", (strategy("normal", "1.5"),))
        self.observe("cheap", "normal", ok=25, fail=25, cost="1")
        self.observe("steady", "normal", ok=49, fail=1, cost="1.5")

        decision = self.router([cheap, steady]).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertEqual(decision.provider, "steady", "ranked on usable results, not list price")

        by_ref = {c.ref: c for c in decision.candidates}
        self.assertEqual(by_ref["cheap:normal"].expected_cost, Decimal("2.00"))
        self.assertAlmostEqual(float(by_ref["steady:normal"].expected_cost), 1.53, places=2)

    def test_cost_per_valid_result_is_none_until_something_succeeds(self) -> None:
        self.observe("p", "s", ok=0, fail=5, cost="1")
        record = self.stats.get(
            ProviderStrategyKey(provider="p", strategy_id="s", domain=DOMAIN, url_class=URL_CLASS)
        )
        assert record is not None
        self.assertIsNone(record.cost_per_valid_result, "no valid results: not a number")

    def test_unattributed_spend_makes_cost_per_valid_result_unknowable(self) -> None:
        key = ProviderStrategyKey(provider="p", strategy_id="s", domain=DOMAIN, url_class=URL_CLASS)
        self.stats.record(key, verdict=Verdict.OK, cost=Cost.of("5"))
        self.stats.record(key, verdict=Verdict.OK, cost=Cost.unknown())
        record = self.stats.get(key)
        assert record is not None
        self.assertFalse(record.cost_is_complete)
        self.assertIsNone(record.cost_per_valid_result, "a floor divided by a count is not a cost")
        self.assertEqual(record.known_cost, Decimal("5"), "the known part is still reported")


class SelectionTests(RouterCase):
    def test_a_strategy_below_the_confidence_bound_is_refused(self) -> None:
        weak = FakeProvider("weak", (strategy("normal", "1"),))
        strong = FakeProvider("strong", (strategy("normal", "10"),))
        self.observe("weak", "normal", ok=2, fail=18)
        self.observe("strong", "normal", ok=40, fail=0, cost="10")

        decision = self.router([weak, strong]).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertEqual(decision.provider, "strong")
        weak_candidate = next(c for c in decision.candidates if c.provider == "weak")
        self.assertFalse(weak_candidate.eligible)
        self.assertIn("below", weak_candidate.reason)

    def test_expensive_is_never_chosen_for_being_expensive(self) -> None:
        cheap = FakeProvider("cheap", (strategy("normal", "1"),))
        dear = FakeProvider("dear", (strategy("normal", "40"),))
        self.observe("cheap", "normal", ok=40, fail=0, cost="1")
        self.observe("dear", "normal", ok=40, fail=0, cost="40")

        decision = self.router([cheap, dear]).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertEqual(decision.provider, "cheap")

    def test_a_capability_that_cannot_help_is_not_paid_for(self) -> None:
        # Rendering does not defeat a hard refusal at the edge.
        renderer = FakeProvider("r", (strategy("render", "5", render=True, premium=False),))
        decision = self.router([renderer]).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertFalse(decision.chosen)
        self.assertIn("does not address", decision.candidates[0].reason)

    def test_a_tripped_breaker_removes_a_candidate_at_any_price(self) -> None:
        from web_scraper.providers.base import ProviderErrorKind

        # A frozen clock, because the default one made this test flaky: on a
        # loaded machine the cooldown elapsed between tripping the breaker and
        # asserting, the breaker went half-open, and the candidate reappeared.
        # The test is about price never overriding health, not about timing.
        breakers = ProviderBreakers(threshold=1, clock=lambda: 1000.0)
        breakers.record_error("cheap", "normal", ProviderErrorKind.TIMEOUT)

        cheap = FakeProvider("cheap", (strategy("normal", "1"),))
        other = FakeProvider("other", (strategy("normal", "10"),))
        self.observe("cheap", "normal", ok=40, fail=0)
        self.observe("other", "normal", ok=40, fail=0, cost="10")

        decision = self.router([cheap, other], breakers=breakers).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertEqual(decision.provider, "other")

    def test_nothing_eligible_means_the_url_stays_unresolved(self) -> None:
        weak = FakeProvider("weak", (strategy("normal", "1"),))
        self.observe("weak", "normal", ok=0, fail=20)
        decision = self.router([weak]).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertFalse(decision.chosen)
        self.assertIn("stays unresolved", decision.explain())


class ColdStartTests(RouterCase):
    def test_an_untried_strategy_is_explored_not_trusted(self) -> None:
        fresh = FakeProvider("fresh", (strategy("normal", "1"),))
        decision = self.router([fresh]).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        candidate = decision.candidates[0]
        self.assertTrue(candidate.exploring)
        self.assertEqual(candidate.confidence, 0.0, "no history is not 100% reliable")
        self.assertIn("exploring", candidate.reason)

    def test_exploration_is_capped_by_calls(self) -> None:
        # Otherwise a strategy that always fails is retried forever, because it
        # never gathers enough evidence to be rejected.
        fresh = FakeProvider("fresh", (strategy("normal", "1"),))
        key = ProviderStrategyKey(
            provider="fresh", strategy_id="normal", domain=DOMAIN, url_class=URL_CLASS
        )
        for _ in range(10):
            self.stats.record(key, provider_error=True, cost=Cost.of("1"))

        router = self.router([fresh], min_observations=50, max_exploration_calls=10)
        decision = router.choose(domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED)
        self.assertFalse(decision.chosen)
        self.assertIn("exploration budget spent", decision.candidates[0].reason)

    def test_exploration_is_capped_by_credits(self) -> None:
        dear = FakeProvider("dear", (strategy("browser", "40"),))
        key = ProviderStrategyKey(
            provider="dear", strategy_id="browser", domain=DOMAIN, url_class=URL_CLASS
        )
        self.stats.record(key, verdict=Verdict.BLOCKED, cost=Cost.of("60"))

        router = self.router([dear], min_observations=50, max_exploration_credits=Decimal("50"))
        decision = router.choose(domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED)
        self.assertFalse(decision.chosen)
        self.assertIn("credits", decision.candidates[0].reason)


class NeutralOutcomeTests(RouterCase):
    def test_an_origin_outage_does_not_damage_a_strategy(self) -> None:
        key = ProviderStrategyKey(provider="p", strategy_id="s", domain=DOMAIN, url_class=URL_CLASS)
        for _ in range(20):
            self.stats.record(key, verdict=Verdict.OK, cost=Cost.of("1"))
        before = self.stats.get(key)
        assert before is not None

        for _ in range(20):
            self.stats.record(key, verdict=Verdict.ORIGIN_DOWN, cost=Cost.of("1"))
        after = self.stats.get(key)
        assert after is not None

        self.assertEqual(after.confidence_bound, before.confidence_bound)
        self.assertEqual(after.neutral_outcomes, 20)
        self.assertEqual(after.scored_attempts, before.scored_attempts)

    def test_a_provider_error_does_count_against_the_strategy(self) -> None:
        key = ProviderStrategyKey(provider="p", strategy_id="s", domain=DOMAIN, url_class=URL_CLASS)
        self.stats.record(key, provider_error=True)
        record = self.stats.get(key)
        assert record is not None
        self.assertEqual(record.provider_errors, 1)
        self.assertEqual(record.scored_attempts, 1, "the strategy could have avoided this")


class ShadowProbeTests(RouterCase):
    def test_a_cheaper_rejected_option_is_occasionally_retried(self) -> None:
        cheap = FakeProvider("cheap", (strategy("normal", "1"),))
        dear = FakeProvider("dear", (strategy("normal", "20"),))
        self.observe("cheap", "normal", ok=1, fail=19)
        self.observe("dear", "normal", ok=40, fail=0, cost="20")

        always = self.router([cheap, dear], _rng=lambda: 0.0, shadow_probe_rate=0.05)
        decision = always.choose(domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED)
        self.assertTrue(decision.shadow_probe)
        self.assertEqual(decision.provider, "cheap", "re-testing the cheap door on purpose")

    def test_a_site_that_relaxes_can_get_its_cheap_door_back(self) -> None:
        """The four runs that decide whether a price rise is permanent.

        Without this loop the first expensive choice is paid forever: the cheap
        strategy is below the bar, so it is never used, so it never gathers the
        evidence that would put it back above the bar. The probe is the only
        thing that breaks that circle, and it is worth testing as the sequence
        an operator would actually live through rather than as one call.
        """

        cheap = FakeProvider("cheap", (strategy("normal", "1"),))
        dear = FakeProvider("dear", (strategy("normal", "20"),))

        bar = 0.6  # a bar 20 clean results can clear, so the scenario stays short

        # Run 1: the cheap door works and is chosen.
        self.observe("cheap", "normal", ok=20, fail=0)
        self.observe("dear", "normal", ok=20, fail=0, cost="20")
        first = self.router([cheap, dear], minimum_confidence_bound=bar).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertEqual(first.provider, "cheap")

        # Run 2: the site hardens. The cheap door falls below the bar.
        self.observe("cheap", "normal", ok=0, fail=30)
        second = self.router([cheap, dear], minimum_confidence_bound=bar).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertEqual(second.provider, "dear", "the evidence moved the traffic")

        # Run 3: a probe re-tests the cheap door despite the evidence.
        probing = self.router(
            [cheap, dear],
            minimum_confidence_bound=bar,
            _rng=lambda: 0.0,
            shadow_probe_rate=0.05,
        )
        probe = probing.choose(domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED)
        self.assertTrue(probe.shadow_probe)
        self.assertEqual(probe.provider, "cheap")

        # ...and the site has relaxed, so the probe succeeds — repeatedly.
        self.observe("cheap", "normal", ok=100, fail=0)

        # Run 4: no probe needed. The ordinary decision comes back down in price.
        fourth = self.router([cheap, dear], minimum_confidence_bound=bar).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertFalse(fourth.shadow_probe)
        self.assertEqual(fourth.provider, "cheap", "a recovered site must get cheap again")

    def test_without_a_probe_the_trusted_option_is_used(self) -> None:
        cheap = FakeProvider("cheap", (strategy("normal", "1"),))
        dear = FakeProvider("dear", (strategy("normal", "20"),))
        self.observe("cheap", "normal", ok=1, fail=19)
        self.observe("dear", "normal", ok=40, fail=0, cost="20")

        never = self.router([cheap, dear], _rng=lambda: 1.0)
        decision = never.choose(domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED)
        self.assertFalse(decision.shadow_probe)
        self.assertEqual(decision.provider, "dear")


class IdentityTests(RouterCase):
    def test_statistics_never_merge_across_providers(self) -> None:
        a = ProviderStrategyKey(
            provider="a", strategy_id="normal", domain=DOMAIN, url_class=URL_CLASS
        )
        b = ProviderStrategyKey(
            provider="b", strategy_id="normal", domain=DOMAIN, url_class=URL_CLASS
        )
        self.stats.record(a, verdict=Verdict.OK, cost=Cost.of("1"))
        self.assertIsNone(self.stats.get(b), "same strategy name, different vendor")

    def test_statistics_never_merge_across_url_classes(self) -> None:
        page = ProviderStrategyKey(
            provider="a", strategy_id="normal", domain=DOMAIN, url_class="page"
        )
        listing = ProviderStrategyKey(
            provider="a", strategy_id="normal", domain=DOMAIN, url_class="listing"
        )
        self.stats.record(page, verdict=Verdict.OK, cost=Cost.of("1"))
        self.assertIsNone(self.stats.get(listing))

    def test_the_reference_is_stable_and_readable(self) -> None:
        key = ProviderStrategyKey(
            provider="scrape_do", strategy_id="normal", domain=DOMAIN, url_class=URL_CLASS
        )
        self.assertEqual(key.strategy_ref, "scrape_do:normal")


class ExplainabilityTests(RouterCase):
    def test_the_decision_answers_why_this_vendor_at_this_price(self) -> None:
        cheap = FakeProvider("cheap", (strategy("normal", "1"),))
        dear = FakeProvider("dear", (strategy("unlocker", "20"),))
        self.observe("cheap", "normal", ok=1, fail=19)
        self.observe("dear", "unlocker", ok=40, fail=0, cost="20")

        decision = self.router([cheap, dear]).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        text = decision.explain()
        self.assertIn("escalation verdict: BLOCKED", text)
        self.assertIn("cheap:normal", text, "the rejected option is shown, with its reason")
        self.assertIn("dear:unlocker", text)
        self.assertIn("selected: dear:unlocker", text)
        self.assertIn("holding", text, "the hold is part of the explanation")


if __name__ == "__main__":
    unittest.main()


class CanonicalMoneyTests(RouterCase):
    """Ranking across vendors whose units are not the same thing."""

    def router_with_pricing(self, providers, book, **kw):
        kw.setdefault("_rng", lambda: 1.0)
        return MultiProviderRouter(providers=providers, stats=self.stats, pricing=book, **kw)

    def book(self, **rates):
        from web_scraper.providers.pricing import (
            PricingBook,
            PricingSnapshot,
            StrategyRate,
        )

        snapshots = tuple(
            PricingSnapshot(
                provider=provider,
                native_unit=unit,
                pricing_source="test",
                docs_verified_at="2026-08-19",
                effective_at="2026-08-19",
                rates={sid: StrategyRate(Decimal(str(native)), Decimal(str(usd)))},
            )
            for provider, (sid, unit, native, usd) in rates.items()
        )
        return PricingBook(snapshots)

    def test_equal_native_cost_can_mean_very_different_money(self) -> None:
        # Both charge "1 unit". One unit costs 10x the other. Ranking in native
        # units would call these equal and pick by tie-break.
        cheap = FakeProvider("cheap", (strategy("normal", "1"),))
        dear = FakeProvider("dear", (strategy("normal", "1"),))
        self.observe("cheap", "normal", ok=40, fail=0)
        self.observe("dear", "normal", ok=40, fail=0)

        book = self.book(
            cheap=("normal", "credits", "1", "0.001"),
            dear=("normal", "requests", "1", "0.010"),
        )
        decision = self.router_with_pricing([cheap, dear], book).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertEqual(decision.provider, "cheap")
        self.assertEqual(decision.estimated_usd, Decimal("0.001000"))

    def test_a_higher_native_price_can_be_the_cheaper_choice(self) -> None:
        # 10 credits at $0.0001 is cheaper than 1 request at $0.01.
        many_cheap = FakeProvider("credits_vendor", (strategy("normal", "10"),))
        few_dear = FakeProvider("request_vendor", (strategy("normal", "1"),))
        self.observe("credits_vendor", "normal", ok=40, fail=0)
        self.observe("request_vendor", "normal", ok=40, fail=0)

        book = self.book(
            credits_vendor=("normal", "credits", "10", "0.0001"),
            request_vendor=("normal", "requests", "1", "0.01"),
        )
        decision = self.router_with_pricing([many_cheap, few_dear], book).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertEqual(decision.provider, "credits_vendor", "$0.001 beats $0.01")

    def test_an_unpriced_strategy_never_wins_by_default(self) -> None:
        # "We do not know what this costs" must not sort as free.
        priced = FakeProvider("priced", (strategy("normal", "5"),))
        unpriced = FakeProvider("unpriced", (strategy("normal", "1"),))
        self.observe("priced", "normal", ok=40, fail=0)
        self.observe("unpriced", "normal", ok=40, fail=0)

        book = self.book(priced=("normal", "credits", "5", "0.001"))
        decision = self.router_with_pricing([priced, unpriced], book).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        self.assertEqual(decision.provider, "priced")
        unpriced_candidate = next(c for c in decision.candidates if c.provider == "unpriced")
        self.assertIsNone(unpriced_candidate.expected_usd)

    def test_money_is_divided_by_how_often_the_strategy_works(self) -> None:
        vendor = FakeProvider("v", (strategy("normal", "1"),))
        self.observe("v", "normal", ok=90, fail=10)
        book = self.book(v=("normal", "credits", "1", "0.001"))
        decision = self.router_with_pricing([vendor], book).choose(
            domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED
        )
        # $0.001 list / 0.9 success = $0.001111 per usable result.
        self.assertEqual(decision.estimated_usd, Decimal("0.001111"))


class EvidenceDecayTests(RouterCase):
    """A perfect record from a year ago is not a perfect record."""

    def stats_for(self, provider, sid):
        return self.stats.get(
            ProviderStrategyKey(
                provider=provider, strategy_id=sid, domain=DOMAIN, url_class=URL_CLASS
            )
        )

    def test_aged_evidence_loses_confidence_but_keeps_its_rate(self) -> None:
        self.observe("v", "normal", ok=40, fail=0)
        record = self.stats_for("v", "normal")
        assert record is not None
        fresh_bound = record.confidence_bound

        import time

        a_year_later = time.time() + 365 * 86400
        aged_bound = record.decayed_confidence_bound(now=a_year_later, half_life_days=30)

        self.assertLess(aged_bound, fresh_bound, "a year of silence is not free")
        self.assertEqual(record.success_rate, 1.0, "we still believe it worked")

    def test_recent_evidence_is_not_discounted(self) -> None:
        self.observe("v", "normal", ok=40, fail=0)
        record = self.stats_for("v", "normal")
        assert record is not None
        import time

        self.assertAlmostEqual(
            record.decay_factor(now=time.time(), half_life_days=30), 1.0, places=2
        )

    def test_one_half_life_halves_the_weight(self) -> None:
        self.observe("v", "normal", ok=10, fail=0)
        record = self.stats_for("v", "normal")
        assert record is not None
        import time

        factor = record.decay_factor(now=time.time() + 30 * 86400, half_life_days=30)
        self.assertAlmostEqual(factor, 0.5, places=2)

    def test_a_strategy_with_no_history_is_not_discounted(self) -> None:
        from web_scraper.providers.stats import ProviderStrategyStats

        empty = ProviderStrategyStats(
            key=ProviderStrategyKey(
                provider="v", strategy_id="s", domain=DOMAIN, url_class=URL_CLASS
            )
        )
        self.assertEqual(empty.decay_factor(now=1e12), 1.0, "nothing to age")

    def test_stale_evidence_can_drop_a_strategy_below_the_bar(self) -> None:
        # The point of the whole mechanism: a site has had a year to change.
        import time

        self.observe("stale", "normal", ok=12, fail=0)
        vendor = FakeProvider("stale", (strategy("normal", "1"),))

        a_year_later = time.time() + 365 * 86400
        router = MultiProviderRouter(
            providers=[vendor],
            stats=self.stats,
            clock=lambda: a_year_later,
            _rng=lambda: 1.0,
        )
        decision = router.choose(domain=DOMAIN, url_class=URL_CLASS, verdict=Verdict.BLOCKED)
        self.assertFalse(decision.chosen, "year-old proof is not proof today")
        self.assertIn("evidence", decision.candidates[0].reason)


class CapabilityMatchingTests(unittest.TestCase):
    """A strategy is appropriate when ONE of its capabilities answers the verdict.

    An earlier rule required every capability to be relevant, which rejected any
    strategy that could do more than one thing. A mode combining a premium
    network with rendering was refused for BLOCKED — the very verdict its
    premium network exists to answer — because it also happened to render.

    That made every Firecrawl mode unreachable for BLOCKED, the most common paid
    escalation verdict: the provider was configured, priced, tested and
    permanently unused. A live end-to-end call exposed it, not the suite.
    """

    def strategy(self, *, premium: bool, renders: bool) -> ProviderStrategy:
        return ProviderStrategy(
            id="s",
            nominal_cost=Decimal("1"),
            premium_network=premium,
            renders_javascript=renders,
        )

    def appropriate(self, verdict, *, premium, renders) -> bool:
        from web_scraper.providers.router import _strategy_is_appropriate

        return _strategy_is_appropriate(self.strategy(premium=premium, renders=renders), verdict)

    def test_a_combined_strategy_is_usable_for_a_block(self) -> None:
        # The regression. Its premium network answers BLOCKED; the fact that it
        # also renders is irrelevant, not disqualifying.
        self.assertTrue(self.appropriate(Verdict.BLOCKED, premium=True, renders=True))

    def test_a_combined_strategy_is_usable_for_a_csr_page(self) -> None:
        self.assertTrue(self.appropriate(Verdict.CSR_REQUIRED, premium=True, renders=True))

    def test_rendering_alone_still_does_not_answer_a_block(self) -> None:
        # Paying five credits to be refused again is the waste the rule exists
        # to prevent, and it is still prevented.
        self.assertFalse(self.appropriate(Verdict.BLOCKED, premium=False, renders=True))

    def test_a_premium_network_alone_does_not_answer_a_csr_page(self) -> None:
        self.assertFalse(self.appropriate(Verdict.CSR_REQUIRED, premium=True, renders=False))

    def test_a_plain_fetch_is_appropriate_for_anything(self) -> None:
        for verdict in (Verdict.BLOCKED, Verdict.SOFT_BLOCK, Verdict.CSR_REQUIRED):
            with self.subTest(verdict=verdict):
                self.assertTrue(self.appropriate(verdict, premium=False, renders=False))

    def test_every_firecrawl_premium_mode_can_serve_a_block(self) -> None:
        from web_scraper.providers.firecrawl import AUTO, ENHANCED
        from web_scraper.providers.router import _strategy_is_appropriate

        for strategy in (AUTO, ENHANCED):
            with self.subTest(strategy=strategy.id):
                self.assertTrue(_strategy_is_appropriate(strategy, Verdict.BLOCKED))

    def test_the_reason_names_every_capability_that_failed(self) -> None:
        from web_scraper.providers.router import _inappropriate_reason

        reason = _inappropriate_reason(self.strategy(premium=False, renders=True), Verdict.BLOCKED)
        self.assertIn("rendering", reason)
        self.assertIn("BLOCKED", reason)
