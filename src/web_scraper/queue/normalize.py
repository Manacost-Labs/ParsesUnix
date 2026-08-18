"""URL normalization for stable dedup keys.

Two URLs that address the same resource must collapse to one queue row.
Normalization is deliberately conservative — it must never merge distinct
resources — so it only touches parts that are semantically irrelevant:
scheme/host case, default ports, dot segments, fragments, and (optionally)
tracking query parameters.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": "80", "https": "443"}

#: Query parameters dropped as pure tracking noise (never resource-selecting).
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "ref",
        "ref_src",
    }
)


def normalize_url(url: str, *, drop_tracking: bool = True) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()

    netloc = host
    if parts.port is not None and str(parts.port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    path = _remove_dot_segments(parts.path) or "/"

    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if drop_tracking:
        pairs = [(k, v) for k, v in pairs if k.lower() not in TRACKING_PARAMS]
    pairs.sort()  # order-independent identity
    query = urlencode(pairs)

    return urlunsplit((scheme, netloc, path, query, ""))  # fragment dropped


def _remove_dot_segments(path: str) -> str:
    segments = path.split("/")
    output: list[str] = []
    for segment in segments:
        if segment == ".":
            continue
        if segment == "..":
            if output and output[-1] not in ("", ".."):
                output.pop()
            continue
        output.append(segment)
    return "/".join(output)
