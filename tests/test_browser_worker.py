"""Concurrency for L2: one browser thread behind a bounded queue.

The logic is tested against a stub pool so it runs everywhere; the property the
worker exists for — that several threads can render at once without Playwright's
greenlet error — is verified against a real browser when Playwright is present.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.fetchers.browser_worker import BrowserBusy, BrowserWorker
from web_scraper.fetchers.transports import playwright_available


class StubPool:
    """Stands in for a BrowserPool; records the thread it was used from."""

    def __init__(self) -> None:
        self.closed = False
        self.threads: set[int] = set()
        self.calls = 0

    def close(self) -> None:
        self.closed = True


def touch(pool: StubPool) -> str:
    pool.threads.add(threading.get_ident())
    pool.calls += 1
    return "done"


class WorkerTests(unittest.TestCase):
    def worker(self, **kwargs: object) -> tuple[BrowserWorker, list[StubPool]]:
        pools: list[StubPool] = []

        def factory() -> StubPool:
            pool = StubPool()
            pools.append(pool)
            return pool

        return BrowserWorker(pool_factory=factory, **kwargs), pools  # type: ignore[arg-type]

    def test_work_runs_and_returns_its_result(self) -> None:
        worker, pools = self.worker()
        with worker:
            self.assertEqual(worker.submit(touch), "done")
        self.assertEqual(pools[0].calls, 1)

    def test_every_job_runs_on_the_same_single_thread(self) -> None:
        # The whole point: no browser object ever crosses a thread boundary.
        worker, pools = self.worker()
        with worker:
            threads = [threading.Thread(target=lambda: worker.submit(touch)) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(pools[0].threads), 1, "all work must run on one thread")
        self.assertNotIn(threading.get_ident(), pools[0].threads)  # and not the caller's

    def test_an_error_is_relayed_to_the_caller(self) -> None:
        worker, _ = self.worker()

        def boom(_pool: StubPool) -> None:
            raise RuntimeError("render failed")

        with worker, self.assertRaises(RuntimeError):
            worker.submit(boom)
        self.assertEqual(worker.metrics.failed, 1)

    def test_a_failed_job_does_not_stop_the_worker(self) -> None:
        worker, _ = self.worker()

        def boom(_pool: StubPool) -> None:
            raise RuntimeError("render failed")

        with worker:
            with self.assertRaises(RuntimeError):
                worker.submit(boom)
            self.assertEqual(worker.submit(touch), "done")

    def test_a_slow_job_times_out_rather_than_hanging(self) -> None:
        worker, _ = self.worker(job_timeout_seconds=0.2)

        def slow(_pool: StubPool) -> str:
            time.sleep(2.0)
            return "late"

        with worker, self.assertRaises(BrowserBusy):
            worker.submit(slow)
        self.assertEqual(worker.metrics.timed_out, 1)

    def test_closing_releases_the_pool(self) -> None:
        worker, pools = self.worker()
        with worker:
            worker.submit(touch)
        self.assertTrue(pools[0].closed)

    def test_submitting_after_close_is_refused(self) -> None:
        worker, _ = self.worker()
        worker.start()
        worker.close()
        with self.assertRaises(BrowserBusy):
            worker.submit(touch)

    def test_invalid_bounds_are_rejected(self) -> None:
        for kwargs in ({"queue_size": 0}, {"job_timeout_seconds": 0}):
            with self.assertRaises(ValueError):
                BrowserWorker(**kwargs)  # type: ignore[arg-type]

    def test_metrics_are_reported(self) -> None:
        worker, _ = self.worker()
        with worker:
            worker.submit(touch)
        payload = worker.metrics.to_dict()
        self.assertEqual(payload["browser_jobs_completed"], 1)
        self.assertIn("browser_max_queue_depth", payload)


@unittest.skipUnless(playwright_available(), "Playwright is not installed")
class LiveWorkerTests(unittest.TestCase):
    def test_several_threads_render_concurrently_without_greenlet_errors(self) -> None:
        # Doing this against a bare BrowserPool raises
        # "greenlet.error: Cannot switch to a different thread".
        rendered: list[str] = []
        errors: list[BaseException] = []

        def render(worker: BrowserWorker, domain: str) -> None:
            def work(pool: object) -> str:
                with pool.page(domain) as page:  # type: ignore[attr-defined]
                    page.set_content(f"<html><body><h1>{domain}</h1></body></html>")
                    return str(page.content())

            try:
                rendered.append(worker.submit(work))
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

        with BrowserWorker() as worker:
            threads = [
                threading.Thread(target=render, args=(worker, f"d{i}.example")) for i in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(rendered), 4)
        for index, html in enumerate(sorted(rendered)):
            self.assertIn(f"d{index}.example", html)


if __name__ == "__main__":
    unittest.main()
