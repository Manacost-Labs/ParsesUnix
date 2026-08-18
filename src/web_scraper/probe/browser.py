"""Optional browser reconnaissance for CSR sites.

Runs only after a static probe, intercepts JSON XHR/fetch responses,
strips cookies and other sensitive headers from every capture, and
emits L0 API route candidates. Paid providers are never involved here.

Playwright is imported lazily: the module works (sanitization, candidate
extraction, skip logic) without it, and raises ``BrowserUnavailable``
with an install hint only when a real browser run is requested.
"""

from __future__ import annotations

import json
import socket
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from web_scraper.probe.safety import Resolver, validate_public_url

BROWSER_RECON_SCHEMA = "web-scraper/browser-recon/v1"

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

MAX_JSON_BYTES = 400_000
MAX_LIST_SCAN = 5
MAX_DEPTH = 6


class BrowserUnavailable(RuntimeError):
    pass


def sanitize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Drop cookies, authorization, and other sensitive headers (case-insensitive)."""

    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() not in SENSITIVE_HEADERS
    }


def _normalize(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "")


def find_field_paths(
    value: Any,
    target_fields: Sequence[str],
    *,
    _prefix: str = "",
    _depth: int = 0,
) -> dict[str, list[str]]:
    """Locate target field names (case/style-insensitive) inside parsed JSON."""

    found: dict[str, list[str]] = {}
    if _depth > MAX_DEPTH:
        return found
    normalized_targets = {_normalize(fieldname): fieldname for fieldname in target_fields}

    def merge(hits: dict[str, list[str]]) -> None:
        for key, paths in hits.items():
            found.setdefault(key, []).extend(paths)

    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{_prefix}.{key}" if _prefix else str(key)
            target = normalized_targets.get(_normalize(str(key)))
            if target is not None:
                found.setdefault(target, []).append(path)
            merge(find_field_paths(child, target_fields, _prefix=path, _depth=_depth + 1))
    elif isinstance(value, list):
        for item in value[:MAX_LIST_SCAN]:
            merge(find_field_paths(item, target_fields, _prefix=f"{_prefix}[]", _depth=_depth + 1))
    return found


def _same_site(page_url: str, response_url: str) -> bool:
    page_host = (urlsplit(page_url).hostname or "").lower()
    response_host = (urlsplit(response_url).hostname or "").lower()
    if not page_host or not response_host:
        return False
    page_root = ".".join(page_host.rsplit(".", 2)[-2:])
    response_root = ".".join(response_host.rsplit(".", 2)[-2:])
    return page_root == response_root


@dataclass(frozen=True)
class CapturedResponse:
    """One intercepted JSON response with secrets already removed."""

    url: str
    method: str
    status: int
    content_type: str
    same_site: bool
    request_headers: dict[str, str]
    body_bytes: int
    json_body: Any = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("json_body", None)
        return payload


@dataclass(frozen=True)
class ApiCandidate:
    url: str
    method: str
    status: int
    same_site: bool
    matched_fields: dict[str, list[str]]
    body_bytes: int

    @property
    def route(self) -> dict[str, Any]:
        return {"type": "json_api", "level": "L0", "url": self.url, "source": "browser-recon"}

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["route"] = self.route
        return payload


def extract_candidates(
    captured: Sequence[CapturedResponse],
    target_fields: Sequence[str],
) -> list[ApiCandidate]:
    """Rank captured JSON responses by how many target fields they contain."""

    candidates: list[ApiCandidate] = []
    for capture in captured:
        if capture.json_body is None:
            continue
        matched = find_field_paths(capture.json_body, target_fields)
        if not matched:
            continue
        candidates.append(
            ApiCandidate(
                url=capture.url,
                method=capture.method,
                status=capture.status,
                same_site=capture.same_site,
                matched_fields={key: paths[:5] for key, paths in matched.items()},
                body_bytes=capture.body_bytes,
            )
        )
    candidates.sort(key=lambda c: (-len(c.matched_fields), not c.same_site, c.url))
    return candidates


@dataclass(frozen=True)
class BrowserReconReport:
    schema: str
    requested_url: str
    executed: bool
    skip_reason: str | None
    captured_count: int
    candidates: tuple[dict, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidates"] = list(self.candidates)
        payload["notes"] = list(self.notes)
        return payload


def should_run_browser(static_report: Any) -> tuple[bool, str]:
    """Decide from a static probe report whether a browser pass is justified."""

    report = static_report.to_dict() if hasattr(static_report, "to_dict") else dict(static_report)
    rendering = report.get("rendering", {}).get("classification", "unknown")
    recommendation = report.get("recommendation", {})
    if recommendation.get("needs_browser_recon"):
        return True, "static probe classified the page as CSR without an API candidate"
    if rendering == "csr":
        return True, "static probe classified the page as CSR"
    return False, f"static probe classified rendering as {rendering!r}; browser pass not justified"


_RECON_NOTES = (
    "cookies, authorization, and other sensitive headers were removed from all captures",
    "paid providers are never used during reconnaissance",
)


def _capture_with_playwright(
    url: str,
    *,
    timeout_s: float,
    max_captures: int,
    max_json_bytes: int,
    headless: bool,
) -> list[CapturedResponse]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise BrowserUnavailable(
            "browser reconnaissance requires Playwright: "
            "pip install playwright && playwright install chromium"
        ) from exc

    captured: list[CapturedResponse] = []

    def on_response(response) -> None:  # pragma: no cover - needs a live browser
        if len(captured) >= max_captures:
            return
        content_type = (response.headers or {}).get("content-type", "")
        if "json" not in content_type.lower():
            return
        try:
            body = response.body()
        except Exception:
            return
        if not body or len(body) > max_json_bytes:
            return
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        captured.append(
            CapturedResponse(
                url=response.url,
                method=response.request.method,
                status=response.status,
                content_type=content_type,
                same_site=_same_site(url, response.url),
                request_headers=sanitize_headers(response.request.headers or {}),
                body_bytes=len(body),
                json_body=parsed,
            )
        )

    with sync_playwright() as playwright:  # pragma: no cover - needs a live browser
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()  # fresh context: no cookies, no stored state
        page = context.new_page()
        page.on("response", on_response)
        page.goto(url, timeout=timeout_s * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_s * 1000)
        except Exception:
            pass  # capture whatever arrived before the timeout
        context.close()
        browser.close()
    return captured


def browser_recon(
    url: str,
    *,
    target_fields: Sequence[str],
    static_report: Any = None,
    force: bool = False,
    allow_private: bool = False,
    resolver: Resolver = socket.getaddrinfo,
    timeout_s: float = 30.0,
    max_captures: int = 40,
    max_json_bytes: int = MAX_JSON_BYTES,
    headless: bool = True,
) -> BrowserReconReport:
    """Run one browser pass over a CSR page and report L0 API candidates."""

    validate_public_url(url, allow_private=allow_private, resolver=resolver)
    if not target_fields:
        raise ValueError("target_fields must name at least one field to look for")

    if not force:
        if static_report is None:
            raise ValueError(
                "browser recon runs only after a static probe; pass static_report or force=True"
            )
        run, reason = should_run_browser(static_report)
        if not run:
            return BrowserReconReport(
                schema=BROWSER_RECON_SCHEMA,
                requested_url=url,
                executed=False,
                skip_reason=reason,
                captured_count=0,
                candidates=(),
                notes=_RECON_NOTES,
            )

    captured = _capture_with_playwright(
        url,
        timeout_s=timeout_s,
        max_captures=max_captures,
        max_json_bytes=max_json_bytes,
        headless=headless,
    )
    candidates = extract_candidates(captured, target_fields)
    return BrowserReconReport(
        schema=BROWSER_RECON_SCHEMA,
        requested_url=url,
        executed=True,
        skip_reason=None,
        captured_count=len(captured),
        candidates=tuple(candidate.to_dict() for candidate in candidates),
        notes=_RECON_NOTES,
    )
