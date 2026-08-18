"""Static probe v2: bounded reconnaissance producing a stable JSON report.

The report contract (``PROBE_REPORT_SCHEMA``) is the input for drafting a
Site Profile; see ``web_scraper.profiles.draft``.
"""

from __future__ import annotations

import hashlib
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import partial
from typing import Any
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request

from web_scraper.contracts import ContentRules
from web_scraper.probe import analysis
from web_scraper.probe.safety import Resolver, UnsafeTarget, build_safe_opener, validate_public_url
from web_scraper.triage import classify_response

PROBE_REPORT_SCHEMA = "web-scraper/probe-report/v2"
MAX_BODY_BYTES = 512_000
ROBOTS_MAX_BYTES = 128_000
SAFE_HEADERS = {"content-type", "content-length", "etag", "last-modified", "retry-after"}
USER_AGENT = "WebScraperSkill-Probe/0.2 (+authorized reconnaissance)"
ACCEPT = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.1"


@dataclass(frozen=True)
class FetchResult:
    """Raw outcome of one bounded GET, before any interpretation."""

    requested_url: str
    final_url: str
    status: int | None
    headers: dict[str, str]
    body: bytes
    truncated: bool
    redirect_chain: tuple[dict[str, Any], ...]
    transport_error: str | None = None


FetchFn = Callable[[str], FetchResult]


def default_fetch(
    url: str,
    *,
    allow_private: bool = False,
    resolver: Resolver = socket.getaddrinfo,
    max_body_bytes: int = MAX_BODY_BYTES,
    timeout: float = 20.0,
) -> FetchResult:
    chain: list[dict[str, Any]] = []
    opener = build_safe_opener(allow_private=allow_private, resolver=resolver, chain=chain)
    # No Range header: a server honoring Range returns exactly the requested
    # window, which would make every response look complete. Reading one byte
    # past the cap detects truncation honestly while still bounding the download.
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": ACCEPT,
        },
    )

    status: int | None = None
    final_url = url
    raw_headers: dict[str, str] = {}
    body = b""
    transport_error: str | None = None
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            final_url = response.geturl()
            validate_public_url(final_url, allow_private=allow_private, resolver=resolver)
            raw_headers = dict(response.headers.items())
            body = response.read(max_body_bytes + 1)
    except HTTPError as exc:
        status = exc.code
        final_url = exc.geturl()
        validate_public_url(final_url, allow_private=allow_private, resolver=resolver)
        raw_headers = dict(exc.headers.items()) if exc.headers else {}
        body = exc.read(max_body_bytes + 1)
    except (URLError, TimeoutError, OSError) as exc:
        transport_error = str(exc)

    truncated = len(body) > max_body_bytes
    return FetchResult(
        requested_url=url,
        final_url=final_url,
        status=status,
        headers=raw_headers,
        body=body[:max_body_bytes],
        truncated=truncated,
        redirect_chain=tuple(chain),
        transport_error=transport_error,
    )


def fetch_robots(
    origin: str,
    fetch: FetchFn,
    *,
    target_url: str | None = None,
    user_agent: str = USER_AGENT,
) -> dict[str, Any]:
    """Fetch and parse robots.txt: declared sitemaps, crawl delay, permission.

    ``target_allowed`` reflects an actual ``can_fetch`` check for ``target_url``.
    When robots.txt is absent or unreadable it defaults to True (permitted),
    matching the robots convention that no rules means no restriction.
    """

    robots_url = urljoin(origin, "/robots.txt")
    section: dict[str, Any] = {
        "url": robots_url,
        "fetched": False,
        "status": None,
        "sitemaps": [],
        "crawl_delay": None,
        "target_allowed": True,
        "checked_user_agent": user_agent,
    }
    try:
        outcome = fetch(robots_url)
    except (UnsafeTarget, ValueError) as exc:
        section["error"] = str(exc)
        return section
    section["status"] = outcome.status
    if outcome.transport_error or outcome.status is None:
        return section
    if outcome.status in {401, 403}:
        # An access-controlled robots.txt is treated as "disallow all" by the RFC.
        section["target_allowed"] = False
        return section
    if not (200 <= outcome.status < 300):
        return section

    text = outcome.body[:ROBOTS_MAX_BYTES].decode("utf-8", errors="ignore")
    parser = robotparser.RobotFileParser()
    parser.parse(text.splitlines())
    section["fetched"] = True
    section["sitemaps"] = list(parser.site_maps() or [])[:20]
    section["crawl_delay"] = parser.crawl_delay(user_agent) or parser.crawl_delay("*")
    if target_url is not None:
        section["target_allowed"] = parser.can_fetch(user_agent, target_url)
    return section


@dataclass(frozen=True)
class ProbeReport:
    """Stable JSON contract for one probed URL (schema v2)."""

    schema: str
    requested_url: str
    final_url: str
    status: int | None
    verdict: str
    reason: str
    redirect_chain: tuple[dict[str, Any], ...]
    headers: dict[str, str]
    fetch: dict[str, Any]
    robots: dict[str, Any]
    discovery: dict[str, Any]
    rendering: dict[str, Any]
    recommendation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["redirect_chain"] = list(self.redirect_chain)
        return payload


def probe(
    url: str,
    *,
    allow_private: bool = False,
    max_body_bytes: int = MAX_BODY_BYTES,
    include_robots: bool = True,
    timeout: float = 20.0,
    fetch: FetchFn | None = None,
    resolver: Resolver = socket.getaddrinfo,
) -> ProbeReport:
    validate_public_url(url, allow_private=allow_private, resolver=resolver)
    fetch_fn: FetchFn = fetch or partial(
        default_fetch,
        allow_private=allow_private,
        resolver=resolver,
        max_body_bytes=max_body_bytes,
        timeout=timeout,
    )

    outcome = fetch_fn(url)
    validate_public_url(outcome.final_url, allow_private=allow_private, resolver=resolver)
    for hop in outcome.redirect_chain:
        validate_public_url(hop["to"], allow_private=allow_private, resolver=resolver)

    content_type = next(
        (value for key, value in outcome.headers.items() if key.lower() == "content-type"), ""
    )
    triage = classify_response(
        status=outcome.status,
        body=outcome.body,
        headers=outcome.headers,
        transport_error=outcome.transport_error,
        rules=ContentRules(min_body_bytes=1),
    )

    robots_section: dict[str, Any] = {
        "url": None,
        "fetched": False,
        "sitemaps": [],
        "target_allowed": True,
    }
    if include_robots and outcome.transport_error is None:
        parts = urlsplit(outcome.final_url)
        robots_section = fetch_robots(
            f"{parts.scheme}://{parts.netloc}/", fetch_fn, target_url=outcome.final_url
        )

    discovery = analysis.discover(outcome.body, outcome.final_url, content_type)
    rendering = analysis.classify_rendering(outcome.body, content_type, discovery["app_state"])
    recommendation = analysis.recommend(discovery, rendering, robots_section, triage.verdict.value)

    safe_headers = {
        key: value for key, value in outcome.headers.items() if key.lower() in SAFE_HEADERS
    }
    return ProbeReport(
        schema=PROBE_REPORT_SCHEMA,
        requested_url=url,
        final_url=outcome.final_url,
        status=outcome.status,
        verdict=triage.verdict.value,
        reason=triage.reason,
        redirect_chain=outcome.redirect_chain,
        headers=safe_headers,
        fetch={
            "content_type": content_type,
            "body_bytes": len(outcome.body),
            "truncated": outcome.truncated,
            "sha256": hashlib.sha256(outcome.body).hexdigest(),
            "transport_error": outcome.transport_error,
        },
        robots=robots_section,
        discovery=discovery,
        rendering=rendering,
        recommendation=recommendation,
    )
