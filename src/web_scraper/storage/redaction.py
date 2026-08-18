"""Removal of secrets from headers before anything is persisted or reported."""

from __future__ import annotations

from typing import Mapping

SENSITIVE_HEADERS = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
        "x-amz-security-token",
    }
)

REDACTED = "[REDACTED]"


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
