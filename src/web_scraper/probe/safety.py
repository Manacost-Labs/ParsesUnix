"""Target safety checks: SSRF protection for every hop of a fetch.

Two layers work together:

* ``validate_public_url`` is a fail-fast pre-check (scheme, credentials, and a
  resolve-then-classify pass) used before a request and on every redirect hop.
* The pinned connection classes returned by ``build_safe_opener`` close the
  DNS-rebinding window: they resolve the hostname, validate **every** returned
  address, and connect to one of the validated addresses — so the address that
  was checked is the address the socket actually reaches. HTTPS keeps the
  original hostname for SNI and certificate validation.

Cross-host redirects additionally have their sensitive request headers stripped
so an ``Authorization``/``Cookie`` supplied for host A never reaches host B.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    build_opener,
)

Resolver = Callable[..., list[tuple]]

#: Request headers that must never cross to a different host on redirect.
SENSITIVE_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
        "x-amz-security-token",
    }
)


class UnsafeTarget(ValueError):
    pass


def _is_disallowed_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Disallow anything not globally reachable.

    ``is_global`` is the only reliable gate: CGNAT (``100.64.0.0/10``) and
    several other internal ranges are not flagged by ``is_private`` yet are not
    globally routable, so they would slip past a flag-by-flag check.
    """

    return not address.is_global


def _resolve_addresses(hostname: str, port: int, resolver: Resolver) -> list[ipaddress._BaseAddress]:
    try:
        resolved = resolver(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeTarget(f"hostname resolution failed: {exc}") from exc
    addresses = []
    for item in resolved:
        try:
            addresses.append(ipaddress.ip_address(item[4][0]))
        except ValueError:
            continue
    if not addresses:
        raise UnsafeTarget(f"hostname {hostname!r} did not resolve to any usable address")
    return addresses


def validate_public_url(
    url: str,
    *,
    allow_private: bool = False,
    resolver: Resolver = socket.getaddrinfo,
) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeTarget("only http and https URLs are allowed")
    if not parsed.hostname:
        raise UnsafeTarget("URL hostname is missing")
    if parsed.username or parsed.password:
        raise UnsafeTarget("credentials in URLs are not allowed")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:  # out-of-range port in the URL
        raise UnsafeTarget(f"invalid port in URL: {exc}") from exc
    if allow_private:
        return

    try:
        addresses: list[ipaddress._BaseAddress] = [ipaddress.ip_address(parsed.hostname)]
    except ValueError:
        addresses = _resolve_addresses(parsed.hostname, port, resolver)

    blocked = sorted(str(address) for address in addresses if _is_disallowed_address(address))
    if blocked:
        raise UnsafeTarget("target resolves to a non-public address: " + ", ".join(blocked))


def pick_safe_address(
    hostname: str,
    port: int,
    *,
    resolver: Resolver = socket.getaddrinfo,
    allow_private: bool = False,
) -> str:
    """Return a validated IP literal to connect to (the DNS-rebinding pin).

    For a hostname, resolves once and validates every address; connecting to the
    returned literal guarantees the socket reaches an address that was checked.
    """

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        if not allow_private and _is_disallowed_address(literal):
            raise UnsafeTarget(f"target address {hostname} is not public")
        return hostname

    addresses = _resolve_addresses(hostname, port, resolver)
    if not allow_private:
        blocked = sorted(str(a) for a in addresses if _is_disallowed_address(a))
        if blocked:
            raise UnsafeTarget("target resolves to a non-public address: " + ", ".join(blocked))
    return str(addresses[0])


def _host_key(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    return (parts.scheme, (parts.hostname or "").lower(), port)


class ValidatingRedirectHandler(HTTPRedirectHandler):
    """Re-runs the SSRF check on every redirect hop and records the chain.

    On a cross-host redirect, sensitive request headers are removed so that
    credentials supplied for the original host do not follow to a new one.
    """

    def __init__(
        self,
        *,
        allow_private: bool,
        resolver: Resolver = socket.getaddrinfo,
        chain: list[dict] | None = None,
    ) -> None:
        super().__init__()
        self.allow_private = allow_private
        self.resolver = resolver
        self.chain: list[dict] = chain if chain is not None else []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        validate_public_url(newurl, allow_private=self.allow_private, resolver=self.resolver)
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is not None and _host_key(req.full_url)[1] != _host_key(newurl)[1]:
            _strip_sensitive_headers(new_request)
        self.chain.append({"from": req.full_url, "to": newurl, "status": code})
        return new_request


def _strip_sensitive_headers(request) -> None:  # type: ignore[no-untyped-def]
    for store in (request.headers, request.unredirected_hdrs):
        for name in list(store):
            if name.lower().replace("_", "-") in SENSITIVE_REQUEST_HEADERS:
                del store[name]


def _make_safe_connection(base: type, *, allow_private: bool, resolver: Resolver) -> type:
    """Build a connection class that pins to a validated address at connect time."""

    class _SafeConnection(base):  # type: ignore[valid-type, misc]
        def connect(self) -> None:  # type: ignore[no-untyped-def]
            if allow_private:
                target = self.host
            else:
                target = pick_safe_address(
                    self.host, self.port, resolver=resolver, allow_private=False
                )
            self.sock = self._create_connection(
                (target, self.port), self.timeout, self.source_address
            )
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if getattr(self, "_tunnel_host", None):
                self._tunnel()
            if isinstance(self, http.client.HTTPSConnection):
                server_hostname = self._tunnel_host or self.host
                self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)

    _SafeConnection.__name__ = f"Safe{base.__name__}"
    return _SafeConnection


class _SafeHTTPHandler(HTTPHandler):
    def __init__(self, *, allow_private: bool, resolver: Resolver, debuglevel: int = 0) -> None:
        super().__init__(debuglevel=debuglevel)
        self._conn = _make_safe_connection(
            http.client.HTTPConnection, allow_private=allow_private, resolver=resolver
        )

    def http_open(self, req):  # type: ignore[no-untyped-def]
        return self.do_open(self._conn, req)


class _SafeHTTPSHandler(HTTPSHandler):
    def __init__(
        self,
        *,
        allow_private: bool,
        resolver: Resolver,
        context: ssl.SSLContext | None = None,
        debuglevel: int = 0,
    ) -> None:
        super().__init__(debuglevel=debuglevel, context=context)
        self._conn = _make_safe_connection(
            http.client.HTTPSConnection, allow_private=allow_private, resolver=resolver
        )

    def https_open(self, req):  # type: ignore[no-untyped-def]
        return self.do_open(self._conn, req, context=self._context)


def build_safe_opener(
    *,
    allow_private: bool = False,
    resolver: Resolver = socket.getaddrinfo,
    cookie_processor: HTTPCookieProcessor | None = None,
    chain: list[dict] | None = None,
) -> OpenerDirector:
    """An opener whose HTTP(S) connections are SSRF-pinned and redirect-checked."""

    handlers: list[object] = [
        _SafeHTTPHandler(allow_private=allow_private, resolver=resolver),
        _SafeHTTPSHandler(allow_private=allow_private, resolver=resolver),
        ValidatingRedirectHandler(allow_private=allow_private, resolver=resolver, chain=chain),
    ]
    if cookie_processor is not None:
        handlers.append(cookie_processor)
    return build_opener(*handlers)
