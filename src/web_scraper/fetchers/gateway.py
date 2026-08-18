"""Fetch Gateway L0-L2: free routes only, triage after every attempt.

Escalation policy (the load-bearing invariant of this module):

- attempts start at the profile's primary route; routes at the same or a
  cheaper level are always allowed, in declared order;
- only ``BLOCKED`` and ``SOFT_BLOCK`` unlock the next level, and never
  above L2 — paid levels belong to provider adapters (stage 3);
- ``DEAD_URL``, ``AUTH_REQUIRED``, and ``ACCESS_DENIED`` are terminal;
- ``RATE_LIMITED`` (honoring ``Retry-After``) and ``ORIGIN_DOWN`` retry
  the same route with bounded backoff and never raise the level;
- ``PARSE_FAIL`` may try other same-or-cheaper routes and never raises
  the level.
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from web_scraper.contracts import (
    PAID_ESCALATION_VERDICTS,
    Attempt,
    ContentRules,
    Level,
    Result,
    Route,
    RouteType,
    TriageResult,
    Verdict,
)
from web_scraper.fetchers.base import RawResponse, Transport, TransportUnavailable
from web_scraper.fetchers.pacing import Pacer, parse_retry_after
from web_scraper.fetchers.sessions import SessionPool
from web_scraper.fetchers.transports import (
    PlaywrightRenderTransport,
    ScraplingStealthyTransport,
    UrllibTransport,
)
from web_scraper.probe.safety import Resolver
from web_scraper.profiles.model import SiteProfile, UrlClass
from web_scraper.storage.snapshots import SnapshotStore
from web_scraper.triage import classify_response

MAX_FREE_RANK = Level.L2.rank

TransportProvider = Callable[[Route, UrlClass, str], Transport]

_TERMINAL_VERDICTS = frozenset({Verdict.DEAD_URL, Verdict.AUTH_REQUIRED, Verdict.ACCESS_DENIED})
_TEMPLATE_RE = re.compile(r"\{[^}]+\}")


def rules_for_route(url_class: UrlClass, route: Route) -> ContentRules:
    """Adapt the class validation rules to the route's media type."""

    base = url_class.content_rules()
    if route.type is RouteType.JSON_API:
        return ContentRules(
            min_body_bytes=2,
            expected_content_type="json",
            required_json_paths=base.required_json_paths,
            stop_signatures=base.stop_signatures,
        )
    if route.type in {RouteType.RSS, RouteType.SITEMAP}:
        return ContentRules(
            min_body_bytes=25,
            expected_content_type="xml",
            stop_signatures=base.stop_signatures,
        )
    return base


def resolve_route_url(route: Route, target_url: str) -> str | None:
    """A route without a URL fetches the target itself; unresolved templates skip."""

    if not route.url:
        return target_url
    if _TEMPLATE_RE.search(route.url):
        return None
    return route.url


def default_transport_provider(
    *,
    allow_private: bool = False,
    resolver: Resolver = socket.getaddrinfo,
    timeout: float = 20.0,
    session_clock: Callable[[], float] = time.monotonic,
) -> TransportProvider:
    """L0 -> plain urllib, L1 -> per-domain cookie session, L2 -> browser."""

    shared_l0 = UrllibTransport(
        allow_private=allow_private, resolver=resolver, timeout=timeout, use_cookies=False
    )
    pool = SessionPool(
        lambda domain: UrllibTransport(
            allow_private=allow_private, resolver=resolver, timeout=timeout, use_cookies=True
        ),
        clock=session_clock,
    )

    def provider(route: Route, url_class: UrlClass, url: str) -> Transport:
        if route.level is Level.L0:
            return shared_l0
        if route.level is Level.L1:
            parts = urlsplit(url)
            warmup = (
                f"{parts.scheme}://{parts.netloc}/" if url_class.session.get("warmup") else None
            )
            return pool.get(
                parts.netloc,
                ttl_minutes=int(url_class.session.get("ttl_minutes", 30)),
                warmup_url=warmup,
            )
        if route.level is Level.L2:
            if route.type is RouteType.STEALTHY:
                return ScraplingStealthyTransport()
            return PlaywrightRenderTransport(
                allow_private=allow_private, resolver=resolver, timeout=max(timeout, 30.0)
            )
        raise TransportUnavailable(
            "paid levels are handled by provider adapters (stage 3), not the free gateway"
        )

    provider.session_pool = pool  # type: ignore[attr-defined]  # exposed for warmup introspection
    return provider


@dataclass(frozen=True)
class GatewayOutcome:
    result: Result
    response: RawResponse | None
    skipped_routes: tuple[dict, ...]
    snapshot_paths: tuple[str, ...]

    @property
    def paid_escalation_candidate(self) -> bool:
        return self.result.verdict in PAID_ESCALATION_VERDICTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result.to_dict(),
            "paid_escalation_candidate": self.paid_escalation_candidate,
            "skipped_routes": list(self.skipped_routes),
            "snapshot_paths": list(self.snapshot_paths),
        }


class FetchGateway:
    """Runs one URL through the free routes of its Site Profile class."""

    def __init__(
        self,
        profile: SiteProfile,
        *,
        transport_provider: TransportProvider | None = None,
        pacer: Pacer | None = None,
        snapshots: SnapshotStore | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.profile = profile
        self._provider = transport_provider or default_transport_provider()
        self._pacer = pacer or Pacer()
        self._snapshots = snapshots
        self._clock = clock

    def fetch_url(self, url: str) -> GatewayOutcome:
        url_class = self.profile.class_for_url(url)
        if url_class is None:
            raise ValueError(f"no url class in profile {self.profile.site!r} matches {url!r}")

        attempts: list[Attempt] = []
        skipped: list[dict] = []
        snapshot_paths: list[str] = []
        final_verdict: Verdict | None = None
        final_response: RawResponse | None = None

        plan = self._plan_routes(url_class, skipped)
        # The start level is set by the first route that actually executes;
        # after that, only BLOCKED/SOFT_BLOCK verdicts may raise it.
        unlocked_rank: int | None = None

        for route in plan:
            if unlocked_rank is not None and route.level.rank > unlocked_rank:
                skipped.append(
                    {
                        "route": route.to_dict(),
                        "reason": "level escalation is not justified by any BLOCKED/SOFT_BLOCK verdict",
                    }
                )
                continue

            target = resolve_route_url(route, url)
            if target is None:
                skipped.append(
                    {
                        "route": route.to_dict(),
                        "reason": "route URL template has unresolved placeholders",
                    }
                )
                continue

            try:
                transport = self._provider(route, url_class, target)
                triage, response = self._attempt_with_retries(
                    route=route,
                    target=target,
                    transport=transport,
                    url_class=url_class,
                    attempts=attempts,
                    snapshot_paths=snapshot_paths,
                )
            except TransportUnavailable as exc:
                skipped.append({"route": route.to_dict(), "reason": str(exc)})
                continue

            unlocked_rank = max(unlocked_rank or 0, route.level.rank)
            final_verdict = triage.verdict
            final_response = response
            if triage.verdict is Verdict.OK:
                break
            if triage.verdict in _TERMINAL_VERDICTS:
                break
            if triage.verdict in PAID_ESCALATION_VERDICTS:
                unlocked_rank = max(unlocked_rank, min(route.level.rank + 1, MAX_FREE_RANK))
            # RATE_LIMITED / ORIGIN_DOWN / PARSE_FAIL: continue without unlocking.

        if final_verdict is None:
            final_verdict = Verdict.PARSE_FAIL
            if not attempts:
                attempts.append(
                    Attempt(
                        url=url,
                        level=Level.L0,
                        verdict=final_verdict,
                        reason="no runnable free route in the profile",
                    )
                )

        result = Result(url=url, verdict=final_verdict, attempts=tuple(attempts))
        return GatewayOutcome(
            result=result,
            response=final_response,
            skipped_routes=tuple(skipped),
            snapshot_paths=tuple(snapshot_paths),
        )

    def _plan_routes(self, url_class: UrlClass, skipped: list[dict]) -> list[Route]:
        """Primary first, then alternatives cheapest-first; paid routes are reported, never run."""

        ordered = [url_class.primary_route] + sorted(
            url_class.alternative_routes, key=lambda route: route.level.rank
        )
        plan: list[Route] = []
        for route in ordered:
            if route.level.is_paid:
                skipped.append(
                    {
                        "route": route.to_dict(),
                        "reason": "paid levels are handled by provider adapters (stage 3), not the free gateway",
                    }
                )
                continue
            plan.append(route)
        return plan

    def _attempt_with_retries(
        self,
        *,
        route: Route,
        target: str,
        transport: Transport,
        url_class: UrlClass,
        attempts: list[Attempt],
        snapshot_paths: list[str],
    ) -> tuple[TriageResult, RawResponse | None]:
        max_attempts = int(url_class.retry.get("max_attempts", 2))
        backoff_seconds = float(url_class.retry.get("backoff_seconds", 5))
        rules = rules_for_route(url_class, route)
        domain = urlsplit(target).netloc

        triage: TriageResult | None = None
        response: RawResponse | None = None
        for attempt_no in range(1, max_attempts + 1):
            self._pacer.pause(domain)
            started = self._clock()
            response = transport.fetch(target)
            elapsed_ms = response.elapsed_ms
            if elapsed_ms is None:
                elapsed_ms = int((self._clock() - started) * 1000)

            triage = classify_response(
                status=response.status,
                body=response.body,
                headers=response.headers,
                rules=rules,
                transport_error=response.transport_error,
            )
            if self._snapshots is not None:
                path = self._snapshots.save(
                    url=target,
                    attempt_index=len(attempts) + 1,
                    response=response,
                    verdict=triage.verdict.value,
                )
                snapshot_paths.append(str(path))
            attempts.append(
                Attempt(
                    url=target,
                    level=route.level,
                    verdict=triage.verdict,
                    reason=triage.reason,
                    route=route,
                    status=response.status,
                    body_bytes=len(response.body),
                    elapsed_ms=elapsed_ms,
                )
            )

            if triage.verdict is Verdict.RATE_LIMITED and attempt_no < max_attempts:
                delay = parse_retry_after(response.headers)
                self._pacer.backoff(delay if delay is not None else backoff_seconds)
                continue
            if triage.verdict is Verdict.ORIGIN_DOWN and attempt_no < max_attempts:
                self._pacer.backoff(backoff_seconds * attempt_no)
                continue
            break

        assert triage is not None  # max_attempts >= 1 is enforced by the profile validator
        return triage, response
