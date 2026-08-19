"""Shared HTTP plumbing for provider adapters.

Every adapter needs the same three things: send a request, survive the ways a
network call can fail, and never let a provider-side failure masquerade as a
verdict about the target site. Duplicating that across three vendors is how the
adapters drift apart — one grows a timeout mapping the others lack, and the
router starts comparing statistics gathered under different rules.

This module is deliberately thin. Vendor-specific status interpretation stays in
each adapter, because only the adapter knows whether a 403 came from the vendor
rejecting our key or from the site rejecting the vendor.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from web_scraper.providers.base import ProviderError, ProviderErrorKind

#: Ceiling on a provider response body. The free transports have always had one;
#: the paid path did not, which meant a single malfunctioning provider response
#: could read unbounded bytes into memory. A provider is not more trustworthy
#: than an origin — it is just better paid.
DEFAULT_MAX_BODY_BYTES = 8_000_000

#: We never send Accept-Encoding, so a compressed body is unsolicited. urllib
#: does not decompress on its own, so a bomb cannot expand here — but the
#: compressed bytes still count against the ceiling above, and a body we cannot
#: read as text is reported rather than silently handed to triage as garbage.
UNSOLICITED_ENCODINGS = frozenset({"gzip", "br", "deflate", "zstd"})


class Opener(Protocol):
    """Just enough of urllib's opener for adapters, so tests can substitute one."""

    def urlopen(self, request: Any, timeout: float = ...) -> Any: ...


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes
    latency_ms: int
    #: True when the body hit the ceiling. Triage must not read a truncated
    #: document as a thin page: the bytes we have are a prefix, not the content.
    truncated: bool = False

    @property
    def content_encoding(self) -> str | None:
        value = self.headers.get("content-encoding", "").strip().lower()
        return value or None

    def json(self, *, provider: str) -> Any:
        """Parse the envelope, or say the provider answered unusably.

        A vendor that returns HTML from a JSON endpoint has malfunctioned. That
        is a provider fault, never a statement about the target — reporting it
        as a target problem would quarantine a perfectly good URL.
        """

        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(
                kind=ProviderErrorKind.MALFORMED_RESPONSE,
                message=f"response was not JSON: {exc}",
                provider=provider,
                status=self.status,
            ) from exc


def post_json(
    endpoint: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    provider: str,
    timeout_seconds: float,
    opener: Opener | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> HttpResult:
    """POST a JSON body, mapping only transport-level failures.

    HTTP error statuses are *returned*, not raised: which of them mean "bad
    credentials" versus "the site refused the vendor" is vendor knowledge, and
    guessing it here would put the wrong verdict on a URL.
    """

    request = urllib.request.Request(  # noqa: S310 - каждый вызывающий передаёт свою константу https
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    return _send(
        request,
        provider=provider,
        timeout_seconds=timeout_seconds,
        opener=opener,
        max_body_bytes=max_body_bytes,
    )


def get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    provider: str,
    timeout_seconds: float,
    opener: Opener | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> HttpResult:
    """GET a URL built by the adapter from its own constant endpoint."""

    request = urllib.request.Request(url, headers=headers or {})  # noqa: S310 - см. post_json
    return _send(
        request,
        provider=provider,
        timeout_seconds=timeout_seconds,
        opener=opener,
        max_body_bytes=max_body_bytes,
    )


def _send(
    request: urllib.request.Request,
    *,
    provider: str,
    timeout_seconds: float,
    opener: Opener | None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> HttpResult:
    started = time.monotonic()
    try:
        client: Opener = opener or urllib.request  # type: ignore[assignment]
        with client.urlopen(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            # Read one byte past the ceiling so we can tell "exactly at the
            # limit" from "larger than the limit" without buffering the rest.
            body = response.read(max_body_bytes + 1)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        headers = {str(k).lower(): str(v) for k, v in (exc.headers or {}).items()}
        body = exc.read(max_body_bytes + 1)
    except TimeoutError as exc:
        # We do not know whether the request reached the provider, so callers
        # must treat the spend as unknown rather than refunding it.
        raise ProviderError(
            kind=ProviderErrorKind.TIMEOUT,
            message=str(exc),
            provider=provider,
            retryable=True,
        ) from exc
    except OSError as exc:
        raise ProviderError(
            kind=ProviderErrorKind.TRANSPORT,
            message=str(exc),
            provider=provider,
            retryable=True,
        ) from exc

    truncated = len(body) > max_body_bytes
    if truncated:
        body = body[:max_body_bytes]

    return HttpResult(
        status=status,
        headers=headers,
        body=body,
        latency_ms=int((time.monotonic() - started) * 1000),
        truncated=truncated,
    )
