"""Draft a Site Profile from a probe report and browser-recon candidates.

Every draft this module produces passes ``parse_profile`` — the output is
a reviewable starting point, not a production profile.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from web_scraper.profiles.model import parse_profile

#: Placeholder content proof. It will not match any real page, so a draft fails
#: closed (PARSE_FAIL) until a reviewer sets a real canary / required JSON path.
CANARY_PLACEHOLDER = "__SET_CONTENT_CANARY__"

DRAFT_NOTES = (
    "draft generated from a probe report; the content proof is a placeholder that "
    "fails closed — replace canary/required_json_paths, then review the match "
    "pattern, selectors, and routes before production use"
)


def _report_dict(report: Any) -> dict[str, Any]:
    return report.to_dict() if hasattr(report, "to_dict") else dict(report)


def _match_pattern(final_url: str) -> str:
    parts = urlsplit(final_url)
    path = parts.path or "/"
    directory = path[: path.rfind("/") + 1] if "/" in path else "/"
    return "^" + re.escape(f"{parts.scheme}://{parts.netloc}{directory}")


def _clean_route(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": candidate["type"],
        "level": candidate["level"],
        "url": candidate.get("url"),
    }


def draft_profile_from_probe(
    report: Any,
    *,
    url_class: str = "page",
    required_fields: Sequence[str] = ("title",),
) -> dict[str, Any]:
    """Build a validated draft profile dict from a static probe report."""

    data = _report_dict(report)
    final_url = data["final_url"]
    parts = urlsplit(final_url)
    recommendation = data.get("recommendation", {})
    discovery = data.get("discovery", {})
    fetch = data.get("fetch", {})

    candidates = list(recommendation.get("candidate_routes", []))
    start_level = recommendation.get("start_level", "L1")

    primary: dict[str, Any] | None = None
    alternatives: list[dict[str, Any]] = []
    if start_level == "L0":
        l0_candidates = [c for c in candidates if c["level"] == "L0"]
        primary = _clean_route(l0_candidates[0])
        alternatives = [_clean_route(c) for c in l0_candidates[1:]]
        alternatives.append({"type": "direct_http", "level": "L1", "url": None})
    elif start_level == "L2":
        primary = {"type": "dynamic", "level": "L2", "url": None}
        alternatives = [_clean_route(c) for c in candidates]
    else:
        primary = {"type": "direct_http", "level": "L1", "url": None}
        alternatives = [_clean_route(c) for c in candidates]

    content_type = fetch.get("content_type", "")
    expected = "json" if "json" in content_type else "html"

    extractors: list[dict[str, Any]] = []
    json_ld_types = discovery.get("json_ld_types", [])
    if json_ld_types:
        extractors.append({"kind": "json_ld", "schema_type": json_ld_types[0]})
    app_state = discovery.get("app_state", {})
    for source in ("next_data", "initial_state", "nuxt", "apollo"):
        if app_state.get(source):
            extractors.append({"kind": "app_state", "source": source})
            break
    opengraph = discovery.get("opengraph", {})
    meta_fields = {
        field: f"og:{field}"
        for field in required_fields
        if f"og:{field}" in opengraph
    }
    if meta_fields:
        extractors.append({"kind": "meta", "fields": meta_fields})
    extractors.append({"kind": "heuristic"})

    # A draft must ship a triage-checkable content proof, not just required_fields
    # (which the extraction layer enforces, not triage). We fail closed with a
    # placeholder canary the reviewer MUST replace, so the draft never silently
    # accepts an arbitrary >=200-byte body as OK.
    validation: dict[str, Any] = {
        "min_body_bytes": 200,
        "required_fields": list(required_fields),
    }
    if expected == "json":
        validation["required_json_paths"] = [CANARY_PLACEHOLDER]
    else:
        validation["canary"] = CANARY_PLACEHOLDER

    profile = {
        "site": parts.hostname or "",
        "authorization": {"public_data_only": True},
        "notes": DRAFT_NOTES,
        "url_classes": {
            url_class: {
                "match": _match_pattern(final_url),
                "expected_content_type": expected,
                "validation": validation,
                "routes": {
                    "primary": primary,
                    "alternatives": alternatives,
                },
                "extractors": extractors,
                "quorum_fields": list(required_fields),
            }
        },
    }
    parse_profile(profile)  # a draft must never be emitted invalid
    return profile


def merge_api_candidate(
    profile: Mapping[str, Any],
    url_class: str,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Add a browser-recon L0 API candidate to a draft profile.

    If the class currently starts at L2 (a CSR shell), the found API becomes
    the primary route and the browser route moves to the alternatives — this
    is exactly the "one recon, then work through L0" transition.
    """

    route = candidate.get("route", candidate)
    new_route = {"type": "json_api", "level": "L0", "url": route.get("url")}
    if not new_route["url"]:
        raise ValueError("candidate has no URL")

    updated = {key: value for key, value in profile.items()}
    classes = dict(updated.get("url_classes", {}))
    if url_class not in classes:
        raise KeyError(f"url class {url_class!r} is not present in the profile")
    cls = {key: value for key, value in classes[url_class].items()}
    routes = {key: value for key, value in cls.get("routes", {}).items()}
    primary = dict(routes.get("primary", {}))
    alternatives = [dict(item) for item in routes.get("alternatives", [])]

    existing_urls = {r.get("url") for r in [primary, *alternatives]}
    if new_route["url"] in existing_urls:
        return dict(profile)

    if primary.get("level") == "L2":
        alternatives.insert(0, primary)
        routes["primary"] = new_route
    else:
        alternatives.insert(0, new_route)
    routes["alternatives"] = alternatives
    cls["routes"] = routes
    classes[url_class] = cls
    updated["url_classes"] = classes
    parse_profile(updated)
    return updated
