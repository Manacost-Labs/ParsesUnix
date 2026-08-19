"""What the browser saw, turned into routes worth considering.

A page that needs a browser is expensive forever unless something changes. The
change worth making is almost always the same: the page fetched its data from an
endpoint, and that endpoint is usually cheaper, faster and far more stable than
the rendered markup around it. This module turns observed network traffic into
typed candidates for that endpoint.

Three refusals shape it.

**Discovery is not permission.** Seeing a request does not make it ours to
replay. Anything that carried an ``Authorization`` header, a cookie, or a CSRF
token was authorised by a session we were given for rendering, not by a licence
to call it directly. Those candidates are rejected, not stored and reconsidered.

**A candidate is not a route.** Nothing here writes into a Site Profile.
Candidates are validated and then *proposed*; an operator decides. An endpoint
that answered once during one render is a coincidence until it has been shown to
answer the same shape for several different pages.

**Signatures describe shape, never content.** A schema signature records that a
field is a string, never which string. Signatures are stored, compared across
runs and printed in reports, and a signature carrying real values would spread
whatever the endpoint returned into all three.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Query parameters whose values are secrets. Redacted before a URL is stored,
#: because candidates end up in reports, logs and profile drafts.
SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "token",
        "access_token",
        "auth",
        "auth_token",
        "key",
        "apikey",
        "api_key",
        "signature",
        "sig",
        "secret",
        "password",
        "session",
        "sid",
    }
)

#: Request headers that mean "this call was authorised for us, not by us".
AUTH_HEADERS = frozenset(
    {"authorization", "cookie", "x-csrf-token", "x-xsrf-token", "x-api-key", "proxy-authorization"}
)

#: Content types worth considering as a data endpoint.
API_CONTENT_TYPES = (
    "application/json",
    "application/ld+json",
    "application/graphql-response+json",
    "text/json",
)

#: Resource types that are never a data endpoint, however they are labelled.
IGNORED_RESOURCE_TYPES = frozenset(
    {
        "image",
        "media",
        "font",
        "stylesheet",
        "script",
        "manifest",
        "texttrack",
        "websocket",
        "other",
    }
)

#: Hosts that are analytics, ads or error reporting. Their JSON is not our data.
NOISE_HOST_MARKERS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook.com/tr",
    "segment.io",
    "segment.com",
    "amplitude.com",
    "mixpanel.com",
    "sentry.io",
    "bugsnag",
    "newrelic",
    "hotjar",
    "intercom.io",
    "cloudflareinsights",
    "adsbygoogle",
)

#: Paths that mark a request as telemetry rather than content.
NOISE_PATH_MARKERS = ("/collect", "/track", "/telemetry", "/beacon", "/pixel", "/analytics")

#: How much of a response body to inspect. A candidate is identified by shape,
#: and shape is visible in the first slice; buffering whole payloads to classify
#: them would make discovery cost more than the render it rides along with.
MAX_INSPECTED_BYTES = 256_000

#: Ceilings per page, so one chatty application cannot fill a report.
MAX_CANDIDATES_PER_PAGE = 25

#: Numeric or hash-like path segments, replaced when deriving identity so that
#: /api/user/1 and /api/user/2 are recognised as one endpoint.
_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{8,}|[0-9a-fA-F-]{32,})$")

#: Query parameters that only move through a result set. Two pages of one
#: endpoint are one endpoint.
PAGINATION_PARAMS = frozenset(
    {"page", "offset", "start", "skip", "cursor", "after", "before", "from", "p", "limit", "size"}
)

#: JSON keys that indicate how a listing is paged.
_OFFSET_KEYS = ("offset", "start", "skip")
_CURSOR_KEYS = ("cursor", "next_cursor", "endcursor", "next", "after")
_PAGE_KEYS = ("page", "page_number", "current_page")
_MORE_KEYS = ("has_more", "hasnextpage", "has_next", "more")
_TOTAL_KEYS = ("total", "total_count", "count", "totalresults")


class CandidateVerdict(StrEnum):
    """Whether a discovered endpoint may become a route."""

    #: Looks like a data endpoint. Not yet proven across pages.
    PROMISING = "PROMISING"
    #: Answered consistently on several representative URLs.
    VALIDATED = "VALIDATED"

    REJECTED_AUTH = "REJECTED_AUTH"
    REJECTED_PRIVATE = "REJECTED_PRIVATE"
    REJECTED_UNSTABLE = "REJECTED_UNSTABLE"
    REJECTED_WRONG_SCHEMA = "REJECTED_WRONG_SCHEMA"
    REJECTED_NOISE = "REJECTED_NOISE"

    @property
    def is_usable(self) -> bool:
        return self is CandidateVerdict.VALIDATED

    @property
    def is_rejected(self) -> bool:
        return self.value.startswith("REJECTED_")


@dataclass(frozen=True)
class SchemaSignature:
    """The shape of a JSON document, with none of its contents.

    Rendered as a small tree of type names. Two responses from the same endpoint
    produce the same signature even though their values differ, which is what
    makes "did this endpoint change?" answerable without storing user data.
    """

    signature: str
    depth: int = 0
    leaf_count: int = 0

    @classmethod
    def of(cls, document: Any, *, max_depth: int = 6, max_keys: int = 40) -> SchemaSignature:
        leaves = [0]
        text = _describe(document, 0, max_depth, max_keys, leaves)
        return cls(signature=text, depth=_depth_of(document, max_depth), leaf_count=leaves[0])

    def matches(self, other: SchemaSignature) -> bool:
        return self.signature == other.signature

    def to_dict(self) -> dict[str, Any]:
        return {"signature": self.signature, "depth": self.depth, "leaf_count": self.leaf_count}


@dataclass(frozen=True)
class PaginationHint:
    """How this endpoint appears to page, in the project's own vocabulary."""

    strategy: str = "NONE"
    parameters: tuple[str, ...] = ()
    cursor_field: str | None = None
    total_field: str | None = None
    has_more_field: str | None = None

    @property
    def is_paged(self) -> bool:
        return self.strategy != "NONE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "parameters": list(self.parameters),
            "cursor_field": self.cursor_field,
            "total_field": self.total_field,
            "has_more_field": self.has_more_field,
        }


@dataclass(frozen=True)
class RouteCandidate:
    """One observed endpoint, described well enough to judge."""

    url: str
    method: str
    status: int
    content_type: str
    resource_type: str = ""
    same_origin: bool = True
    response_bytes: int = 0
    observed_count: int = 1
    schema: SchemaSignature | None = None
    pagination: PaginationHint = field(default_factory=PaginationHint)
    graphql_operation: str | None = None
    auth_required: bool = False
    verdict: CandidateVerdict = CandidateVerdict.PROMISING
    rejection_detail: str = ""
    #: Fields the caller was looking for, and where they were found.
    matched_fields: dict[str, str] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        """Stable identity: method + normalised path (+ operation for GraphQL).

        Pagination parameters and id-shaped path segments are removed, so
        ``/api/items?page=1`` and ``/api/items?page=2`` are recognised as one
        endpoint rather than reported as two.
        """

        parts = [self.method.upper(), normalize_endpoint(self.url)]
        if self.graphql_operation:
            parts.append(self.graphql_operation)
        return " ".join(parts)

    @property
    def confidence(self) -> str:
        """How much this looks like a real, reusable data endpoint."""

        if self.verdict.is_rejected:
            return "NONE"
        score = 0
        score += 2 if self.matched_fields else 0
        score += 1 if self.same_origin else -1
        score += 1 if self.observed_count > 1 else 0
        score += 1 if self.schema and self.schema.leaf_count >= 3 else 0
        score += 1 if self.verdict is CandidateVerdict.VALIDATED else 0
        if score >= 4:
            return "HIGH"
        return "MEDIUM" if score >= 2 else "LOW"

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "url": self.url,
            "method": self.method,
            "status": self.status,
            "content_type": self.content_type,
            "resource_type": self.resource_type,
            "same_origin": self.same_origin,
            "response_bytes": self.response_bytes,
            "observed_count": self.observed_count,
            "schema": self.schema.to_dict() if self.schema else None,
            "pagination": self.pagination.to_dict(),
            "graphql_operation": self.graphql_operation,
            "auth_required": self.auth_required,
            "verdict": self.verdict.value,
            "rejection_detail": self.rejection_detail,
            "confidence": self.confidence,
            "matched_fields": dict(self.matched_fields),
        }

    def describe(self) -> str:
        lines = [
            f"{self.method} {self.url}",
            f"   {self.content_type}, observed {self.observed_count}x, "
            f"{'same-origin' if self.same_origin else 'cross-origin'}",
            f"   auth: {'yes' if self.auth_required else 'no'}",
        ]
        if self.graphql_operation:
            lines.append(f"   graphql operation: {self.graphql_operation}")
        if self.pagination.is_paged:
            lines.append(f"   pagination: {self.pagination.strategy.lower()}")
        if self.matched_fields:
            lines.append(f"   fields: {', '.join(sorted(self.matched_fields))}")
        lines.append(f"   verdict: {self.verdict.value}, confidence: {self.confidence}")
        if self.rejection_detail:
            lines.append(f"   {self.rejection_detail}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------


def redact_url(url: str) -> str:
    """Remove secret query values before a URL is stored anywhere.

    Candidates end up in reports, logs and profile drafts. A signed URL captured
    during a render would otherwise be copied into all three.
    """

    parts = urlsplit(url)
    if not parts.query:
        return url
    cleaned = [
        (key, "REDACTED" if key.lower() in SENSITIVE_QUERY_PARAMS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(cleaned)))


def normalize_endpoint(url: str) -> str:
    """Identity-bearing form of an endpoint.

    Two kinds of query parameter are treated differently, and the distinction is
    the whole reason this function exists. A pagination parameter says *where in
    a result set* you are, so ``?page=1`` and ``?page=2`` are one endpoint and
    are collapsed. Any other parameter says *which* result set, so ``?region=eu``
    and ``?region=us`` are different endpoints and keep their values — promoting
    one as a route and silently serving the other would be a data error, not a
    tidiness one.

    Id-shaped path segments collapse for the same reason as pagination:
    ``/user/1`` and ``/user/2`` are one endpoint with a parameter.
    """

    parts = urlsplit(url)
    segments = [
        "{id}" if _ID_SEGMENT.match(segment) else segment for segment in parts.path.split("/")
    ]
    kept = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in PAGINATION_PARAMS
    )
    query = urlencode(kept)
    return urlunsplit(("", parts.netloc, "/".join(segments), query, ""))


def is_noise(url: str, resource_type: str = "") -> bool:
    """Analytics, ads and error reporting answer JSON too. It is not our data."""

    if resource_type and resource_type.lower() in IGNORED_RESOURCE_TYPES:
        return True
    lowered = url.lower()
    if any(marker in lowered for marker in NOISE_HOST_MARKERS):
        return True
    path = urlsplit(lowered).path
    return any(marker in path for marker in NOISE_PATH_MARKERS)


def looks_like_api(content_type: str, resource_type: str = "") -> bool:
    base = content_type.split(";")[0].strip().lower()
    if any(base.startswith(t) for t in API_CONTENT_TYPES):
        return True
    return resource_type.lower() in {"xhr", "fetch"} and base in {"", "text/plain"}


# ---------------------------------------------------------------------------
# Schema signature
# ---------------------------------------------------------------------------


def _describe(value: Any, depth: int, max_depth: int, max_keys: int, leaves: list[int]) -> str:
    if depth >= max_depth:
        return "..."
    if isinstance(value, Mapping):
        keys = sorted(value)[:max_keys]
        inner = ", ".join(
            f"{key}: {_describe(value[key], depth + 1, max_depth, max_keys, leaves)}"
            for key in keys
        )
        return "object {" + inner + "}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "array<empty>"
        # One element describes the array: recording every element's shape would
        # make the signature grow with the data rather than with the schema.
        return f"array<{_describe(value[0], depth + 1, max_depth, max_keys, leaves)}>"
    leaves[0] += 1
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "string"


def _depth_of(value: Any, cap: int, level: int = 0) -> int:
    if level >= cap:
        return level
    if isinstance(value, Mapping):
        return max((_depth_of(v, cap, level + 1) for v in value.values()), default=level)
    if isinstance(value, (list, tuple)) and value:
        return _depth_of(value[0], cap, level + 1)
    return level


# ---------------------------------------------------------------------------
# GraphQL and pagination
# ---------------------------------------------------------------------------


def graphql_operation_of(url: str, request_body: str | None) -> str | None:
    """The operation name, which is the only part of a GraphQL call worth storing.

    Variables are deliberately not captured: they routinely carry ids, tokens
    and user input, and a stored variable set is a stored secret waiting to be
    printed in a report.
    """

    if "/graphql" not in urlsplit(url).path.lower() and not request_body:
        return None
    if not request_body:
        return "unknown"
    try:
        payload = json.loads(request_body)
    except json.JSONDecodeError:
        return "unknown"
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, Mapping):
        return None
    name = payload.get("operationName")
    if isinstance(name, str) and name:
        return name
    return "unknown" if "query" in payload else None


def pagination_hint_of(url: str, document: Any) -> PaginationHint:
    """Read paging from the URL's parameters and the body's own vocabulary."""

    params = {key.lower() for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)}
    keys = _all_keys(document)

    cursor_field = next((k for k in _CURSOR_KEYS if k in keys), None)
    total_field = next((k for k in _TOTAL_KEYS if k in keys), None)
    more_field = next((k for k in _MORE_KEYS if k in keys), None)

    # GraphQL states its paging explicitly, so it is checked first.
    if (
        ("pageinfo" in keys and (cursor_field or more_field))
        or cursor_field
        or params & {"cursor", "after", "before"}
    ):
        strategy = "CURSOR"
    elif params & {"offset", "skip", "start"} or any(k in keys for k in _OFFSET_KEYS):
        strategy = "OFFSET"
    elif params & {"page", "p"} or any(k in keys for k in _PAGE_KEYS):
        strategy = "PAGE"
    else:
        strategy = "NONE"

    return PaginationHint(
        strategy=strategy,
        parameters=tuple(sorted(params & PAGINATION_PARAMS)),
        cursor_field=cursor_field,
        total_field=total_field,
        has_more_field=more_field,
    )


def _all_keys(document: Any, depth: int = 0) -> set[str]:
    if depth > 4:
        return set()
    found: set[str] = set()
    if isinstance(document, Mapping):
        for key, value in document.items():
            found.add(str(key).lower())
            found |= _all_keys(value, depth + 1)
    elif isinstance(document, (list, tuple)) and document:
        found |= _all_keys(document[0], depth + 1)
    return found


def find_matching_fields(document: Any, wanted: Sequence[str]) -> dict[str, str]:
    """Where each wanted field appears, as a path. Values are never recorded."""

    targets = {name.lower(): name for name in wanted}
    found: dict[str, str] = {}

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > 6 or len(found) == len(targets):
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                here = f"{path}.{key}" if path else str(key)
                lowered = str(key).lower()
                if lowered in targets and targets[lowered] not in found:
                    found[targets[lowered]] = here
                walk(value, here, depth + 1)
        elif isinstance(node, (list, tuple)) and node:
            walk(node[0], f"{path}[*]" if path else "[*]", depth + 1)

    walk(document, "", 0)
    return found
