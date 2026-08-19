"""The unattended run loop.

Ties the pieces together with the invariants the whole project exists for:

* every claimed URL ends with a durable status — nothing is silently skipped;
* interruption is safe — a re-run resumes from the queue and creates no dupes;
* freshness avoids re-downloading unchanged pages (conditional request + hash);
* the clean dataset is only ever replaced by an atomic, validated promote;
* no paid request is ever made (this is the free L0-L2 core).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any

from web_scraper.budget import BudgetLedger
from web_scraper.contracts import Result, Verdict
from web_scraper.discovery import DiscoveryCollector, observed_from_mapping, summarise
from web_scraper.extract import extract_fields, run_quorum
from web_scraper.fetchers import CircuitBreaker, FetchGateway, RawResponse
from web_scraper.fetchers.browser_pool import BrowserPool
from web_scraper.fetchers.browser_worker import BrowserWorker
from web_scraper.fetchers.gateway import GatewayOutcome, default_transport_provider
from web_scraper.fingerprints import FingerprintStore
from web_scraper.finops.canary import CanaryStatus, PaidCanary
from web_scraper.finops.free_canary import (
    CanaryUrl,
    FreeCanary,
    FreeCanaryOutcome,
    FreeCanaryStatus,
)
from web_scraper.freshness import FreshnessStore
from web_scraper.observability import Alerter, AlertEvent, LoggingAlerter, RunMetrics
from web_scraper.observability.accounting import build_accounting
from web_scraper.observability.metrics import build_report
from web_scraper.profiles import load_profile
from web_scraper.profiles.model import SiteProfile, UrlClass
from web_scraper.publish import (
    DatasetStore,
    build_availability,
    summarize_availability,
)
from web_scraper.publish.availability import summarize_by_url_class
from web_scraper.publish.drift import DriftReport, SchemaSnapshot, check_drift
from web_scraper.queue import QueueStore, normalize_url
from web_scraper.queue.store import QueuedUrl
from web_scraper.routing import RouteStatsStore
from web_scraper.routing.router import AdaptiveRouter
from web_scraper.run.config import RunConfig
from web_scraper.run.paid_ledger import PaidAttemptLedger, PaidAttemptState
from web_scraper.run.phases import Phase, PhaseController, PhaseStore, admits
from web_scraper.storage.snapshots import SnapshotStore

logger = logging.getLogger(__name__)

#: Below this many pending URLs a free canary is not worth running: the sample
#: would be most of the queue, so it would fetch the run twice rather than
#: preview it. See ``_run_free_canary``.
MIN_QUEUE_FOR_CANARY = 50

# Verdict -> retry backoff (seconds) for transient failures.
_RETRY_BACKOFF = {Verdict.RATE_LIMITED: 1800.0, Verdict.ORIGIN_DOWN: 7200.0}


@dataclass
class RunResult:
    report: dict[str, Any]
    promote: dict[str, Any] | None
    processed: int


class Runner:
    def __init__(
        self,
        config: RunConfig,
        *,
        profile: SiteProfile | None = None,
        gateway: FetchGateway | None = None,
        alerter: Alerter | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.profile = profile or load_profile(config.profile_path)
        self.queue = QueueStore(config.queue_path, now=wall_clock)
        self.dataset = DatasetStore(config.dataset_path, now=wall_clock)
        self.freshness = FreshnessStore(config.freshness_path, now=wall_clock)
        self.snapshots = SnapshotStore(config.snapshot_dir, now=wall_clock)
        self.route_stats = RouteStatsStore(config.route_stats_path, now=wall_clock)
        self.fingerprints = FingerprintStore(config.fingerprints_path, now=wall_clock)
        # Only built when a limit is configured: no limit means no paid work.
        self.budget = (
            BudgetLedger(
                config.budget_path,
                daily_credit_limit=config.daily_credit_limit,
                now=wall_clock,
            )
            if config.daily_credit_limit is not None
            else None
        )
        self.alerter = alerter or LoggingAlerter()
        self.metrics = RunMetrics()
        self._clock = clock
        self._wall_clock = wall_clock
        # One browser for the whole run, owned by a dedicated thread and shut
        # down in run(). The runner must NOT hold the pool itself: sync
        # Playwright objects belong to the greenlet that created them, and a
        # pool reachable from the run loop is a pool that will eventually be
        # touched from the wrong thread. An injected gateway brings its own
        # transports, so the runner builds nothing for it.
        self._browser: BrowserWorker | None = None
        if gateway is None and config.browser_pool:
            contexts = config.max_browser_contexts
            self._browser = BrowserWorker(
                pool_factory=lambda: BrowserPool(max_contexts=contexts),
                # The queue is bounded so a fast HTTP loop cannot pile up
                # unbounded render jobs behind one browser.
                queue_size=max(config.batch_size * 2, 4),
            ).start()
        self._gateway = gateway or FetchGateway(
            self.profile,
            transport_provider=default_transport_provider(
                browser_worker=self._browser,
                network_observer=self._observe_network,
            ),
            snapshots=self.snapshots,
            breaker=CircuitBreaker(),
            route_stats=self.route_stats,
            fingerprints=self.fingerprints,
            router=AdaptiveRouter(self.route_stats) if config.adaptive_routing else None,
        )
        self._results: list[Result] = []
        self._escalator: Any = None
        self._paid_router: Any = None

        # The paid gateway is a SEPARATE instance. Phase A must be unable to
        # spend by construction, not by a flag someone can flip: the free
        # gateway has no escalator attached, so there is no code path from it
        # to a provider at all.
        self._paid_gateway = self._build_paid_gateway() if gateway is None else None
        self._paid_ledger = PaidAttemptLedger(
            config.state_dir / "paid_attempts.sqlite3", now=wall_clock
        )
        self._phases = PhaseController(
            run_id=config.effective_run_id,
            store=PhaseStore(config.state_dir / "phases.sqlite3", now=wall_clock),
            allowed=self._allowed_phases(),
        )
        self._canary_reports: dict[str, Any] = {}
        # Discovery rides along with browser renders. It accumulates across the
        # whole run, which is the point: one page proves nothing, and the
        # threshold for VALIDATED is only reachable once several pages of the
        # same class have been rendered.
        self._discovery = (
            DiscoveryCollector(
                wanted_fields=self._critical_fields(),
                allow_private=config.allow_private,
            )
            if config.discover_api
            else None
        )
        #: Set by a signal handler or by request_stop(). Read between URLs, so a
        #: shutdown never interrupts a paid call it cannot account for.
        self._stopping = False

    def _critical_fields(self) -> tuple[str, ...]:
        """What discovery should look for: the fields the profile actually needs.

        Searching for the profile's own critical fields is what makes a draft
        checkable — the extractor paths come from where those fields were seen,
        not from a template.
        """

        names: set[str] = set()
        for cls in self.profile.url_classes.values():
            names |= set(cls.quorum_fields or ())
            names |= set(cls.required_fields or ())
        return tuple(sorted(names))

    def _observe_network(self, payload: dict[str, Any]) -> None:
        """Called from the browser thread for every response a render received.

        Guarded: discovery is a passenger on the render, and a passenger must
        never be able to crash the vehicle.
        """

        if self._discovery is None:
            return
        try:
            self._discovery.observe(observed_from_mapping(payload))
        except Exception:
            logger.debug("discovery failed to record an observation", exc_info=True)

    def _allowed_phases(self) -> tuple[Phase, ...]:
        """A run with no funded paid layer never enters a paid phase."""

        if self._paid_gateway is None:
            return (Phase.FREE, Phase.FREE_RETRY)
        return (Phase.FREE, Phase.FREE_RETRY, Phase.CHEAP_PAID, Phase.EXPENSIVE_PAID)

    def _build_paid_gateway(self) -> FetchGateway | None:
        """A second gateway that CAN spend, used only in the paid phases."""

        if self.budget is None:
            return None
        from web_scraper.providers.breaker import BreakerStore, ProviderBreakers
        from web_scraper.providers.multi_escalation import MultiProviderEscalator
        from web_scraper.providers.multi_router import MultiProviderRouter
        from web_scraper.providers.stats import ProviderStatsStore
        from web_scraper.run.estimate_cli import configured_providers

        providers = configured_providers()
        if not providers:
            return None

        router = MultiProviderRouter(
            providers=providers,
            stats=ProviderStatsStore(self.config.state_dir / "provider_stats.sqlite3"),
            breakers=ProviderBreakers(
                store=BreakerStore(self.config.state_dir / "provider_breakers.sqlite3")
            ),
        )
        self._paid_router = router
        escalator = MultiProviderEscalator(router, budget=self.budget)
        self._escalator = escalator
        return FetchGateway(
            self.profile,
            transport_provider=default_transport_provider(
                browser_worker=self._browser, network_observer=self._observe_network
            ),
            snapshots=self.snapshots,
            breaker=CircuitBreaker(),
            route_stats=self.route_stats,
            fingerprints=self.fingerprints,
            router=AdaptiveRouter(self.route_stats) if self.config.adaptive_routing else None,
            paid_escalator=escalator,
        )

    # -- public ------------------------------------------------------------

    def run(self) -> RunResult:
        with self._shutdown_handlers():
            return self._run()

    def _run(self) -> RunResult:
        # Track whether each seeded URL actually reached the queue: a URL lost
        # between the caller and the ledger would otherwise be invisible.
        seeded: dict[str, bool] = {}
        for url in self.config.seed_urls:
            url_class = self.profile.class_for_url(url)
            self.queue.add(url, url_class=url_class.name if url_class else None)
            seeded[url] = self.queue.get(url) is not None

        # Money first: a reservation left open by a crashed process must be
        # resolved before this run is allowed to commit any more spend.
        self._recover_budget()

        # Resume: any IN_PROGRESS row is from a crashed run.
        self.queue.reset_stale_in_progress()
        self.dataset.reset_staging()

        # Freshness re-crawl: re-open DONE urls whose interval has elapsed (or
        # everything under a full review), so unchanged pages get a cheap 304.
        due = [
            url
            for url in self.queue.done_urls()
            if self.freshness.is_due(url, full_review=self.config.full_review)
        ]
        if due:
            self.queue.reactivate(due)

        if self.config.sweep:
            self._run_sweep()

        start = self._clock()
        processed = 0

        # A few free fetches that can stop a very large run. If the site no
        # longer matches the profile, every remaining URL hits the same wall and
        # the paid phases would pay to discover it one page at a time.
        canary = self._run_free_canary()
        if canary is not None and canary.status is FreeCanaryStatus.BLOCK_RUN:
            return self._abort_run(canary, seeded, processed)

        for phase in self._phases.remaining():
            if self._out_of_time(start):
                break
            self._phases.enter(phase)

            if phase.is_paid and not self._paid_phases_permitted():
                continue

            processed += self._run_phase(phase, start=start)
            self._phases.complete(phase, counts={"processed": processed})

        # The browser is released before reporting: a run must not leave a
        # Chromium behind because report building raised.
        self._close_browser_pool()

        promote = self._promote()
        accounting = build_accounting(self.queue.counts_by_status(), seeded_urls=seeded)
        if not accounting.is_complete:
            self.alerter.send(
                AlertEvent(
                    kind="unaccounted_urls",
                    message="run finished with URLs that are not accounted for",
                    context=accounting.to_dict(),
                )
            )
        self.metrics.route_stats = [stats.to_dict() for stats in self.route_stats.all_stats()]
        self.metrics.availability = self._availability_slo()
        report = build_report(
            self._results,
            metrics=self.metrics,
            quarantined_urls=[q["url"] for q in self.queue.quarantined()],
            dead_zone_urls=[d["url"] for d in self.queue.dead_zones()],
            promote=promote,
            accounting=accounting,
        )
        payload = report.to_dict()
        payload["canaries"] = self._canary_reports
        payload["discovery"] = self._discovery_report()
        payload["phases"] = self._phases.to_dict()
        payload["paid_attempts"] = self._paid_ledger.summary()
        stranded = self._paid_ledger.stranded()
        if stranded:
            self.alerter.send(
                AlertEvent(
                    kind="stranded_paid_attempt",
                    message=(
                        f"{len(stranded)} URL(s) have a paid attempt that never completed; "
                        "they may have been billed and are held out of the paid layer"
                    ),
                    context={"urls": [r.url for r in stranded][:20]},
                )
            )
        self._final_alerts(payload, promote)
        return RunResult(report=payload, promote=promote, processed=processed)

    def _abort_run(
        self, canary: FreeCanaryOutcome, seeded: dict[str, bool], processed: int
    ) -> RunResult:
        """Stop before the crawl, keeping every URL and publishing nothing.

        A blocked canary must not look like a completed run with poor coverage:
        nothing was attempted, so nothing is promoted and the consumer stays on
        the previous dataset.
        """

        self._close_browser_pool()
        accounting = build_accounting(self.queue.counts_by_status(), seeded_urls=seeded)
        self.metrics.availability = self._availability_slo()
        report = build_report(
            self._results,
            metrics=self.metrics,
            quarantined_urls=[q["url"] for q in self.queue.quarantined()],
            dead_zone_urls=[d["url"] for d in self.queue.dead_zones()],
            promote={"ok": False, "reason": "free canary blocked the run", "staged": 0},
            accounting=accounting,
        )
        payload = report.to_dict()
        payload["canaries"] = self._canary_reports
        payload["aborted"] = True
        return RunResult(report=payload, promote=None, processed=processed)

    # -- phases ------------------------------------------------------------

    def _out_of_time(self, start: float) -> bool:
        """Stop claiming new work: the window closed, or someone asked us to.

        Both mean the same thing to the loop — finish the URL in hand, claim no
        more, and let the run report what it carried. A shutdown that killed the
        current URL mid-flight would leave it IN_PROGRESS and, if it was a paid
        attempt, leave the spend unresolved.
        """

        if self._stopping:
            return True
        return (
            self.config.deadline_seconds is not None
            and (self._clock() - start) >= self.config.deadline_seconds
        )

    def request_stop(self, reason: str = "shutdown requested") -> None:
        """Ask the run to wind down after the URL currently in flight."""

        if not self._stopping:
            logger.warning("%s; finishing the current URL and stopping", reason)
        self._stopping = True

    @contextmanager
    def _shutdown_handlers(self) -> Iterator[None]:
        """Turn SIGTERM/SIGINT into a request to wind down, not a kill.

        Only installed when running on the main thread: signal handlers are
        process-global, and a library used inside someone else's service must
        not silently take over its signal handling.
        """

        installed: list[tuple[signal.Signals, Any]] = []
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                try:
                    previous = signal.signal(signum, lambda s, _f: self.request_stop(f"signal {s}"))
                except (ValueError, OSError):  # pragma: no cover - platform dependent
                    continue
                installed.append((signum, previous))
        try:
            yield
        finally:
            for signum, previous in installed:
                with suppress(ValueError, OSError):
                    signal.signal(signum, previous)

    def _run_phase(self, phase: Phase, *, start: float) -> int:
        """Drain one phase. Paid phases use a different gateway entirely."""

        gateway = self._paid_gateway if phase.is_paid else self._gateway
        processed = 0
        while True:
            if self._out_of_time(start):
                break
            batch = self.queue.claim_batch(self.config.batch_size)
            if not batch:
                break
            for queued in batch:
                if not self._admits(phase, queued):
                    # Not this phase's work. Return it to the queue so a later
                    # phase — or the next run — still sees it. Dropping it here
                    # is how a URL disappears.
                    self.queue.defer(queued.url)
                    continue
                self._process_guarded(queued, gateway=gateway, phase=phase)
                processed += 1
        return processed

    def _admits(self, phase: Phase, queued: QueuedUrl) -> bool:
        """Does this phase take this URL, given how it last ended?"""

        if phase is Phase.FREE:
            return True
        last = queued.verdict
        if not last:
            # No prior verdict means phase A never ran it. Only phase A takes
            # untried URLs; a later phase picking one up would skip the free
            # attempt entirely and could send it straight to a provider.
            return False
        try:
            verdict = Verdict(last)
        except ValueError:
            return False
        if not admits(phase, verdict):
            return False
        # Already attempted, or attempted and never resolved. Paying again is
        # the one error the budget system exists to prevent.
        return not (phase.is_paid and not self._paid_ledger.may_attempt(queued.url))

    def _paid_phases_permitted(self) -> bool:
        """Budget, providers and the paid canary must all say yes."""

        if self._paid_gateway is None or self.budget is None:
            return False
        if not self.budget.state().allows_paid_work:
            self.alerter.send(
                AlertEvent(
                    kind="paid_phase_skipped",
                    message=f"budget state is {self.budget.state().value}; paid phases skipped",
                    context={"state": self.budget.state().value},
                )
            )
            return False
        return self._run_paid_canary()

    def _record_paid_outcome(self, url: str, outcome: GatewayOutcome) -> None:
        """Write down what the paid attempt did, so a restart cannot repeat it."""

        paid = outcome.paid
        if paid is None:
            # The gateway never reached the paid step for this URL, so nothing
            # was risked and it stays eligible.
            self._paid_ledger.finish(
                url, state=PaidAttemptState.REFUSED, reason="free routes resolved it"
            )
            return
        state = (
            PaidAttemptState.REFUSED
            if not paid.attempted
            else PaidAttemptState.UNKNOWN
            if not paid.cost.is_known
            else PaidAttemptState.SETTLED
        )
        self._paid_ledger.finish(
            url,
            state=state,
            cost=paid.cost,
            verdict=paid.triage.verdict.value if paid.triage else None,
            provider_hint=getattr(paid, "provider", None),
            reason=paid.reason,
        )

    # -- canaries ----------------------------------------------------------

    def _run_free_canary(self) -> FreeCanaryOutcome | None:
        """Stratified free fetches, run before anything else."""

        if not self.config.free_canary:
            return None
        candidates = self._canary_candidates()
        # A canary is a SAMPLE. On a queue small enough that the sample would
        # cover most of it, it samples nothing — it just fetches the run twice,
        # doubling the work and the route statistics. Below the threshold the
        # run itself is the check.
        if len(candidates) < MIN_QUEUE_FOR_CANARY:
            return None
        try:
            outcome = FreeCanary().run(candidates, fetch=self._gateway.fetch_url)
        except Exception:
            logger.exception("free canary failed to run; continuing without its verdict")
            return None
        self._canary_reports["free"] = outcome.to_dict()
        if outcome.status is not FreeCanaryStatus.PASS:
            self.alerter.send(
                AlertEvent(
                    kind="free_canary",
                    message=f"free canary: {outcome.status.value}",
                    context={"explanation": outcome.explain()},
                )
            )
        return outcome

    def _canary_candidates(self) -> list[CanaryUrl]:
        """A handful of URLs per stratum, drawn from what the queue holds."""

        rows = [r for r in self.queue.all_rows() if r.status.value in ("PENDING", "RETRY")]
        out: list[CanaryUrl] = []
        for row in rows:
            url_class = row.url_class or "unknown"
            stratum = url_class
            if row.verdict in {Verdict.CSR_REQUIRED.value, Verdict.BLOCKED.value}:
                stratum = "unstable"
            out.append(CanaryUrl(url=row.url, stratum=stratum, url_class=url_class))
        return out

    def _run_paid_canary(self) -> bool:
        """Spend a few credits to decide whether to spend many."""

        if not self.config.paid_canary or self._escalator is None:
            return True
        candidates = [
            (row.url, _domain(row.url), row.url_class or "unknown", Verdict(row.verdict))
            for row in self.queue.all_rows()
            if row.verdict in {Verdict.BLOCKED.value, Verdict.SOFT_BLOCK.value}
            and self._paid_ledger.may_attempt(row.url)
        ]
        if not candidates:
            return True
        outcome = PaidCanary().run(candidates, attempt=self._paid_attempt)
        self._canary_reports["paid"] = outcome.to_dict()
        if outcome.status is CanaryStatus.BLOCK_PAID_PHASE:
            self.alerter.send(
                AlertEvent(
                    kind="paid_canary_block",
                    message="paid canary blocked the paid phases; free results are kept",
                    context={"explanation": outcome.explain()},
                )
            )
            return False
        return True

    def _paid_attempt(self, url: str, *, verdict: Verdict, domain: str, url_class: str) -> Any:
        """One paid attempt, recorded per URL before any money moves."""

        assert self._escalator is not None
        self._paid_ledger.start(url, provider="?", strategy_id="?")
        outcome = self._escalator.attempt(url, verdict=verdict, domain=domain, url_class=url_class)
        state = (
            PaidAttemptState.REFUSED
            if not outcome.attempted
            else PaidAttemptState.UNKNOWN
            if outcome.unknown_spend
            else PaidAttemptState.SETTLED
        )
        self._paid_ledger.finish(
            url,
            state=state,
            cost=outcome.cost,
            verdict=outcome.triage.verdict.value if outcome.triage else None,
            reason=outcome.reason,
        )
        return outcome

    def _discovery_report(self) -> dict[str, Any]:
        """What the renders taught us, and what it would have saved.

        The saving is stated only where it is countable: a validated endpoint
        replaces the renders that were needed to find it, and that count is
        known. No estimate is made for future runs, because nobody has run them.
        """

        if self._discovery is None:
            return {}
        candidates = self._discovery.candidates()
        report = summarise(candidates)
        rendered = self.metrics.by_level.get("L2", 0)
        report["browser_renders_this_run"] = rendered
        report["api_routes_discovered"] = len(candidates)
        report["api_routes_validated"] = report["validated"]
        if report["validated"] and rendered:
            report["browser_renders_replaceable"] = rendered
            report["note"] = (
                f"{report['validated']} validated endpoint(s) cover the {rendered} render(s) "
                "this run performed. Accepting a draft route makes those renders unnecessary; "
                "the saving on future runs is not estimated here because they have not happened."
            )
        if report["validated"]:
            self.alerter.send(
                AlertEvent(
                    kind="api_routes_validated",
                    message=(
                        f"{report['validated']} structured route candidate(s) validated; "
                        "drafts are in the run report for review"
                    ),
                    context={"drafts": report["drafts"]},
                )
            )
        return report

    def _recover_budget(self) -> None:
        """Resolve reservations from a crashed process, and alert if money is lost."""

        if self.budget is None:
            return
        outcome = self.budget.recover_after_crash()
        if outcome["marked_unknown"]:
            self.alerter.send(
                AlertEvent(
                    kind="unknown_spend",
                    message=(
                        "reservations were submitted but never settled; paid work is "
                        "blocked until they are reconciled"
                    ),
                    context=outcome,
                )
            )

    def _close_browser_pool(self) -> None:
        """Shut the browser thread down deterministically, keeping its metrics.

        Metrics are read before the close, not after: a worker that has been
        shut down has nothing to report, and a run that leaves Chromium behind
        because reporting raised is worse than one with no browser numbers.
        """

        if self._browser is None:
            return
        self.metrics.browser = {
            **self._browser.metrics.to_dict(),
            **self._browser.pool_metrics,
        }
        self._browser.close()
        self._browser = None

    def _run_sweep(self) -> None:
        """Phase-A HEAD sweep: quarantine 404/410 before the main pass."""

        from web_scraper.fetchers.transports import UrllibTransport
        from web_scraper.run.sweep import sweep_dead_urls

        transport = UrllibTransport(allow_private=self.config.allow_private, use_cookies=False)
        head = getattr(transport, "head", None)
        if head is None:
            return
        pending = [
            row.url for row in self.queue.all_rows() if row.status.value in ("PENDING", "RETRY")
        ]
        sweep_dead_urls(
            pending,
            head=head,
            quarantine=lambda u, s: self.queue.quarantine_url(u, status_code=s),
        )

    # -- per-URL -----------------------------------------------------------

    def _process_guarded(
        self,
        queued: QueuedUrl,
        *,
        gateway: FetchGateway | None = None,
        phase: Phase | None = None,
    ) -> None:
        """Run one URL so that no failure can abort the run or lose the URL.

        Without this, an unexpected error (a malformed body crashing an
        extractor, a disk error) propagates out of the loop: the run produces no
        report at all and every claimed URL stays IN_PROGRESS. Here the URL gets
        a real verdict instead, and the run continues.
        """

        try:
            self._process(queued, gateway=gateway or self._gateway, phase=phase)
        except Exception:
            logger.exception("unhandled error while processing %s", queued.url)
            self.queue.mark_failed(queued.url, verdict=Verdict.PARSE_FAIL.value)
            self._results.append(Result(url=queued.url, verdict=Verdict.PARSE_FAIL))
            self.metrics.observe(
                Result(url=queued.url, verdict=Verdict.PARSE_FAIL), domain=_domain(queued.url)
            )
            self.alerter.send(
                AlertEvent(
                    kind="processing_error",
                    message=f"unhandled error while processing {queued.url}",
                    context={"url": queued.url},
                )
            )

    def _process(
        self,
        queued: QueuedUrl,
        *,
        gateway: FetchGateway | None = None,
        phase: Phase | None = None,
    ) -> None:
        gateway = gateway or self._gateway
        url = queued.url
        url_class = self.profile.class_for_url(url)
        if url_class is None:
            self.queue.mark_failed(url, verdict="PARSE_FAIL")
            self._results.append(Result(url=url, verdict=Verdict.PARSE_FAIL))
            return

        # Freshness gate: an unchanged, not-yet-due URL is skipped entirely.
        if not self.config.full_review and not self.freshness.is_due(url):
            self._results.append(Result(url=url, verdict=Verdict.NOT_MODIFIED))
            self.metrics.observe(Result(url=url, verdict=Verdict.NOT_MODIFIED), domain=_domain(url))
            self.queue.mark_done(url, verdict="NOT_MODIFIED")
            return

        conditional = self.freshness.conditional_headers(url)
        # In a paid phase this gateway carries an escalator; in a free phase it
        # physically cannot reach a provider.
        if phase is not None and phase.is_paid:
            self._paid_ledger.start(url, provider="?", strategy_id="?")
        outcome = gateway.fetch_url(url, extra_headers=conditional or None)
        if phase is not None and phase.is_paid:
            self._record_paid_outcome(url, outcome)
        result = outcome.result
        self._results.append(result)
        last = result.attempts[-1] if result.attempts else None
        self.queue.log_attempt(
            url,
            verdict=result.verdict.value,
            level=last.level.value if last else None,
            reason=last.reason if last else None,
        )
        self._route_verdict(queued, url_class, outcome)

    def _route_verdict(
        self, queued: QueuedUrl, url_class: UrlClass, outcome: GatewayOutcome
    ) -> None:
        url = queued.url
        result = outcome.result
        verdict = result.verdict
        response = outcome.response
        domain = _domain(url)

        if verdict is Verdict.DEAD_URL:
            self.queue.quarantine_url(
                url, status_code=result.attempts[-1].status if result.attempts else None
            )
            self.metrics.observe(result, domain=domain)
            return

        if verdict is Verdict.NOT_MODIFIED:
            self.freshness.record_result(url, not_modified=True)
            self.queue.mark_done(url, verdict="NOT_MODIFIED")
            self.metrics.observe(result, domain=domain)
            return

        if verdict in _RETRY_BACKOFF:
            self.queue.schedule_retry(
                url, verdict=verdict.value, delay_seconds=_RETRY_BACKOFF[verdict]
            )
            self.metrics.observe(result, domain=domain)
            return

        if verdict is Verdict.OK and response is not None:
            self._handle_ok(queued, url_class, result, response)
            return

        # Everything else (BLOCKED, SOFT_BLOCK, ACCESS_DENIED, AUTH_REQUIRED,
        # THIN_CONTENT, PARSE_FAIL, PROVIDER_ERROR): a hard non-OK outcome.
        history = [a.verdict.value for a in result.attempts]
        if queued.attempts + 1 >= self.config.dead_zone_after_attempts:
            snapshot = outcome.snapshot_paths[-1] if outcome.snapshot_paths else None
            self.queue.mark_dead_zone(url, verdict_history=history, last_snapshot=snapshot)
            self.alerter.send(
                AlertEvent(
                    kind="dead_zone",
                    message=f"{url} unresolved by any free route",
                    context={"verdict": verdict.value, "history": history},
                )
            )
        else:
            self.queue.mark_failed(url, verdict=verdict.value)
        self.metrics.observe(result, domain=domain)

    def _handle_ok(
        self,
        queued: QueuedUrl,
        url_class: UrlClass,
        result: Result,
        response: RawResponse,
    ) -> None:
        url = queued.url
        changed, new_hash = self.freshness.record_result(
            url, headers=response.headers, body=response.body
        )
        if not changed:
            # Fetched successfully but the content hash is identical: the
            # conditional request did not save the download. Tracking this
            # separates "we avoided a fetch" from "we paid for an unchanged page".
            self.metrics.fetched_unchanged += 1
        natural_key = queued.natural_key or normalize_url(url)

        extractors = [dict(e) for e in url_class.extractors]
        target_fields = list(url_class.required_fields) or ["title"]
        extraction = extract_fields(
            response.body, extractors=extractors, fields=target_fields, base_url=response.final_url
        )
        conflicts = 0
        if url_class.quorum_fields:
            quorum = run_quorum(
                response.body,
                extractors=extractors,
                quorum_fields=list(url_class.quorum_fields),
                base_url=response.final_url,
            )
            conflicts = len(quorum.conflicts)
            if conflicts:
                self.alerter.send(
                    AlertEvent(
                        kind="quorum_conflict",
                        message=f"extractor disagreement on {url}",
                        context={"fields": list(quorum.conflicts)},
                    )
                )

        self.dataset.stage(
            natural_key,
            url=url,
            data=extraction.data,
            content_hash=new_hash,
            conflict=bool(conflicts),
        )
        self.queue.mark_done(url, verdict="OK", content_hash=new_hash, natural_key=natural_key)
        self.metrics.observe(
            result, extractor_sources=extraction.sources, conflicts=conflicts, domain=_domain(url)
        )

    # -- finalize ----------------------------------------------------------

    def _availability_slo(self) -> dict[str, Any]:
        """How much of the published dataset a consumer may treat as current.

        Every record is judged against ITS OWN class's freshness window. The
        previous global ``min()`` across classes meant a site with hourly news
        and monthly guides judged every guide at the news window and reported
        perfectly current guides as stale — a correctness bug in the direction
        that matters, since a consumer trusting the report would re-fetch data
        that was fine.
        """

        windows = {
            name: float(cls.freshness.get("max_age_hours", 24)) * 3600.0
            for name, cls in self.profile.url_classes.items()
        }
        fallback = min(windows.values(), default=24.0 * 3600.0)

        verdicts: dict[str, str] = {}
        classes: dict[str, str] = {}
        for row in self.queue.all_rows():
            if not row.natural_key:
                continue
            if row.verdict:
                verdicts[row.natural_key] = row.verdict
            if row.url_class:
                classes[row.natural_key] = row.url_class

        records = build_availability(
            self.dataset.clean_rows_with_meta(),
            now=self._wall_clock(),
            max_age_seconds=fallback,
            verdicts_by_key=verdicts,
            max_age_by_url_class=windows,
            url_class_by_key=classes,
        )
        summary = summarize_availability(records).to_dict()
        # A global figure can read as healthy while one small, important class
        # is entirely stale. Both are reported.
        summary["by_url_class"] = summarize_by_url_class(records)
        return summary

    def _promote(self) -> dict[str, Any] | None:
        required = sorted(
            {
                f
                for cls in self.profile.url_classes.values()
                for f in (cls.quorum_fields or cls.required_fields)
            }
        )
        if not required:
            return None
        if not self.dataset.staged_rows():
            # Nothing changed this run (all fresh/unchanged): a no-op, not a failure.
            return {"ok": True, "reason": "no changes to promote", "staged": 0}
        min_completeness = min(
            (
                float(cls.promote.get("min_completeness", 0.95))
                for cls in self.profile.url_classes.values()
            ),
            default=0.95,
        )
        max_growth = max(
            (
                float(cls.promote.get("max_null_rate_growth", 2.0))
                for cls in self.profile.url_classes.values()
            ),
            default=2.0,
        )
        # Schema drift is checked BEFORE promoting, against the last healthy
        # dataset. Per-row validation cannot see a field that went uniformly
        # empty because a CSS class was renamed: every row is individually
        # valid, the count is right, and the data is wrong.
        drift = self._check_drift(required)
        if drift is not None and not drift.verdict.allows_promotion:
            self.alerter.send(
                AlertEvent(
                    kind="drift_block",
                    message="schema drift blocked promotion; the consumer stays on the LKG",
                    context={"explanation": drift.explain()},
                )
            )
            return {
                "ok": False,
                "reason": "schema drift",
                "staged": len(self.dataset.staged_rows()),
                "drift": drift.to_dict(),
            }

        decision = self.dataset.promote(
            required_fields=required,
            expected_count=None,
            min_completeness=min_completeness,
            max_null_rate_growth=max_growth,
        )
        payload = decision.to_dict()
        if drift is not None:
            payload["drift"] = drift.to_dict()
        if not decision.ok:
            self.alerter.send(
                AlertEvent(
                    kind="promote_rejected",
                    message="staging failed validation; clean dataset unchanged",
                    context={"reason": decision.reason, "completeness": decision.completeness},
                )
            )
        return payload

    def _check_drift(self, critical_fields: list[str]) -> DriftReport | None:
        """Compare the staged dataset's SHAPE against the last healthy one."""

        staged = self.dataset.staged_rows()
        if not staged:
            return None
        baseline_rows = self.dataset.clean_rows_with_meta()
        current = SchemaSnapshot.from_rows([dict(r.get("data") or {}) for r in staged])
        baseline = (
            SchemaSnapshot.from_rows([dict(r.get("data") or {}) for r in baseline_rows])
            if baseline_rows
            else None
        )
        return check_drift(current, baseline, critical_fields=critical_fields)

    def _final_alerts(self, report: dict[str, Any], promote: dict[str, Any] | None) -> None:
        if report["dead_zone_urls"]:
            self.alerter.send(
                AlertEvent(
                    kind="dead_zone",
                    message=f"{len(report['dead_zone_urls'])} dead zone(s) need review",
                    context={"count": len(report["dead_zone_urls"])},
                )
            )


def _domain(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc
