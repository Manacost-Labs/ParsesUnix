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
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from web_scraper.contracts import Result, Verdict
from web_scraper.extract import extract_fields, run_quorum
from web_scraper.fetchers import CircuitBreaker, FetchGateway, RawResponse
from web_scraper.fetchers.browser_pool import BrowserPool
from web_scraper.fetchers.gateway import GatewayOutcome, default_transport_provider
from web_scraper.fingerprints import FingerprintStore
from web_scraper.freshness import FreshnessStore
from web_scraper.observability import Alerter, AlertEvent, LoggingAlerter, RunMetrics
from web_scraper.observability.accounting import build_accounting
from web_scraper.observability.metrics import build_report
from web_scraper.profiles import load_profile
from web_scraper.profiles.model import SiteProfile, UrlClass
from web_scraper.publish import DatasetStore, build_availability, summarize_availability
from web_scraper.queue import QueueStore, normalize_url
from web_scraper.queue.store import QueuedUrl
from web_scraper.routing import RouteStatsStore
from web_scraper.routing.router import AdaptiveRouter
from web_scraper.run.config import RunConfig
from web_scraper.storage.snapshots import SnapshotStore

logger = logging.getLogger(__name__)

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
        self.alerter = alerter or LoggingAlerter()
        self.metrics = RunMetrics()
        self._clock = clock
        self._wall_clock = wall_clock
        # One browser for the whole run, shut down in run(). An injected gateway
        # brings its own transports, so the runner does not build a pool for it.
        self._browser_pool: BrowserPool | None = None
        if gateway is None and config.browser_pool:
            self._browser_pool = BrowserPool(max_contexts=config.max_browser_contexts)
        self._gateway = gateway or FetchGateway(
            self.profile,
            transport_provider=default_transport_provider(browser_pool=self._browser_pool),
            snapshots=self.snapshots,
            breaker=CircuitBreaker(),
            route_stats=self.route_stats,
            fingerprints=self.fingerprints,
            router=AdaptiveRouter(self.route_stats) if config.adaptive_routing else None,
        )
        self._results: list[Result] = []

    # -- public ------------------------------------------------------------

    def run(self) -> RunResult:
        # Track whether each seeded URL actually reached the queue: a URL lost
        # between the caller and the ledger would otherwise be invisible.
        seeded: dict[str, bool] = {}
        for url in self.config.seed_urls:
            url_class = self.profile.class_for_url(url)
            self.queue.add(url, url_class=url_class.name if url_class else None)
            seeded[url] = self.queue.get(url) is not None

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
        while True:
            if (
                self.config.deadline_seconds is not None
                and (self._clock() - start) >= self.config.deadline_seconds
            ):
                break
            batch = self.queue.claim_batch(self.config.batch_size)
            if not batch:
                break
            for queued in batch:
                self._process_guarded(queued)
                processed += 1

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
        self._final_alerts(report.to_dict(), promote)
        return RunResult(report=report.to_dict(), promote=promote, processed=processed)

    def _close_browser_pool(self) -> None:
        if self._browser_pool is None:
            return
        self.metrics.browser = self._browser_pool.metrics.to_dict()
        self._browser_pool.close()
        self._browser_pool = None

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

    def _process_guarded(self, queued: QueuedUrl) -> None:
        """Run one URL so that no failure can abort the run or lose the URL.

        Without this, an unexpected error (a malformed body crashing an
        extractor, a disk error) propagates out of the loop: the run produces no
        report at all and every claimed URL stays IN_PROGRESS. Here the URL gets
        a real verdict instead, and the run continues.
        """

        try:
            self._process(queued)
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

    def _process(self, queued: QueuedUrl) -> None:
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
        outcome = self._gateway.fetch_url(url, extra_headers=conditional or None)
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
        """How much of the published dataset a consumer may treat as current."""

        max_age_hours = min(
            (
                float(cls.freshness.get("max_age_hours", 24))
                for cls in self.profile.url_classes.values()
            ),
            default=24.0,
        )
        verdicts = {
            row.natural_key: row.verdict
            for row in self.queue.all_rows()
            if row.natural_key and row.verdict
        }
        records = build_availability(
            self.dataset.clean_rows_with_meta(),
            now=self._wall_clock(),
            max_age_seconds=max_age_hours * 3600.0,
            verdicts_by_key=verdicts,
        )
        return summarize_availability(records).to_dict()

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
        decision = self.dataset.promote(
            required_fields=required,
            expected_count=None,
            min_completeness=min_completeness,
            max_null_rate_growth=max_growth,
        )
        if not decision.ok:
            self.alerter.send(
                AlertEvent(
                    kind="promote_rejected",
                    message="staging failed validation; clean dataset unchanged",
                    context={"reason": decision.reason, "completeness": decision.completeness},
                )
            )
        return decision.to_dict()

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
