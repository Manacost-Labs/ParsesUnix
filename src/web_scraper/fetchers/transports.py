"""Concrete transports for the free gateway levels.

L0/L1 run on the standard library (cookie-aware opener with SSRF-checked
redirects). L2 uses a lazily imported Playwright renderer. Scrapling
adapters are declared as explicit slots: per references/providers-and-stacks.md
their exact API must be confirmed against the live documentation before
being wired in, so until then they refuse loudly instead of guessing.
"""

from __future__ import annotations

import http.cookiejar
import socket
import time
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request

from web_scraper.fetchers.base import RawResponse, TransportUnavailable
from web_scraper.probe.safety import Resolver, build_safe_opener, validate_public_url

DEFAULT_USER_AGENT = "WebScraperSkill/0.2 (+authorized collection)"
DEFAULT_MAX_BODY_BYTES = 2_000_000


def _module_available(name: str) -> bool:
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def scrapling_available() -> bool:
    return _module_available("scrapling")


def playwright_available() -> bool:
    return _module_available("playwright")


class UrllibTransport:
    """Standard-library HTTP session: cookies, SSRF-checked redirects, timing."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        resolver: Resolver = socket.getaddrinfo,
        timeout: float = 20.0,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        user_agent: str = DEFAULT_USER_AGENT,
        use_cookies: bool = True,
    ) -> None:
        self.allow_private = allow_private
        self.resolver = resolver
        self.timeout = timeout
        self.max_body_bytes = max_body_bytes
        self.user_agent = user_agent
        cookie_processor = (
            HTTPCookieProcessor(http.cookiejar.CookieJar()) if use_cookies else None
        )
        self._opener = build_safe_opener(
            allow_private=allow_private, resolver=resolver, cookie_processor=cookie_processor
        )

    def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> RawResponse:
        validate_public_url(url, allow_private=self.allow_private, resolver=self.resolver)
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5",
        }
        request_headers.update(headers or {})
        request = Request(url, headers=request_headers)

        started = time.monotonic()
        status: int | None = None
        final_url = url
        raw_headers: dict[str, str] = {}
        body = b""
        truncated = False
        transport_error: str | None = None
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                final_url = response.geturl()
                validate_public_url(final_url, allow_private=self.allow_private, resolver=self.resolver)
                raw_headers = dict(response.headers.items())
                body = response.read(self.max_body_bytes + 1)
        except HTTPError as exc:
            status = exc.code
            final_url = exc.geturl()
            validate_public_url(final_url, allow_private=self.allow_private, resolver=self.resolver)
            raw_headers = dict(exc.headers.items()) if exc.headers else {}
            body = exc.read(self.max_body_bytes + 1)
        except (URLError, TimeoutError, OSError) as exc:
            transport_error = str(exc)

        if len(body) > self.max_body_bytes:
            truncated = True
            body = body[: self.max_body_bytes]

        return RawResponse(
            requested_url=url,
            final_url=final_url,
            status=status,
            headers=raw_headers,
            body=body,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            truncated=truncated,
            transport_error=transport_error,
        )


class PlaywrightRenderTransport:
    """L2 dynamic rendering via a lazily imported headless Chromium."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        resolver: Resolver = socket.getaddrinfo,
        timeout: float = 30.0,
        headless: bool = True,
    ) -> None:
        self.allow_private = allow_private
        self.resolver = resolver
        self.timeout = timeout
        self.headless = headless

    def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> RawResponse:
        validate_public_url(url, allow_private=self.allow_private, resolver=self.resolver)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise TransportUnavailable(
                "L2 dynamic fetch requires Playwright: "
                "pip install playwright && playwright install chromium"
            ) from exc

        started = time.monotonic()  # pragma: no cover - needs a live browser
        with sync_playwright() as playwright:  # pragma: no cover - needs a live browser
            browser = playwright.chromium.launch(headless=self.headless)
            context = browser.new_context(extra_http_headers=dict(headers or {}))
            page = context.new_page()
            response = page.goto(url, timeout=self.timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=self.timeout * 1000)
            except Exception:
                pass  # render whatever settled before the timeout
            html = page.content()
            status = response.status if response else None
            raw_headers = dict(response.headers) if response else {}
            final_url = page.url
            context.close()
            browser.close()
        return RawResponse(  # pragma: no cover - needs a live browser
            requested_url=url,
            final_url=final_url,
            status=status,
            headers=raw_headers,
            body=html.encode("utf-8"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


_SCRAPLING_NOTE = (
    "the Scrapling adapter is a declared slot, not an implementation: confirm the "
    "current API against https://scrapling.readthedocs.io/ "
    "(see references/providers-and-stacks.md) before wiring it in"
)


class ScraplingSessionTransport:
    """L1 slot for a Scrapling session (preferred once the library is installed)."""

    def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> RawResponse:
        if not scrapling_available():
            raise TransportUnavailable("scrapling is not installed: pip install scrapling")
        raise TransportUnavailable(_SCRAPLING_NOTE)


class ScraplingStealthyTransport:
    """L2 slot for Scrapling's StealthyFetcher (proven browser challenges only)."""

    def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> RawResponse:
        if not scrapling_available():
            raise TransportUnavailable("scrapling is not installed: pip install scrapling")
        raise TransportUnavailable(_SCRAPLING_NOTE)
