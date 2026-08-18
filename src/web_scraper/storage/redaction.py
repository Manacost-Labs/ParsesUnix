"""Removal of secrets from headers, URLs, and bodies before anything is persisted."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_HEADERS = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "www-authenticate",
        "proxy-authenticate",
        "api-key",
        "x-api-key",
        "x-auth-token",
        "x-access-token",
        "x-csrf-token",
        "x-xsrf-token",
        "x-amz-security-token",
        "x-goog-api-key",
    }
)

#: Query-string parameter names whose values are redacted from stored URLs.
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "key",
        "api_key",
        "apikey",
        "access_token",
        "auth",
        "auth_token",
        "token",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "signature",
        "sig",
        "sid",
        "session",
        "sessionid",
        "session_id",
    }
)

#: Value patterns redacted from stored bodies (best-effort, not a guarantee).
BODY_SECRET_PATTERNS = (
    re.compile(rb"(?i)\bbearer\s+[a-z0-9._\-]{8,}"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"),
    re.compile(
        rb"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
    ),
    re.compile(
        rb"(?i)\"?(?:api[_-]?key|access[_-]?token|secret|password)\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{8,}\"?"
    ),
)

REDACTED = "[REDACTED]"
REDACTED_BYTES = b"[REDACTED]"


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Replace sensitive header values, keeping the fact of their presence."""

    return {
        str(key): (REDACTED if str(key).lower() in SENSITIVE_HEADERS else str(value))
        for key, value in headers.items()
    }


def drop_sensitive_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Remove sensitive headers entirely (used for captured request headers)."""

    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() not in SENSITIVE_HEADERS
    }


def redact_url(url: str) -> str:
    """Mask values of sensitive query parameters in a URL before storing it."""

    parts = urlsplit(url)
    if not parts.query:
        return url
    redacted_pairs = [
        (key, REDACTED if key.lower() in SENSITIVE_QUERY_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    new_query = urlencode(redacted_pairs)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def redact_body(body: bytes) -> bytes:
    """Best-effort masking of well-known secret shapes in a response body."""

    for pattern in BODY_SECRET_PATTERNS:
        body = pattern.sub(REDACTED_BYTES, body)
    return body
