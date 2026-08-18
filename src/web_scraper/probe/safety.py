"""Target safety checks: SSRF protection for every hop of a fetch."""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler

Resolver = Callable[..., list[tuple]]


class UnsafeTarget(ValueError):
    pass


def _is_disallowed_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


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
    if allow_private:
        return

    try:
        literal = ipaddress.ip_address(parsed.hostname)
        addresses = {literal}
    except ValueError:
        try:
            resolved = resolver(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeTarget(f"hostname resolution failed: {exc}") from exc
        addresses = {ipaddress.ip_address(item[4][0]) for item in resolved}

    blocked = sorted(str(address) for address in addresses if _is_disallowed_address(address))
    if blocked:
        raise UnsafeTarget("target resolves to a non-public address: " + ", ".join(blocked))


class ValidatingRedirectHandler(HTTPRedirectHandler):
    """Re-runs the SSRF check on every redirect hop and records the chain."""

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
        self.chain.append({"from": req.full_url, "to": newurl, "status": code})
        return super().redirect_request(req, fp, code, msg, headers, newurl)
