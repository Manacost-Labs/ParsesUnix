"""Turning observed browser traffic into judged candidates, and back into routes.

The pipeline this implements is the whole point of discovery:

.. code-block:: text

    observed request/response
        -> is it noise?           analytics answer JSON too
        -> did it carry auth?     rendering authorised it, not us
        -> is it public?          a discovered URL is still an SSRF vector
        -> what shape is it?      schema signature, no values
        -> PROMISING
        -> seen on several pages, same shape
        -> VALIDATED
        -> profile draft, for a human to accept

Nothing here writes a route. The last step produces a draft an operator reads.
An endpoint that answered once during one render is a coincidence; treating it
as a production route because it looked good is how a crawl starts silently
depending on something that was never meant to be called.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from web_scraper.discovery.candidates import (
    AUTH_HEADERS,
    MAX_CANDIDATES_PER_PAGE,
    MAX_INSPECTED_BYTES,
    CandidateVerdict,
    PaginationHint,
    RouteCandidate,
    SchemaSignature,
    find_matching_fields,
    graphql_operation_of,
    is_noise,
    looks_like_api,
    normalize_endpoint,
    pagination_hint_of,
    redact_url,
)
from web_scraper.probe.safety import Resolver, UnsafeTarget, validate_public_url

logger = logging.getLogger(__name__)

#: How many pages must agree before a candidate is trusted. One page is a
#: coincidence; the same endpoint answering the same shape for several different
#: URLs is a pattern.
DEFAULT_MIN_PAGES = 2


@dataclass(frozen=True)
class ObservedRequest:
    """One network exchange the browser performed, already stripped of secrets.

    Header *names* are kept because they decide the verdict; header *values* are
    not, because keeping them is how a token reaches a report.
    """

    url: str
    method: str
    status: int
    content_type: str
    resource_type: str = ""
    request_header_names: tuple[str, ...] = ()
    request_body: str | None = None
    body: bytes = b""
    page_url: str = ""

    @property
    def carried_auth(self) -> bool:
        return any(name.lower() in AUTH_HEADERS for name in self.request_header_names)


@dataclass
class DiscoveryCollector:
    """Accumulates observations across pages and judges them at the end."""

    wanted_fields: tuple[str, ...] = ()
    allow_private: bool = False
    resolver: Resolver = None  # type: ignore[assignment]
    min_pages: int = DEFAULT_MIN_PAGES
    max_candidates: int = MAX_CANDIDATES_PER_PAGE
    _seen: dict[str, _Accumulated] = field(default_factory=dict, repr=False)
    #: Observations arrive on the browser thread while the run loop reads
    #: candidates from its own. One lock is cheaper than the class of bug that
    #: comes from assuming they never overlap.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.resolver is None:
            import socket

            self.resolver = socket.getaddrinfo

    def observe(self, request: ObservedRequest) -> None:
        """Fold one exchange in. Rejections are recorded, not dropped.

        A rejected candidate stays visible so an operator asking "why did it not
        find the API?" gets "it found it and refused it, because it needed a
        cookie" instead of silence.
        """

        identity = self._identity(request)
        # Screening happens outside the lock: it does DNS resolution, and
        # holding a lock across a network call would stall the run loop behind
        # the browser thread.
        verdict, detail = self._screen(request)
        document = self._parse(request) if verdict is CandidateVerdict.PROMISING else None

        with self._lock:
            self._record(identity, request, verdict, detail, document)

    def _record(
        self,
        identity: str,
        request: ObservedRequest,
        verdict: CandidateVerdict,
        detail: str,
        document: Any,
    ) -> None:
        if len(self._seen) >= self.max_candidates and identity not in self._seen:
            return

        existing = self._seen.get(identity)
        if existing is None:
            self._seen[identity] = _Accumulated(
                request=request, verdict=verdict, detail=detail, document=document
            )
            existing = self._seen[identity]
        else:
            existing.count += 1
            if request.page_url:
                existing.pages.add(request.page_url)
            if existing.document is None and document is not None:
                existing.document = document
        if request.page_url:
            existing.pages.add(request.page_url)

        if document is not None:
            signature = SchemaSignature.of(document)
            if existing.schema is None:
                existing.schema = signature
            elif not existing.schema.matches(signature):
                # The same endpoint answering two different shapes is not a
                # stable route, whatever else it looks like.
                existing.verdict = CandidateVerdict.REJECTED_UNSTABLE
                existing.detail = "the same endpoint returned two different schemas"

    def _identity(self, request: ObservedRequest) -> str:
        parts = [request.method.upper(), normalize_endpoint(request.url)]
        operation = graphql_operation_of(request.url, request.request_body)
        if operation:
            parts.append(operation)
        return " ".join(parts)

    def _screen(self, request: ObservedRequest) -> tuple[CandidateVerdict, str]:
        """The refusals, in the order that costs least to check."""

        if is_noise(request.url, request.resource_type):
            return CandidateVerdict.REJECTED_NOISE, "analytics, ads or tracking"
        if not looks_like_api(request.content_type, request.resource_type):
            return CandidateVerdict.REJECTED_WRONG_SCHEMA, "not a JSON data response"
        if request.carried_auth:
            # Seeing a request does not make it ours to replay. This one was
            # authorised by the session we were given for rendering.
            return (
                CandidateVerdict.REJECTED_AUTH,
                "the request carried authorisation; rendering authorised it, not us",
            )
        try:
            validate_public_url(
                request.url, allow_private=self.allow_private, resolver=self.resolver
            )
        except (UnsafeTarget, ValueError) as exc:
            # A discovered URL is still an SSRF vector. A rendered page can ask
            # the browser for anything, including the metadata service.
            return CandidateVerdict.REJECTED_PRIVATE, f"not a public target: {exc}"
        if not 200 <= request.status < 300:
            return CandidateVerdict.REJECTED_UNSTABLE, f"answered HTTP {request.status}"
        return CandidateVerdict.PROMISING, ""

    def _parse(self, request: ObservedRequest) -> Any:
        if not request.body:
            return None
        head = request.body[:MAX_INSPECTED_BYTES]
        try:
            return json.loads(head.decode("utf-8", errors="strict"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Truncated at our own ceiling, or simply not JSON. Either way we
            # cannot describe its shape, and guessing one would be worse.
            return None

    def candidates(self) -> list[RouteCandidate]:
        """Everything observed, judged, best first."""

        with self._lock:
            snapshot = dict(self._seen)

        out: list[RouteCandidate] = []
        for identity, seen in snapshot.items():
            verdict = seen.verdict
            if verdict is CandidateVerdict.PROMISING and len(seen.pages) >= self.min_pages:
                verdict = CandidateVerdict.VALIDATED

            document = seen.document
            matched = (
                find_matching_fields(document, self.wanted_fields)
                if document is not None and self.wanted_fields
                else {}
            )
            request = seen.request
            out.append(
                RouteCandidate(
                    # Redacted here, once, at the boundary where a candidate
                    # becomes something that gets written down.
                    url=redact_url(request.url),
                    method=request.method.upper(),
                    status=request.status,
                    content_type=request.content_type,
                    resource_type=request.resource_type,
                    same_origin=_same_origin(request.page_url, request.url),
                    response_bytes=len(request.body),
                    observed_count=seen.count,
                    schema=seen.schema,
                    pagination=(
                        pagination_hint_of(request.url, document)
                        if document is not None
                        else PaginationHint()
                    ),
                    graphql_operation=graphql_operation_of(request.url, request.request_body),
                    auth_required=request.carried_auth,
                    verdict=verdict,
                    rejection_detail=seen.detail,
                    matched_fields=matched,
                )
            )
            del identity
        out.sort(key=lambda c: (c.verdict.is_rejected, -c.observed_count, c.url))
        return out

    def usable(self) -> list[RouteCandidate]:
        return [c for c in self.candidates() if c.verdict.is_usable]


@dataclass
class _Accumulated:
    request: ObservedRequest
    verdict: CandidateVerdict
    detail: str
    document: Any = None
    schema: SchemaSignature | None = None
    count: int = 1
    pages: set[str] = field(default_factory=set)


def _same_origin(page_url: str, request_url: str) -> bool:
    if not page_url:
        return True
    page, target = urlsplit(page_url), urlsplit(request_url)
    return (page.scheme, page.netloc) == (target.scheme, target.netloc)


def profile_route_draft(
    candidate: RouteCandidate, *, route_id: str | None = None
) -> dict[str, Any]:
    """A Site Profile fragment an operator can read, edit and paste.

    Emitted as a draft, never written. The extractor paths come from where the
    wanted fields were actually observed, so the draft is checkable against the
    endpoint rather than being a template with names filled in.
    """

    if not candidate.verdict.is_usable:
        raise ValueError(
            f"{candidate.identity} is {candidate.verdict.value}; only a VALIDATED "
            "candidate may be proposed as a route"
        )

    name = route_id or _suggest_id(candidate)
    draft: dict[str, Any] = {
        "suggested_route": {
            "id": name,
            "type": "json_api",
            "level": "L0",
            "url": candidate.url,
            "method": candidate.method,
        },
        "extractor": {"kind": "json", "fields": dict(candidate.matched_fields)},
        "evidence": {
            "observed_count": candidate.observed_count,
            "confidence": candidate.confidence,
            "schema": candidate.schema.to_dict() if candidate.schema else None,
            "pagination": candidate.pagination.to_dict(),
        },
        "review": (
            "Proposed, not applied. Check the field paths against the endpoint and "
            "confirm the site permits calling it directly before adding this route."
        ),
    }
    if candidate.graphql_operation:
        draft["suggested_route"]["graphql_operation"] = candidate.graphql_operation
    return draft


def _suggest_id(candidate: RouteCandidate) -> str:
    if candidate.graphql_operation and candidate.graphql_operation != "unknown":
        return f"graphql-{candidate.graphql_operation.lower()}"
    segments = [s for s in urlsplit(candidate.url).path.split("/") if s and s != "{id}"]
    tail = segments[-1] if segments else "api"
    return f"{tail.replace('.', '-').lower()}-api"


def summarise(candidates: Sequence[RouteCandidate]) -> dict[str, Any]:
    """Counts an operator can act on, plus the human-readable listing."""

    by_verdict: dict[str, int] = {}
    for candidate in candidates:
        by_verdict[candidate.verdict.value] = by_verdict.get(candidate.verdict.value, 0) + 1
    usable = [c for c in candidates if c.verdict.is_usable]
    return {
        "candidates_found": len(candidates),
        "by_verdict": by_verdict,
        "validated": len(usable),
        "listing": "\n".join(c.describe() for c in candidates),
        "drafts": [profile_route_draft(c) for c in usable],
    }


def describe_report(candidates: Sequence[RouteCandidate]) -> str:
    """What a human reads in a terminal."""

    if not candidates:
        return "no structured route candidates found"
    usable = [c for c in candidates if c.verdict.is_usable]
    header = f"{len(candidates)} structured route candidate(s) found, {len(usable)} validated"
    body = "\n\n".join(
        f"{index}. {candidate.describe()}" for index, candidate in enumerate(candidates, start=1)
    )
    return f"{header}\n\n{body}"


def observed_from_mapping(payload: Mapping[str, Any], *, page_url: str = "") -> ObservedRequest:
    """Build an observation from a plain mapping, for fixtures and CLI input."""

    return ObservedRequest(
        url=str(payload.get("url", "")),
        method=str(payload.get("method", "GET")),
        status=int(payload.get("status", 200)),
        content_type=str(payload.get("content_type", "")),
        resource_type=str(payload.get("resource_type", "")),
        request_header_names=tuple(payload.get("request_header_names", ())),
        request_body=payload.get("request_body"),
        body=payload.get("body", b"") or b"",
        page_url=str(payload.get("page_url", page_url)),
    )
