"""All Playwright work on one thread, reached through a bounded queue.

Sync Playwright objects are bound to the greenlet that created them. A lock
around the pool's bookkeeping does not change that: a second thread touching a
page raises ``greenlet.error: Cannot switch to a different thread``. Measured,
not assumed — three worker threads sharing one pool failed exactly that way.

So concurrency is expressed the only way that is actually safe here:

    HTTP workers  ->  bounded queue  ->  one browser thread  ->  BrowserPool

The queue bound is the backpressure. When L2 is saturated, submitting blocks
instead of piling up pending pages, which is what keeps a crawl of a JavaScript
site from opening thousands of tabs.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from web_scraper.fetchers.browser_pool import BrowserPool

T = TypeVar("T")

#: Jobs allowed to wait. Small on purpose: a deep queue only hides the fact that
#: L2 cannot keep up, and every waiting job is a worker doing nothing.
DEFAULT_QUEUE_SIZE = 8

#: How long a caller waits for the browser thread before giving up.
DEFAULT_JOB_TIMEOUT_SECONDS = 120.0


class BrowserBusy(RuntimeError):
    """The browser thread did not take the job in time."""


@dataclass
class BrowserWorkerMetrics:
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    rejected: int = 0  # queue full
    timed_out: int = 0
    max_queue_depth: int = 0
    total_wait_seconds: float = 0.0

    def to_dict(self) -> dict[str, float | int]:
        average_wait = self.total_wait_seconds / self.completed if self.completed else 0.0
        return {
            "browser_jobs_submitted": self.submitted,
            "browser_jobs_completed": self.completed,
            "browser_jobs_failed": self.failed,
            "browser_jobs_rejected": self.rejected,
            "browser_jobs_timed_out": self.timed_out,
            "browser_max_queue_depth": self.max_queue_depth,
            "browser_avg_wait_seconds": round(average_wait, 4),
        }


@dataclass
class _Job:
    work: Callable[[BrowserPool], Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class BrowserWorker:
    """Serializes every Playwright call onto one thread.

    The pool is created *inside* that thread, so no browser object ever crosses
    a thread boundary.
    """

    def __init__(
        self,
        *,
        pool_factory: Callable[[], BrowserPool] | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    ) -> None:
        if queue_size < 1 or job_timeout_seconds <= 0:
            raise ValueError("queue_size must be >= 1 and job_timeout_seconds positive")
        self._pool_factory = pool_factory or BrowserPool
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=queue_size)
        self._job_timeout = job_timeout_seconds
        self.metrics = BrowserWorkerMetrics()
        self._pool: BrowserPool | None = None
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, name="browser-worker", daemon=True)
        self._closed = False

    # -- worker thread -----------------------------------------------------

    def _run(self) -> None:
        self._pool = self._pool_factory()
        self._started.set()
        try:
            while True:
                job = self._queue.get()
                if job is None:  # shutdown sentinel
                    self._queue.task_done()
                    return
                try:
                    job.result = job.work(self._pool)
                except BaseException as exc:  # noqa: BLE001 - relayed to the caller
                    job.error = exc
                finally:
                    job.done.set()
                    self._queue.task_done()
        finally:
            if self._pool is not None:
                self._pool.close()
                self._pool = None

    def start(self) -> BrowserWorker:
        if not self._thread.is_alive() and not self._closed:
            self._thread.start()
            self._started.wait(timeout=30)
        return self

    # -- callers -----------------------------------------------------------

    def submit(self, work: Callable[[BrowserPool], T], *, timeout: float | None = None) -> T:
        """Run ``work`` on the browser thread and return its result.

        Blocks while the queue is full: that is the backpressure, and it is
        preferable to accepting work L2 cannot do.
        """

        if self._closed:
            raise BrowserBusy("browser worker is closed")
        self.start()
        job = _Job(work=work)
        self.metrics.submitted += 1
        try:
            self._queue.put(job, timeout=timeout or self._job_timeout)
        except queue.Full as exc:
            self.metrics.rejected += 1
            raise BrowserBusy("browser queue is full") from exc

        depth = self._queue.qsize()
        self.metrics.max_queue_depth = max(self.metrics.max_queue_depth, depth)

        import time as _time

        waited_from = _time.monotonic()
        if not job.done.wait(timeout=timeout or self._job_timeout):
            self.metrics.timed_out += 1
            raise BrowserBusy("browser job timed out")
        self.metrics.total_wait_seconds += _time.monotonic() - waited_from

        if job.error is not None:
            self.metrics.failed += 1
            raise job.error
        self.metrics.completed += 1
        return job.result  # type: ignore[no-any-return]

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Stop the thread and release the browser, deterministically."""

        if self._closed:
            return
        self._closed = True
        if self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=30)

    @property
    def pool_metrics(self) -> dict[str, int]:
        pool = self._pool
        return pool.metrics.to_dict() if pool is not None else {}

    def __enter__(self) -> BrowserWorker:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()
