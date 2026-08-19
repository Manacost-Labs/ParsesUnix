"""The one place money leaves. Every refusal is tested explicitly."""

from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.budget import BudgetLedger
from web_scraper.budget_state import BudgetState, ReservationState
from web_scraper.contracts import ContentRules, Verdict
from web_scraper.providers.base import (
    ProviderCost,
    ProviderError,
    ProviderErrorKind,
    ProviderResponse,
)
from web_scraper.providers.breaker import ProviderBreakers
from web_scraper.providers.escalation import PaidEscalator
from web_scraper.providers.router import PaidProviderRouter
from web_scraper.providers.scrape_do import STRATEGIES

DOMAIN, URL_CLASS = "x.example", "page"
URL = "https://x.example/article"
GOOD_BODY = b"<html><body><article><h1>Title</h1>" + b"word " * 200 + b"</article></body></html>"


class FakeProvider:
    """A provider whose answer each call is scripted."""

    name = "scrape.do"

    def __init__(
        self, *, response: ProviderResponse | None = None, error: ProviderError | None = None
    ):
        self._response, self._error = response, error
        self.calls: list[str] = []

    def strategies(self):
        return STRATEGIES

    def fetch(self, request):
        self.calls.append(request.strategy_id)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def response(
    *,
    target_status: int = 200,
    body: bytes = GOOD_BODY,
    cost: str | None = "1",
    strategy_id: str = "normal",
) -> ProviderResponse:
    return ProviderResponse(
        provider="scrape.do",
        strategy_id=strategy_id,
        target_status=target_status,
        provider_status=200,
        body=body,
        headers={"Content-Type": "text/html"},
        cost=ProviderCost.parse(cost) if cost is not None else ProviderCost.unattributed(),
        request_id="req-1",
    )


class EscalatorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.budget = BudgetLedger(Path(self.tempdir.name) / "b.sqlite3", daily_credit_limit="100")
        self.breakers = ProviderBreakers(threshold=3)

    def escalator(self, provider: FakeProvider) -> PaidEscalator:
        return PaidEscalator(
            provider,
            budget=self.budget,
            router=PaidProviderRouter(stats=None, _rng=lambda: 1.0),
            breakers=self.breakers,
        )

    def attempt(self, provider: FakeProvider, *, verdict: Verdict = Verdict.BLOCKED, **kwargs):
        return self.escalator(provider).attempt(
            URL, verdict=verdict, domain=DOMAIN, url_class=URL_CLASS, **kwargs
        )


class PolicyRefusalTests(EscalatorCase):
    """Verdicts that must never reach a provider, whatever else is true."""

    def test_a_dead_url_is_never_paid_for(self) -> None:
        # Measured live: a 404 through scrape.do still costs a credit.
        provider = FakeProvider(response=response())
        outcome = self.attempt(provider, verdict=Verdict.DEAD_URL)
        self.assertFalse(outcome.attempted)
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.budget.usage().credits, Decimal("0"))

    def test_an_origin_outage_is_never_paid_for(self) -> None:
        provider = FakeProvider(response=response())
        self.assertFalse(self.attempt(provider, verdict=Verdict.ORIGIN_DOWN).attempted)
        self.assertEqual(provider.calls, [])

    def test_a_parse_failure_is_never_paid_for(self) -> None:
        provider = FakeProvider(response=response())
        self.assertFalse(self.attempt(provider, verdict=Verdict.PARSE_FAIL).attempted)
        self.assertEqual(provider.calls, [])

    def test_a_client_rendered_page_is_never_paid_for(self) -> None:
        # Rendering is our own job; CSR is not evidence of blocking.
        provider = FakeProvider(response=response())
        self.assertFalse(self.attempt(provider, verdict=Verdict.CSR_REQUIRED).attempted)

    def test_an_auth_wall_is_never_paid_for(self) -> None:
        provider = FakeProvider(response=response())
        self.assertFalse(self.attempt(provider, verdict=Verdict.AUTH_REQUIRED).attempted)

    def test_only_block_verdicts_reach_the_provider(self) -> None:
        for verdict in (Verdict.BLOCKED, Verdict.SOFT_BLOCK):
            with self.subTest(verdict=verdict):
                provider = FakeProvider(response=response())
                self.assertTrue(self.attempt(provider, verdict=verdict).attempted)


class BudgetGateTests(EscalatorCase):
    def test_an_exhausted_budget_stops_paid_work(self) -> None:
        self.budget.settle(
            self.budget.reserve(provider="scrape.do", credits=100), actual_credits=100
        )
        provider = FakeProvider(response=response())
        outcome = self.attempt(provider)
        self.assertFalse(outcome.attempted)
        self.assertIn("EXHAUSTED", outcome.reason)
        self.assertEqual(provider.calls, [])

    def test_an_unresolved_incident_stops_paid_work(self) -> None:
        held = self.budget.reserve(provider="scrape.do", credits=5)
        self.budget.mark_submitted(held)
        self.budget.settle(held, actual_credits=None)  # unknown spend
        self.assertEqual(self.budget.state(), BudgetState.UNKNOWN_SPEND)

        provider = FakeProvider(response=response())
        outcome = self.attempt(provider)
        self.assertFalse(outcome.attempted)
        self.assertEqual(provider.calls, [])

    def test_the_worst_case_is_held_not_the_typical_cost(self) -> None:
        provider = FakeProvider(response=response(cost="1"))
        outcome = self.attempt(provider)
        # normal: nominal 1, reservation 3
        self.assertEqual(outcome.reserved, Decimal("3"))
        self.assertEqual(outcome.actual_cost, Decimal("1"))
        self.assertEqual(self.budget.usage().credits, Decimal("1"), "settled at the real cost")
        self.assertEqual(self.budget.held_credits(), Decimal("0"))

    def test_the_request_is_marked_submitted_before_the_answer(self) -> None:
        """A crash while waiting must look like possible spend, not like nothing.

        Checked by observing the ledger from inside the provider call: at that
        moment the request has left, and the reservation must already say so.
        """

        observed: list[str] = []

        class ObservingProvider(FakeProvider):
            def fetch(inner, request):
                # This is exactly where a crash would happen.
                held = self.budget.open_reservations()
                observed.extend(r.state.value for r in held)
                return super().fetch(request)

        self.attempt(ObservingProvider(response=response()))
        self.assertEqual(
            observed,
            [ReservationState.SUBMITTED.value],
            "the hold must be SUBMITTED while the request is in flight",
        )


class ProviderFailureTests(EscalatorCase):
    def test_a_provider_error_becomes_unknown_spend_not_a_refund(self) -> None:
        # We were never told whether it was billed. Releasing would under-count.
        provider = FakeProvider(
            error=ProviderError(
                kind=ProviderErrorKind.TIMEOUT, message="timed out", provider="scrape.do"
            )
        )
        outcome = self.attempt(provider)
        self.assertTrue(outcome.attempted)
        self.assertTrue(outcome.unknown_spend)
        self.assertEqual(self.budget.held_credits(), Decimal("3"), "the hold stays")
        self.assertEqual(self.budget.state(), BudgetState.UNKNOWN_SPEND)

    def test_a_missing_cost_header_is_unknown_spend(self) -> None:
        provider = FakeProvider(response=response(cost=None))
        outcome = self.attempt(provider)
        self.assertTrue(outcome.unknown_spend)
        self.assertEqual(self.budget.state(), BudgetState.UNKNOWN_SPEND)

    def test_repeated_timeouts_trip_only_that_strategy(self) -> None:
        error = ProviderError(
            kind=ProviderErrorKind.TIMEOUT, message="timed out", provider="scrape.do"
        )
        for _ in range(3):
            self.breakers.record_error("scrape.do", "normal", error.kind)
        self.assertTrue(self.breakers.is_open("scrape.do", "normal"))
        self.assertFalse(self.breakers.is_open("scrape.do", "super"))

    def test_bad_credentials_trip_the_whole_provider(self) -> None:
        self.breakers.record_error("scrape.do", "normal", ProviderErrorKind.AUTH)
        provider = FakeProvider(response=response())
        outcome = self.attempt(provider)
        self.assertFalse(outcome.attempted)
        self.assertIn("circuit breaker", outcome.reason)


class ValidationTests(EscalatorCase):
    def test_a_provider_200_carrying_a_challenge_is_not_success(self) -> None:
        challenge = b"<html><title>Just a moment...</title>checking your browser</html>"
        provider = FakeProvider(response=response(body=challenge))
        outcome = self.attempt(provider)
        self.assertTrue(outcome.attempted)
        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.triage.verdict, Verdict.SOFT_BLOCK)

    def test_a_validated_body_is_a_success(self) -> None:
        provider = FakeProvider(response=response())
        outcome = self.attempt(provider, rules=ContentRules(min_body_bytes=100, canary="<article"))
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.triage.verdict, Verdict.OK)

    def test_the_target_status_is_judged_not_the_envelope(self) -> None:
        # The provider succeeded; the site returned 404.
        provider = FakeProvider(response=response(target_status=404, body=b"gone"))
        outcome = self.attempt(provider)
        self.assertEqual(outcome.triage.verdict, Verdict.DEAD_URL)
        self.assertFalse(outcome.succeeded)

    def test_the_decision_is_explainable(self) -> None:
        provider = FakeProvider(response=response())
        outcome = self.attempt(provider)
        explanation = outcome.decision.explain()
        self.assertIn("escalation verdict: BLOCKED", explanation)
        self.assertIn("normal", explanation)


if __name__ == "__main__":
    unittest.main()
