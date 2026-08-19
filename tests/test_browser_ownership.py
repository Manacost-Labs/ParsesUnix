"""The browser belongs to one thread, and the run loop is not it.

Sync Playwright objects belong to the greenlet that created them; touching a
page from another thread raises `greenlet.error: Cannot switch to a different
thread`. That was measured on this project, not assumed. So the production path
must make the pool unreachable from the run loop rather than merely discourage
reaching it.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper.fetchers.base import RawResponse
from web_scraper.fetchers.browser_worker import BrowserBusy, BrowserWorker
from web_scraper.fetchers.gateway import default_transport_provider
from web_scraper.fetchers.transports import PlaywrightRenderTransport
from web_scraper.run.config import RunConfig
from web_scraper.run.runner import Runner


class FakePool:
    """Records the thread that touched it, which is the whole question."""

    def __init__(self) -> None:
        self.threads: list[str] = []
        self.metrics = type("M", (), {"to_dict": lambda self: {"contexts_created": 1}})()
        self.closed = False

    def page(self, domain):
        self.threads.append(threading.current_thread().name)
        raise RuntimeError("no real browser here")

    def close(self) -> None:
        self.closed = True


class RunnerOwnershipTests(unittest.TestCase):
    def config(self, root, **kw):
        profile = root / "p.json"
        profile.write_text("{}")
        return RunConfig(profile_path=profile, state_dir=root, **kw)

    def test_the_runner_holds_a_worker_not_a_pool(self) -> None:
        # A pool reachable from the run loop is a pool that will eventually be
        # touched from the wrong thread.
        self.assertFalse(
            hasattr(Runner, "_browser_pool"),
            "the attribute that let the run loop reach the pool is gone",
        )

    def test_the_runner_does_not_expose_the_pool_at_all(self) -> None:
        source = Path(ROOT / "src/web_scraper/run/runner.py").read_text()
        self.assertNotIn(
            "browser_pool=self.", source, "the runner must not hand a pool to a transport"
        )
        self.assertIn("browser_worker=self._browser", source)

    def test_a_run_closes_the_browser_thread_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from web_scraper.profiles import parse_profile

            profile = parse_profile(
                {
                    "site": "demo.example",
                    "authorization": {"public_data_only": True},
                    "url_classes": {
                        "page": {
                            "match": "^https://demo\\.example/",
                            "expected_content_type": "html",
                            "validation": {"min_body_bytes": 10, "canary": "x"},
                            "routes": {"primary": {"type": "direct_http", "level": "L1"}},
                            "extractors": [{"kind": "heuristic"}],
                        }
                    },
                }
            )
            config = RunConfig(profile_path=root / "p.json", state_dir=root, browser_pool=False)
            runner = Runner(config, profile=profile, wall_clock=lambda: 1.0)
            runner.run()
            self.assertIsNone(runner._browser, "the worker was released")


class TransportRoutingTests(unittest.TestCase):
    def test_a_worker_is_preferred_over_a_direct_pool(self) -> None:
        pool = FakePool()
        submitted: list[str] = []

        class RecordingWorker:
            def submit(self, work, *, timeout=None):
                submitted.append(threading.current_thread().name)
                return RawResponse(
                    requested_url="u", final_url="u", status=200, headers={}, body=b"ok"
                )

        transport = PlaywrightRenderTransport(pool=pool, worker=RecordingWorker())
        transport.fetch("https://example.com/")
        self.assertEqual(len(submitted), 1, "the work went to the worker")
        self.assertEqual(pool.threads, [], "the pool was never touched directly")

    def test_the_transport_provider_passes_the_worker_through(self) -> None:
        from web_scraper.contracts import Level, Route, RouteType

        class Worker:
            def submit(self, work, *, timeout=None):  # pragma: no cover
                raise AssertionError

        worker = Worker()
        provider = default_transport_provider(browser_worker=worker)
        transport = provider(
            Route(type=RouteType.DYNAMIC, level=Level.L2), None, "https://example.com/"
        )
        self.assertIs(transport.worker, worker)

    def test_a_single_threaded_caller_may_still_use_a_pool(self) -> None:
        # A probe or a test gains nothing from an extra thread.
        transport = PlaywrightRenderTransport(pool=FakePool())
        self.assertIsNone(transport.worker)
        self.assertIsNotNone(transport.pool)


class WorkerBoundsTests(unittest.TestCase):
    """Backpressure: a fast HTTP loop must not pile up render jobs."""

    def worker(self, **kw):
        return BrowserWorker(pool_factory=FakePool, **kw)

    def test_the_queue_is_bounded(self) -> None:
        blocked = threading.Event()
        released = threading.Event()

        def slow(pool):
            blocked.set()
            released.wait(timeout=5)
            return "done"

        worker = self.worker(queue_size=1).start()
        self.addCleanup(worker.close)
        self.addCleanup(released.set)

        thread = threading.Thread(target=lambda: worker.submit(slow, timeout=5))
        thread.start()
        blocked.wait(timeout=5)

        filler = threading.Thread(target=lambda: worker.submit(slow, timeout=5))
        filler.start()

        with self.assertRaises(BrowserBusy):
            worker.submit(lambda pool: "third", timeout=0.2)

        released.set()
        thread.join(timeout=5)
        filler.join(timeout=5)

    def test_rejections_are_counted_not_swallowed(self) -> None:
        worker = self.worker(queue_size=1).start()
        self.addCleanup(worker.close)
        blocked, released = threading.Event(), threading.Event()

        def slow(pool):
            blocked.set()
            released.wait(timeout=5)
            return "done"

        threading.Thread(target=lambda: worker.submit(slow, timeout=5)).start()
        blocked.wait(timeout=5)
        threading.Thread(target=lambda: worker.submit(slow, timeout=5)).start()
        with self.assertRaises(BrowserBusy):
            worker.submit(lambda pool: "x", timeout=0.2)
        released.set()
        self.assertGreaterEqual(worker.metrics.rejected, 1)

    def test_all_the_metrics_an_operator_needs_are_reported(self) -> None:
        worker = self.worker().start()
        self.addCleanup(worker.close)
        worker.submit(lambda pool: "ok", timeout=5)
        payload = worker.metrics.to_dict()
        for key in (
            "browser_jobs_submitted",
            "browser_jobs_completed",
            "browser_jobs_failed",
            "browser_jobs_rejected",
            "browser_jobs_timed_out",
            "browser_max_queue_depth",
            "browser_avg_wait_seconds",
        ):
            self.assertIn(key, payload)

    def test_work_runs_on_the_worker_thread_not_the_caller(self) -> None:
        worker = self.worker().start()
        self.addCleanup(worker.close)
        caller = threading.current_thread().name
        where = worker.submit(lambda pool: threading.current_thread().name, timeout=5)
        self.assertNotEqual(where, caller, "the browser is not touched by the caller")

    def test_a_failing_job_does_not_kill_the_worker(self) -> None:
        worker = self.worker().start()
        self.addCleanup(worker.close)

        def boom(pool):
            raise RuntimeError("render failed")

        with self.assertRaises(RuntimeError):
            worker.submit(boom, timeout=5)
        self.assertEqual(worker.submit(lambda pool: "still alive", timeout=5), "still alive")
        self.assertGreaterEqual(worker.metrics.failed, 1)


if __name__ == "__main__":
    unittest.main()
