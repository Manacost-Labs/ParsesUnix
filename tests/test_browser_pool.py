"""Bounds, reuse and isolation of the browser pool.

The pool's logic is tested against a stub browser so it runs everywhere; the
live behaviour it exists for — that a second page on a domain does not pay a
browser launch — is measured separately when Playwright is present.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.fetchers.browser_pool import BrowserPool, _DomainContext
from web_scraper.fetchers.transports import playwright_available


class StubContext:
    def __init__(self, domain: str) -> None:
        self.domain = domain
        self.closed = False

    def close(self) -> None:
        self.closed = True


class StubPool(BrowserPool):
    """A pool whose contexts are objects, not browsers."""

    def __init__(self, **kwargs: object) -> None:
        self.time = [0.0]
        kwargs.setdefault("clock", lambda: self.time[0])
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.created: list[StubContext] = []

    def _new_context(self, domain: str) -> _DomainContext:
        stub = StubContext(domain)
        self.created.append(stub)
        self.metrics.contexts_created += 1
        return _DomainContext(
            domain=domain, context=stub, created_at=self.time[0], last_used=self.time[0]
        )


class ReuseTests(unittest.TestCase):
    def test_a_second_page_on_the_same_domain_reuses_the_context(self) -> None:
        pool = StubPool()
        first = pool._acquire_context("a.example")
        second = pool._acquire_context("a.example")
        self.assertIs(first.context, second.context)
        self.assertEqual(pool.metrics.contexts_created, 1)
        self.assertEqual(pool.metrics.contexts_reused, 1)

    def test_each_domain_gets_its_own_context(self) -> None:
        # Isolation is not an optimisation detail: cookies must not cross sites.
        pool = StubPool()
        a = pool._acquire_context("a.example")
        b = pool._acquire_context("b.example")
        self.assertIsNot(a.context, b.context)
        self.assertEqual(pool.domains(), ["a.example", "b.example"])


class BoundTests(unittest.TestCase):
    def test_contexts_are_capped_and_the_coldest_is_evicted(self) -> None:
        pool = StubPool(max_contexts=2)
        pool._acquire_context("a.example")
        pool.time[0] = 1.0
        pool._acquire_context("b.example")
        pool.time[0] = 2.0
        pool._acquire_context("a.example")  # a is now the most recently used
        pool.time[0] = 3.0
        pool._acquire_context("c.example")  # must evict b, the coldest

        self.assertEqual(pool.active_contexts, 2)
        self.assertEqual(pool.domains(), ["a.example", "c.example"])
        self.assertEqual(pool.metrics.contexts_evicted, 1)

    def test_a_context_is_recycled_after_enough_pages(self) -> None:
        pool = StubPool(max_pages_per_context=3)
        contexts = {id(pool._acquire_context("a.example").context) for _ in range(3)}
        self.assertEqual(len(contexts), 1)  # first three share one context
        fourth = pool._acquire_context("a.example")
        self.assertNotIn(id(fourth.context), contexts)
        self.assertEqual(pool.metrics.contexts_recycled, 1)
        self.assertTrue(pool.created[0].closed, "the recycled context must be closed")

    def test_a_context_is_recycled_once_it_is_old(self) -> None:
        pool = StubPool(context_ttl_seconds=100.0)
        first = pool._acquire_context("a.example")
        pool.time[0] = 101.0
        second = pool._acquire_context("a.example")
        self.assertIsNot(first.context, second.context)
        self.assertEqual(pool.metrics.contexts_recycled, 1)

    def test_invalid_bounds_are_rejected(self) -> None:
        for kwargs in (
            {"max_contexts": 0},
            {"max_pages_per_context": 0},
            {"context_ttl_seconds": 0},
        ):
            with self.assertRaises(ValueError):
                BrowserPool(**kwargs)  # type: ignore[arg-type]


class ShutdownTests(unittest.TestCase):
    def test_close_releases_every_context(self) -> None:
        pool = StubPool()
        pool._acquire_context("a.example")
        pool._acquire_context("b.example")
        pool.close()
        self.assertEqual(pool.active_contexts, 0)
        self.assertTrue(all(context.closed for context in pool.created))

    def test_the_pool_is_a_context_manager(self) -> None:
        with StubPool() as pool:
            pool._acquire_context("a.example")
        self.assertEqual(pool.active_contexts, 0)


@unittest.skipUnless(playwright_available(), "Playwright is not installed")
class LivePoolTests(unittest.TestCase):
    """The behaviour the pool exists for, against a real browser."""

    def test_pages_share_one_browser_and_the_second_is_much_cheaper(self) -> None:
        import time

        with BrowserPool(max_contexts=2) as pool:
            started = time.monotonic()
            with pool.page("example.com") as page:
                page.set_content("<html><body><h1>one</h1></body></html>")
                self.assertIn("one", page.content())
            first = time.monotonic() - started

            started = time.monotonic()
            with pool.page("example.com") as page:
                page.set_content("<html><body><h1>two</h1></body></html>")
                self.assertIn("two", page.content())
            second = time.monotonic() - started

            self.assertEqual(pool.metrics.contexts_created, 1)
            self.assertEqual(pool.metrics.contexts_reused, 1)
            self.assertEqual(pool.metrics.pages_opened, 2)
            # The launch is paid once. Asserting only the direction, not a ratio:
            # absolute timings are not stable enough to gate CI on.
            self.assertLess(second, first)

    def test_domains_do_not_share_a_context(self) -> None:
        with BrowserPool(max_contexts=2) as pool:
            with pool.page("a.example") as page:
                page.set_content("<html><body>a</body></html>")
            with pool.page("b.example") as page:
                page.set_content("<html><body>b</body></html>")
            self.assertEqual(pool.metrics.contexts_created, 2)
            self.assertEqual(sorted(pool.domains()), ["a.example", "b.example"])


if __name__ == "__main__":
    unittest.main()
