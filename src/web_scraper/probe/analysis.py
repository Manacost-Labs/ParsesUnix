"""Network-free analysis of a fetched body: discovery, rendering, recommendation.

Every function here is pure over (body, headers, final_url) so the whole
layer is testable against saved fixtures without touching the network.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_LIST_ITEMS = 20

_JSON_LD_RE = re.compile(
    r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([a-zA-Z:_-]+)\s*=\s*[\"']([^\"']*)[\"']")
_SCRIPT_TAG_RE = re.compile(r"<script\b", re.IGNORECASE)
_EMPTY_SHELL_RE = re.compile(
    r"<(?:div|main|section)\b[^>]*\bid=[\"'](?:root|app|__next|__nuxt)[\"'][^>]*>\s*"
    r"</(?:div|main|section)>",
    re.IGNORECASE,
)
_API_HINT_RE = re.compile(
    r"(?:https?:)?//[^\"'\s<>]+/(?:api|graphql)(?:/[^\"'\s<>]*)?|/(?:api|graphql)/[^\"'\s<>]+",
    re.IGNORECASE,
)


def _tag_attrs(tag: str) -> dict[str, str]:
    return {key.lower(): value for key, value in _ATTR_RE.findall(tag)}


def _decode(body: bytes) -> str:
    return body.decode("utf-8", errors="ignore")


def extract_json_ld_types(text: str) -> list[str]:
    types: list[str] = []
    for match in _JSON_LD_RE.finditer(text):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict):
                declared = item.get("@type")
                if isinstance(declared, str):
                    types.append(declared)
                elif isinstance(declared, list):
                    types.extend(str(value) for value in declared)
    return types[:MAX_LIST_ITEMS]


def extract_opengraph(text: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for tag in _META_TAG_RE.findall(text):
        attrs = _tag_attrs(tag)
        name = attrs.get("property") or attrs.get("name") or ""
        if name.lower().startswith(("og:", "twitter:")) and "content" in attrs:
            properties.setdefault(name.lower(), attrs["content"])
        if len(properties) >= MAX_LIST_ITEMS:
            break
    return properties


def extract_canonical(text: str, base_url: str) -> str | None:
    for tag in _LINK_TAG_RE.findall(text):
        attrs = _tag_attrs(tag)
        rels = attrs.get("rel", "").lower().split()
        if "canonical" in rels and attrs.get("href"):
            return urljoin(base_url, attrs["href"])
    return None


def extract_alternates(text: str, base_url: str) -> dict[str, Any]:
    rss_atom: list[dict[str, str]] = []
    amp_url: str | None = None
    for tag in _LINK_TAG_RE.findall(text):
        attrs = _tag_attrs(tag)
        rels = attrs.get("rel", "").lower().split()
        href = attrs.get("href")
        if not href:
            continue
        media_type = attrs.get("type", "").lower()
        if "alternate" in rels and media_type in {"application/rss+xml", "application/atom+xml"}:
            rss_atom.append({"url": urljoin(base_url, href), "type": media_type})
        if "amphtml" in rels and amp_url is None:
            amp_url = urljoin(base_url, href)
    return {"rss_atom": rss_atom[:MAX_LIST_ITEMS], "amp_url": amp_url}


def extract_app_state(text: str) -> dict[str, bool]:
    lower = text.lower()
    return {
        "next_data": "__next_data__" in lower,
        "initial_state": "__initial_state__" in lower or "window.__preloaded_state__" in lower,
        "nuxt": "__nuxt__" in lower or "window.__nuxt" in lower,
        "apollo": "__apollo_state__" in lower,
    }


def _registrable_domain(host: str) -> str:
    return ".".join(host.lower().rsplit(".", 2)[-2:])


def extract_api_hints(text: str, base_url: str) -> dict[str, Any]:
    """Absolute, same-site, query-stripped API/GraphQL endpoint hints.

    Raw matches are joined against the page URL, so relative hints become
    absolute. Third-party hosts and non-http schemes are dropped (a hint is
    only useful as a route if it belongs to the site), and query strings are
    removed so a secret like ``?api_key=...`` never survives into a profile.
    """

    base_host = urlsplit(base_url).hostname or ""
    base_domain = _registrable_domain(base_host) if base_host else ""
    urls: list[str] = []
    seen: set[str] = set()
    for raw in _API_HINT_RE.findall(text):
        absolute = urljoin(base_url, raw)
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            continue
        if base_domain and _registrable_domain(parts.hostname) != base_domain:
            continue  # third-party host: not a route for this site
        clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if clean not in seen:
            seen.add(clean)
            urls.append(clean)
    urls.sort()
    return {"urls": urls[:MAX_LIST_ITEMS], "graphql": any("graphql" in u.lower() for u in urls)}


def visible_text(text: str) -> str:
    stripped = re.sub(
        r"<(script|style|noscript|template)\b.*?</\1\s*>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    stripped = re.sub(r"<!--.*?-->", " ", stripped, flags=re.DOTALL)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def discover(body: bytes, final_url: str, content_type: str) -> dict[str, Any]:
    text = _decode(body)
    return {
        "canonical_url": extract_canonical(text, final_url),
        "json_ld_types": extract_json_ld_types(text),
        "opengraph": extract_opengraph(text),
        "app_state": extract_app_state(text),
        "alternates": extract_alternates(text, final_url),
        "api_hints": extract_api_hints(text, final_url),
    }


def classify_rendering(body: bytes, content_type: str, app_state: Mapping[str, bool]) -> dict[str, Any]:
    if "html" not in content_type.lower():
        return {
            "classification": "non_html",
            "visible_text_chars": 0,
            "script_tags": 0,
            "empty_shell": False,
            "evidence": [f"content type is {content_type or 'unknown'!r}, not HTML"],
        }

    text = _decode(body)
    visible = visible_text(text)
    script_tags = len(_SCRIPT_TAG_RE.findall(text))
    empty_shell = bool(_EMPTY_SHELL_RE.search(text))
    has_state = any(app_state.values())
    evidence: list[str] = []

    if empty_shell and len(visible) < 300:
        classification = "csr"
        evidence.append("empty application root element with almost no visible text")
    elif has_state and len(visible) >= 400:
        classification = "hybrid"
        evidence.append("embedded application state alongside server-rendered content")
    elif len(visible) >= 400:
        classification = "ssr"
        evidence.append(f"{len(visible)} chars of visible text without an empty shell")
    elif script_tags >= 2 and len(visible) < 200:
        classification = "csr"
        evidence.append("scripts dominate a page with almost no visible text")
    else:
        classification = "unknown"
        evidence.append("no strong rendering signal")

    return {
        "classification": classification,
        "visible_text_chars": len(visible),
        "script_tags": script_tags,
        "empty_shell": empty_shell,
        "evidence": evidence,
    }


def recommend(
    discovery: Mapping[str, Any],
    rendering: Mapping[str, Any],
    robots: Mapping[str, Any],
    triage_verdict: str,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for feed in discovery["alternates"]["rss_atom"]:
        candidates.append(
            {"type": "rss", "level": "L0", "url": feed["url"], "source": "link-alternate", "verified": False}
        )
    for sitemap_url in robots.get("sitemaps", []):
        candidates.append(
            {"type": "sitemap", "level": "L0", "url": sitemap_url, "source": "robots", "verified": False}
        )
    for api_url in discovery["api_hints"]["urls"][:5]:
        candidates.append(
            {"type": "json_api", "level": "L0", "url": api_url, "source": "static-hint", "verified": False}
        )
    if discovery["alternates"]["amp_url"]:
        candidates.append(
            {
                "type": "direct_http",
                "level": "L1",
                "url": discovery["alternates"]["amp_url"],
                "source": "amphtml",
                "verified": False,
            }
        )

    classification = rendering["classification"]
    reasons: list[str] = []
    if any(candidate["level"] == "L0" for candidate in candidates):
        start_level = "L0"
        reasons.append("a machine-readable route (feed, sitemap, or API hint) was discovered")
    elif classification in {"ssr", "hybrid", "non_html"}:
        start_level = "L1"
        reasons.append("content is server-rendered; direct HTTP should be enough")
    elif classification == "csr":
        start_level = "L2"
        reasons.append("CSR shell without static data; run browser recon to look for an L0 API first")
    else:
        start_level = "L1"
        reasons.append("no strong signal; start with direct HTTP and re-triage")

    if triage_verdict in {"BLOCKED", "SOFT_BLOCK"}:
        reasons.append(
            "the plain probe fetch hit an anti-bot response; retry the same level with a "
            "warmed session or stealth before considering any paid escalation"
        )

    needs_browser_recon = classification == "csr" and not any(
        candidate["type"] == "json_api" for candidate in candidates
    )
    return {
        "start_level": start_level,
        "needs_browser_recon": needs_browser_recon,
        "reasons": reasons,
        "candidate_routes": candidates,
    }
