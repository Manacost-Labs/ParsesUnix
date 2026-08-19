"""A bounded browser pool: one Chromium, one context per domain.

Launching a browser per URL is the most expensive mistake available at L2. A
cold launch costs roughly a second of CPU before a single byte is fetched, and a
crawl of a JavaScript-heavy site pays it on every page.

This pool keeps one browser alive and one context per domain, so pages are cheap
after the first. The bounds are the point: contexts, pages, ages and idle time
are all capped, because an unbounded pool is how a crawler runs a machine out of
memory overnight.

Domain isolation is deliberate and not negotiable: each domain gets its own
context, so cookies and storage from one site can never reach another.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Concurrent domain contexts. Each costs memory even when idle.
DEFAULT_MAX_CONTEXTS = 4

#: Pages opened through one context before it is recycled. Long-lived contexts
#: accumulate renderer state; recycling bounds that without paying a full launch.
DEFAULT_MAX_PAGES_PER_CONTEXT = 50

#: A context older than this is recycled even if it looks healthy.
DEFAULT_CONTEXT_TTL_SECONDS = 900.0


class BrowserUnavailable(RuntimeError):
    """Playwright or a browser binary is not available in this environment."""


@dataclass
class BrowserPoolMetrics:
    """What the pool did, for the run report."""

    contexts_created: int = 0
    contexts_reused: int = 0
    contexts_recycled: int = 0
    contexts_evicted: int = 0
    pages_opened: int = 0
    navigation_timeouts: int = 0
    crashes: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "browser_contexts_created": self.contexts_created,
            "browser_contexts_reused": self.contexts_reused,
            "browser_contexts_recycled": self.contexts_recycled,
            "browser_contexts_evicted": self.contexts_evicted,
            "browser_pages_opened": self.pages_opened,
            "browser_navigation_timeouts": self.navigation_timeouts,
            "browser_crashes": self.crashes,
        }


@dataclass
class _DomainContext:
    domain: str
    context: Any
    created_at: float
    pages_opened: int = 0
    last_used: float = field(default=0.0)

    def is_exhausted(self, *, now: float, max_pages: int, ttl: float) -> bool:
        return self.pages_opened >= max_pages or (now - self.created_at) >= ttl


class BrowserPool:
    """Shared Chromium with per-domain contexts, all of it bounded.

    **Single-threaded.** The lock here guards this object's bookkeeping, not the
    browser: sync Playwright objects belong to the greenlet that created them,
    and touching a page from another thread raises
    ``greenlet.error: Cannot switch to a different thread``. Measured, not
    assumed — an earlier version of this docstring claimed thread-safety and
    three worker threads sharing one pool failed exactly that way.

    For concurrency use :class:`~web_scraper.fetchers.browser_worker.BrowserWorker`,
    which owns a pool on a dedicated thread and accepts work through a bounded
    queue.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        max_contexts: int = DEFAULT_MAX_CONTEXTS,
        max_pages_per_context: int = DEFAULT_MAX_PAGES_PER_CONTEXT,
        context_ttl_seconds: float = DEFAULT_CONTEXT_TTL_SECONDS,
        extra_http_headers: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_contexts < 1 or max_pages_per_context < 1 or context_ttl_seconds <= 0:
            raise ValueError("pool bounds must be positive")
        self.headless = headless
        self.max_contexts = max_contexts
        self.max_pages_per_context = max_pages_per_context
        self.context_ttl_seconds = context_ttl_seconds
        self.extra_http_headers = dict(extra_http_headers or {})
        self.metrics = BrowserPoolMetrics()
        self._clock = clock
        self._lock = threading.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._contexts: dict[str, _DomainContext] = {}

    # -- lifecycle ---------------------------------------------------------

    def _ensure_browser(self) -> Any:
        """Start Playwright and the browser once, on first use."""

        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise BrowserUnavailable(
                "the browser pool requires Playwright: "
                "pip install -e '.[browser]' && playwright install chromium"
            ) from exc
        self._playwright = sync_playwright().start()  # pragma: no cover - live browser
        self._browser = self._playwright.chromium.launch(  # pragma: no cover - live browser
            headless=self.headless
        )
        return self._browser

    def _new_context(self, domain: str) -> _DomainContext:  # pragma: no cover - live browser
        browser = self._ensure_browser()
        context = browser.new_context(extra_http_headers=self.extra_http_headers or None)
        self.metrics.contexts_created += 1
        now = self._clock()
        return _DomainContext(domain=domain, context=context, created_at=now, last_used=now)

    def _close_context(self, entry: _DomainContext) -> None:
        try:
            entry.context.close()
        except Exception:
            logger.debug("failed to close context for %s", entry.domain, exc_info=True)

    def _evict_one_locked(self) -> None:
        """Drop the least recently used context to stay within the bound."""

        if not self._contexts:
            return
        victim_domain = min(self._contexts, key=lambda name: self._contexts[name].last_used)
        self._close_context(self._contexts.pop(victim_domain))
        self.metrics.contexts_evicted += 1

    def _acquire_context(self, domain: str) -> _DomainContext:
        with self._lock:
            now = self._clock()
            entry = self._contexts.get(domain)
            if entry is not None and entry.is_exhausted(
                now=now,
                max_pages=self.max_pages_per_context,
                ttl=self.context_ttl_seconds,
            ):
                self._close_context(self._contexts.pop(domain))
                self.metrics.contexts_recycled += 1
                entry = None

            if entry is None:
                while len(self._contexts) >= self.max_contexts:
                    self._evict_one_locked()
                entry = self._new_context(domain)
                self._contexts[domain] = entry
            else:
                self.metrics.contexts_reused += 1

            entry.last_used = now
            entry.pages_opened += 1
            self.metrics.pages_opened += 1
            return entry

    @contextmanager
    def page(self, domain: str) -> Iterator[Any]:  # pragma: no cover - live browser
        """A page in this domain's context, always closed afterwards.

        A crash takes the context down with it rather than leaving a poisoned one
        behind: the next call gets a fresh one.
        """

        entry = self._acquire_context(domain)
        page = None
        try:
            page = entry.context.new_page()
            yield page
        except Exception:
            self.metrics.crashes += 1
            with self._lock:
                current = self._contexts.get(domain)
                if current is entry:
                    self._close_context(self._contexts.pop(domain))
            raise
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    logger.debug("failed to close page for %s", domain, exc_info=True)

    def close(self) -> None:
        """Deterministic shutdown: contexts, then browser, then Playwright."""

        with self._lock:
            for entry in list(self._contexts.values()):
                self._close_context(entry)
            self._contexts.clear()
            for obj in (self._browser, self._playwright):
                closer = (
                    (getattr(obj, "close", None) or getattr(obj, "stop", None)) if obj else None
                )
                if closer is not None:
                    try:
                        closer()
                    except Exception:
                        logger.debug("failed to shut down %r", obj, exc_info=True)
            self._browser = None
            self._playwright = None

    def __enter__(self) -> BrowserPool:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- introspection -----------------------------------------------------

    @property
    def active_contexts(self) -> int:
        with self._lock:
            return len(self._contexts)

    def domains(self) -> list[str]:
        with self._lock:
            return sorted(self._contexts)
