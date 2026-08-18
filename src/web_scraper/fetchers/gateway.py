"""Fetch Gateway L0-L2: free routes only, triage after every attempt.

Escalation policy (the load-bearing invariant of this module):

- attempts start at the profile's primary route; routes at the same or a
  cheaper level are always allowed, in declared order;
- only ``BLOCKED`` and ``SOFT_BLOCK`` unlock a higher level (up to L2, the
  highest free level); cheapest-first ordering still tries any cheaper route
  first — paid levels belong to provider adapters (stage 3);
- ``DEAD_URL``, ``AUTH_REQUIRED``, and ``ACCESS_DENIED`` are terminal;
- ``RATE_LIMITED`` (honoring ``Retry-After`` on every attempt) and
  ``ORIGIN_DOWN`` retry the same route with bounded backoff and never raise
  the level;
- ``PARSE_FAIL`` and ``THIN_CONTENT`` may try other same-or-cheaper routes and
  never raise the level;
- a per-domain circuit breaker short-circuits a domain that keeps hard-failing.
"""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from web_scraper.contracts import (
    FREE_ESCALATION_VERDICTS,
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
from web_scraper.fetchers.circuit import CircuitBreaker
from web_scraper.fetchers.pacing import Pacer, parse_retry_after
from web_scraper.fetchers.sessions import SessionPool
from web_scraper.fetchers.transports import (
    PlaywrightRenderTransport,
    ScraplingStealthyTransport,
    UrllibTransport,
)
from web_scraper.fingerprints import FailureFingerprint, FingerprintStore, fingerprint_attempt
from web_scraper.probe.safety import Resolver, UnsafeTarget
from web_scraper.profiles.model import SiteProfile, UrlClass
from web_scraper.routing.router import AdaptiveRouter
from web_scraper.routing.stats import RouteKey, RouteStatsStore
from web_scraper.storage.snapshots import SnapshotStore
from web_scraper.triage import classify_response

MAX_FREE_RANK = Level.L2.rank

TransportProvider = Callable[[Route, UrlClass, str], Transport]

_TERMINAL_VERDICTS = frozenset(
    {Verdict.DEAD_URL, Verdict.AUTH_REQUIRED, Verdict.ACCESS_DENIED, Verdict.NOT_MODIFIED}
)
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
    skipped_routes: tuple[dict[str, Any], ...]
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
        breaker: CircuitBreaker | None = None,
        router: AdaptiveRouter | None = None,
        route_stats: RouteStatsStore | None = None,
        fingerprints: FingerprintStore | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.profile = profile
        self._provider = transport_provider or default_transport_provider()
        self._pacer = pacer or Pacer()
        self._snapshots = snapshots
        self._breaker = breaker if breaker is not None else CircuitBreaker()
        # Both are optional: without them the gateway behaves exactly as before,
        # and the router with no statistics reproduces the declared plan anyway.
        self._router = router
        self._route_stats = route_stats
        self._fingerprints = fingerprints
        self._clock = clock

    def fetch_url(self, url: str, *, extra_headers: dict[str, str] | None = None) -> GatewayOutcome:
        url_class = self.profile.class_for_url(url)
        if url_class is None:
            raise ValueError(f"no url class in profile {self.profile.site!r} matches {url!r}")

        domain = urlsplit(url).netloc
        if self._breaker.is_open(domain):
            state = self._breaker.state(domain)
            verdict = state.verdict or Verdict.ORIGIN_DOWN
            attempt = Attempt(
                url=url,
                level=Level.L0,
                verdict=verdict,
                reason=f"circuit breaker open for {domain} after {state.consecutive} hard failures",
            )
            return GatewayOutcome(
                result=Result(url=url, verdict=verdict, attempts=(attempt,)),
                response=None,
                skipped_routes=({"route": None, "reason": "circuit breaker open"},),
                snapshot_paths=(),
            )

        attempts: list[Attempt] = []
        skipped: list[dict[str, Any]] = []
        snapshot_paths: list[str] = []
        final_verdict: Verdict | None = None
        final_response: RawResponse | None = None

        plan = self._plan_routes(url_class, skipped, domain=domain)
        first_failure: FailureFingerprint | None = None
        # The start level is set by the first route that actually executes;
        # after that, only a BLOCKED/SOFT_BLOCK verdict may raise it — up to the
        # highest free level. Cheapest-first ordering guarantees a cheaper route
        # is still tried before an unlocked more expensive one.
        unlocked_rank: int | None = None

        # An explicit queue rather than `for route in plan`: recognising a failure
        # may reorder the routes that have not run yet, and rebinding the name a
        # for-loop is iterating would silently have no effect.
        remaining = list(plan)
        while remaining:
            route = remaining.pop(0)
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
                    extra_headers=extra_headers,
                    stats_domain=domain,
                )
            except TransportUnavailable as exc:
                skipped.append({"route": route.to_dict(), "reason": str(exc)})
                continue
            except (UnsafeTarget, ValueError) as exc:
                # A malformed or unsafe route URL must not crash the whole URL:
                # record it and try the next route.
                skipped.append(
                    {"route": route.to_dict(), "reason": f"unsafe or invalid route URL: {exc}"}
                )
                continue

            unlocked_rank = max(unlocked_rank or 0, route.level.rank)
            final_verdict = triage.verdict
            final_response = response

            if triage.verdict is Verdict.OK:
                # A route that got past a failure we recognise is the most useful
                # thing we can learn: record it against that failure's shape.
                if first_failure is not None and self._fingerprints is not None:
                    self._fingerprints.record_recovery(
                        first_failure.digest, route_id=route.route_id
                    )
            elif response is not None and self._fingerprints is not None:
                shape = fingerprint_attempt(
                    verdict=triage.verdict,
                    status=response.status,
                    body=response.body,
                    headers=response.headers,
                    transport_error=response.transport_error,
                    domain=domain,
                    url_class=url_class.name,
                )
                self._fingerprints.record_failure(shape, route_id=route.route_id)
                if first_failure is None:
                    first_failure = shape
                    # Recognised failure: try the route that historically recovers
                    # it next. This only reorders what the plan already allows —
                    # the escalation policy below still gates every level.
                    remaining = self._prefer_recovery_route(remaining, shape)
            if triage.verdict is Verdict.OK:
                break
            if triage.verdict in _TERMINAL_VERDICTS:
                break
            if triage.verdict in FREE_ESCALATION_VERDICTS:
                # Unlock up to the highest free level; ordering still tries any
                # cheaper route first, so this cannot skip a cheaper door.
                unlocked_rank = MAX_FREE_RANK
            # RATE_LIMITED / ORIGIN_DOWN / PARSE_FAIL / THIN_CONTENT:
            # try same-or-cheaper routes without unlocking a higher level.

        if final_verdict is None:
            # Every route was skipped (templates, unsafe URLs, unavailable
            # transports). Preserve any real verdict already recorded rather than
            # inventing a PARSE_FAIL that would lose an escalation signal.
            if attempts:
                final_verdict = attempts[-1].verdict
            else:
                final_verdict = Verdict.PARSE_FAIL
                attempts.append(
                    Attempt(
                        url=url,
                        level=Level.L0,
                        verdict=final_verdict,
                        reason="no runnable free route in the profile",
                    )
                )

        self._breaker.record(domain, final_verdict)
        result = Result(url=url, verdict=final_verdict, attempts=tuple(attempts))
        return GatewayOutcome(
            result=result,
            response=final_response,
            skipped_routes=tuple(skipped),
            snapshot_paths=tuple(snapshot_paths),
        )

    def _plan_routes(
        self,
        url_class: UrlClass,
        skipped: list[dict[str, Any]],
        *,
        domain: str = "",
    ) -> list[Route]:
        """Primary first, then alternatives cheapest-first; paid routes are reported, never run.

        When a router is configured it may reorder this plan from what past runs
        actually achieved. It can only reorder — the paid-route exclusion and the
        deduplication below still apply, and with no statistics the router
        returns exactly this order.
        """

        ordered = [
            url_class.primary_route,
            *sorted(url_class.alternative_routes, key=lambda route: route.level.rank),
        ]
        plan: list[Route] = []
        seen: set[tuple[str, str, str | None]] = set()
        for route in ordered:
            if route.level.is_paid:
                skipped.append(
                    {
                        "route": route.to_dict(),
                        "reason": "paid levels are handled by provider adapters (stage 3), not the free gateway",
                    }
                )
                continue
            key = (route.type.value, route.level.value, route.url)
            if key in seen:
                skipped.append({"route": route.to_dict(), "reason": "duplicate route"})
                continue
            seen.add(key)
            plan.append(route)

        if self._router is None or not plan:
            return plan
        return self._router.order(plan, domain=domain, url_class=url_class.name)

    def _prefer_recovery_route(
        self, remaining: list[Route], shape: FailureFingerprint
    ) -> list[Route]:
        """Move a historically-recovering route to the front of what is left.

        This is a *hint*, not a decision: it only reorders routes the plan already
        contains, so the paid exclusion and the escalation policy still apply
        unchanged. A fingerprint can make us reach the working door sooner; it can
        never open a door policy has closed.
        """

        if self._fingerprints is None or len(remaining) < 2:
            return remaining
        hint = self._fingerprints.recovery_hint(shape)
        if hint is None:
            return remaining
        preferred = next((r for r in remaining if r.route_id == hint.route_id), None)
        if preferred is None:
            return remaining
        return [preferred, *(r for r in remaining if r is not preferred)]

    def _record_route_stats(
        self,
        *,
        route: Route,
        url_class: UrlClass,
        domain: str,
        triage: TriageResult,
        latency_ms: float | None,
    ) -> None:
        """Feed one attempt's outcome back into route memory."""

        if self._route_stats is None:
            return
        self._route_stats.record(
            RouteKey.for_route(route, domain=domain, url_class=url_class.name),
            verdict=triage.verdict,
            latency_ms=latency_ms,
        )

    def _attempt_with_retries(
        self,
        *,
        route: Route,
        target: str,
        transport: Transport,
        url_class: UrlClass,
        attempts: list[Attempt],
        snapshot_paths: list[str],
        extra_headers: dict[str, str] | None = None,
        stats_domain: str = "",
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
            try:
                response = transport.fetch(target, headers=extra_headers)
            except TransportUnavailable:
                # If earlier attempts on this route already produced a verdict,
                # keep it instead of discarding the route entirely.
                if triage is not None:
                    break
                raise
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
            # Every attempt feeds route memory, including the retries: a route
            # that only works on the second try is genuinely less reliable.
            self._record_route_stats(
                route=route,
                url_class=url_class,
                domain=stats_domain or domain,
                triage=triage,
                latency_ms=elapsed_ms,
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

            if triage.verdict is Verdict.RATE_LIMITED:
                # Honor Retry-After even on the terminal attempt, so moving to the
                # next same-domain route does not immediately re-hit the target.
                delay = parse_retry_after(response.headers)
                self._pacer.backoff(delay if delay is not None else backoff_seconds)
                if attempt_no < max_attempts:
                    continue
                break
            if triage.verdict is Verdict.ORIGIN_DOWN and attempt_no < max_attempts:
                self._pacer.backoff(backoff_seconds * attempt_no)
                continue
            break

        assert triage is not None  # max_attempts >= 1 is enforced by the profile validator
        return triage, response
