"""Transport abstraction shared by every gateway level."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True)
class RawResponse:
    """One raw HTTP exchange, before triage sees it."""

    requested_url: str
    final_url: str
    status: int | None
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int | None = None
    truncated: bool = False
    transport_error: str | None = None


class TransportUnavailable(RuntimeError):
    """The transport for a route cannot run in this environment."""


class Transport(Protocol):
    def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> RawResponse: ...
