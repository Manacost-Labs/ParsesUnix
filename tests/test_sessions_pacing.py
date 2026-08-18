from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.fetchers import Pacer, RawResponse, SessionPool, parse_retry_after  # noqa: E402


class CountingTransport:
    created = 0

    def __init__(self) -> None:
        type(self).created += 1
        self.fetched: list[str] = []

    def fetch(self, url: str, *, headers=None) -> RawResponse:
        self.fetched.append(url)
        return RawResponse(
            requested_url=url, final_url=url, status=200, headers={}, body=b"ok" * 200
        )


class SessionPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        CountingTransport.created = 0
        self.time = 0.0
        self.pool = SessionPool(lambda domain: CountingTransport(), clock=lambda: self.time)

    def test_session_is_reused_within_ttl(self) -> None:
        first = self.pool.get("demo.example", ttl_minutes=30)
        self.time += 60
        second = self.pool.get("demo.example", ttl_minutes=30)
        self.assertIs(first, second)
        self.assertEqual(CountingTransport.created, 1)

    def test_session_is_recreated_after_ttl(self) -> None:
        first = self.pool.get("demo.example", ttl_minutes=30)
        self.time += 31 * 60
        second = self.pool.get("demo.example", ttl_minutes=30)
        self.assertIsNot(first, second)
        self.assertEqual(CountingTransport.created, 2)

    def test_zero_ttl_disables_reuse(self) -> None:
        first = self.pool.get("demo.example", ttl_minutes=0)
        second = self.pool.get("demo.example", ttl_minutes=0)
        self.assertIsNot(first, second)

    def test_warmup_runs_once_per_session_creation(self) -> None:
        warmup = "https://demo.example/"
        session = self.pool.get("demo.example", ttl_minutes=30, warmup_url=warmup)
        self.pool.get("demo.example", ttl_minutes=30, warmup_url=warmup)
        self.assertEqual(session.fetched, [warmup])
        self.assertEqual(self.pool.warmup_urls, [warmup])

    def test_warmup_failure_does_not_break_session_creation(self) -> None:
        class FailingWarmup(CountingTransport):
            def fetch(self, url: str, *, headers=None) -> RawResponse:
                raise OSError("warmup refused")

        pool = SessionPool(lambda domain: FailingWarmup(), clock=lambda: self.time)
        session = pool.get("demo.example", ttl_minutes=30, warmup_url="https://demo.example/")
        self.assertIsNotNone(session)


class PacerTests(unittest.TestCase):
    def test_pause_enforces_min_interval_between_requests(self) -> None:
        slept: list[float] = []
        clock_value = [0.0]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock_value[0] += seconds

        pacer = Pacer(
            min_interval_s=2.0, jitter_s=0.0, sleep=sleep, clock=lambda: clock_value[0], rng=lambda: 0.0
        )
        self.assertEqual(pacer.pause("demo.example"), 0.0)  # first request: no wait
        clock_value[0] += 0.5
        self.assertAlmostEqual(pacer.pause("demo.example"), 1.5)
        self.assertEqual(len(slept), 1)

    def test_domains_are_paced_independently(self) -> None:
        slept: list[float] = []
        pacer = Pacer(min_interval_s=5.0, jitter_s=0.0, sleep=slept.append, clock=lambda: 100.0, rng=lambda: 0.0)
        pacer.pause("a.example")
        self.assertEqual(pacer.pause("b.example"), 0.0)
        self.assertEqual(slept, [])

    def test_jitter_extends_the_interval(self) -> None:
        slept: list[float] = []
        clock_value = [0.0]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock_value[0] += seconds

        pacer = Pacer(
            min_interval_s=1.0, jitter_s=1.0, sleep=sleep, clock=lambda: clock_value[0], rng=lambda: 0.5
        )
        pacer.pause("demo.example")
        self.assertAlmostEqual(pacer.pause("demo.example"), 1.5)

    def test_backoff_is_bounded_by_max_delay(self) -> None:
        slept: list[float] = []
        pacer = Pacer(max_delay_s=120.0, sleep=slept.append)
        self.assertEqual(pacer.backoff(999.0), 120.0)
        self.assertEqual(slept, [120.0])


class RetryAfterTests(unittest.TestCase):
    def test_numeric_retry_after_is_parsed_case_insensitively(self) -> None:
        self.assertEqual(parse_retry_after({"RETRY-AFTER": "30"}), 30.0)

    def test_missing_or_date_retry_after_returns_none(self) -> None:
        self.assertIsNone(parse_retry_after({}))
        self.assertIsNone(parse_retry_after({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))

    def test_negative_retry_after_is_clamped_to_zero(self) -> None:
        self.assertEqual(parse_retry_after({"Retry-After": "-5"}), 0.0)


if __name__ == "__main__":
    unittest.main()
