"""The fingerprint itself: what a failure looks like, with the noise removed."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from web_scraper.contracts import Verdict
from web_scraper.triage import ACCESS_DENIED_SIGNATURES, BLOCK_SIGNATURES

#: Response headers that describe the *defense*, not the visitor. Values are
#: normalized to presence or a coarse token — never stored verbatim, so a ray ID
#: or a session cookie can never reach the store.
INDICATIVE_HEADERS = (
    "server",
    "cf-mitigated",
    "x-served-by",
    "x-cache",
    "content-type",
)

#: Vendors whose header values are worth keeping as a coarse token.
_SERVER_TOKENS = ("cloudflare", "akamai", "fastly", "nginx", "apache", "envoy", "cloudfront")

#: Body-size buckets. Exact sizes differ every request; the order of magnitude is
#: what distinguishes a challenge stub from a real page.
_SIZE_BUCKETS = ((0, "empty"), (1_024, "tiny"), (16_384, "small"), (262_144, "medium"))


def body_size_bucket(size: int) -> str:
    """Coarse size class, so a few bytes of jitter do not split a fingerprint."""

    if size <= 0:
        return "empty"
    for threshold, name in _SIZE_BUCKETS[1:]:
        if size < threshold:
            return name
    return "large"


def _server_token(value: str) -> str:
    lowered = value.lower()
    return next((token for token in _SERVER_TOKENS if token in lowered), "other")


def _normalized_headers(headers: Mapping[str, str] | None) -> tuple[str, ...]:
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    out: list[str] = []
    for name in INDICATIVE_HEADERS:
        if name not in lowered:
            continue
        value = lowered[name]
        if name == "server":
            out.append(f"server={_server_token(value)}")
        elif name == "content-type":
            out.append(f"content-type={value.split(';')[0].strip()}")
        elif name == "cf-mitigated":
            out.append(f"cf-mitigated={value.strip().lower()}")
        else:
            # Presence is the signal; the value may carry per-request identifiers.
            out.append(f"{name}=present")
    return tuple(sorted(out))


def _challenge_markers(body: bytes | None) -> tuple[str, ...]:
    """Which known block/challenge phrases appear, as a stable sorted set.

    Only phrases the classifier already recognises are recorded: this is a
    projection of triage's own vocabulary, never free text from the page, so a
    fingerprint cannot leak page content.
    """

    if not body:
        return ()
    sample = body[:200_000].decode("utf-8", errors="ignore").lower()
    found = [marker for marker in BLOCK_SIGNATURES if marker in sample]
    found += [marker for marker in ACCESS_DENIED_SIGNATURES if marker in sample]
    return tuple(sorted(set(found)))


_TRANSPORT_CATEGORIES = (
    ("timed out", "timeout"),
    ("timeout", "timeout"),
    ("name or service not known", "dns"),
    ("nodename nor servname", "dns"),
    ("getaddrinfo", "dns"),
    ("connection refused", "connection_refused"),
    ("connection reset", "connection_reset"),
    ("certificate", "tls"),
    ("ssl", "tls"),
)


def transport_error_category(error: str | None) -> str | None:
    """Bucket a transport error message; the raw text varies per platform."""

    if not error:
        return None
    lowered = error.lower()
    for needle, category in _TRANSPORT_CATEGORIES:
        if needle in lowered:
            return category
    return "other"


def redirect_pattern(chain: tuple[Mapping[str, Any], ...] | None) -> str:
    """Shape of the redirect chain: statuses and whether the host changed."""

    if not chain:
        return "none"
    parts: list[str] = []
    for hop in chain:
        status = hop.get("status")
        same_host = _host(str(hop.get("from", ""))) == _host(str(hop.get("to", "")))
        parts.append(f"{status}{'s' if same_host else 'x'}")
    return "->".join(parts)


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


@dataclass(frozen=True)
class FailureFingerprint:
    """A normalized failure signature. Contains no secrets and no raw page text."""

    verdict: str
    status: int | None
    size_bucket: str
    challenge_markers: tuple[str, ...]
    headers: tuple[str, ...]
    redirect_pattern: str
    transport_error: str | None
    #: Kept for reporting; deliberately NOT part of the digest so the same defense
    #: recognises itself across sites.
    domain: str = ""
    url_class: str = ""

    @property
    def digest(self) -> str:
        """Site-independent identity: the same defense hashes the same anywhere."""

        material = "|".join(
            [
                self.verdict,
                str(self.status),
                self.size_bucket,
                ",".join(self.challenge_markers),
                ",".join(self.headers),
                self.redirect_pattern,
                self.transport_error or "",
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    @property
    def label(self) -> str:
        """A short human-readable name, e.g. ``BLOCKED/403/cloudflare-challenge``."""

        parts = [self.verdict, str(self.status or "-")]
        if any("cf-mitigated" in header for header in self.headers):
            parts.append("cf-challenge")
        elif self.challenge_markers:
            parts.append(re.sub(r"[^a-z0-9]+", "-", self.challenge_markers[0])[:24].strip("-"))
        elif self.transport_error:
            parts.append(self.transport_error)
        return "/".join(parts)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["challenge_markers"] = list(self.challenge_markers)
        payload["headers"] = list(self.headers)
        payload["digest"] = self.digest
        payload["label"] = self.label
        return payload


def fingerprint_attempt(
    *,
    verdict: Verdict,
    status: int | None,
    body: bytes | None,
    headers: Mapping[str, str] | None = None,
    transport_error: str | None = None,
    redirect_chain: tuple[Mapping[str, Any], ...] | None = None,
    domain: str = "",
    url_class: str = "",
) -> FailureFingerprint:
    """Build a fingerprint from one attempt's raw response."""

    return FailureFingerprint(
        verdict=verdict.value,
        status=status,
        size_bucket=body_size_bucket(len(body) if body else 0),
        challenge_markers=_challenge_markers(body),
        headers=_normalized_headers(headers),
        redirect_pattern=redirect_pattern(redirect_chain),
        transport_error=transport_error_category(transport_error),
        domain=domain,
        url_class=url_class,
    )


@dataclass(frozen=True)
class FingerprintRecord:
    """What the store remembers about one fingerprint."""

    digest: str
    label: str
    verdict: str
    first_seen: float
    last_seen: float
    count: int
    routes_seen: tuple[str, ...] = ()
    recovery_routes: Mapping[str, int] = None  # type: ignore[assignment]
    successful_recovery_count: int = 0
    last_recovery: float | None = None
    sample: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.recovery_routes is None:
            object.__setattr__(self, "recovery_routes", {})

    @property
    def best_recovery(self) -> str | None:
        """The route that most often got past this failure, if any."""

        if not self.recovery_routes:
            return None
        return max(self.recovery_routes.items(), key=lambda item: item[1])[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "label": self.label,
            "verdict": self.verdict,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "count": self.count,
            "routes_seen": list(self.routes_seen),
            "recovery_routes": dict(self.recovery_routes),
            "best_recovery": self.best_recovery,
            "successful_recovery_count": self.successful_recovery_count,
            "last_recovery": self.last_recovery,
            "sample": dict(self.sample) if self.sample else None,
        }
