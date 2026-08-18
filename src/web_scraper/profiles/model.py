"""Site Profile model and validator.

A profile is rejected here, before any network activity, if it is
structurally invalid, references impossible route/level combinations,
or contains anything that looks like a secret, cookie, or token.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from web_scraper.contracts import ContentRules, Level, Route, RouteType

EXTRACTOR_KINDS = ("json_ld", "app_state", "meta", "css", "xpath", "heuristic")
APP_STATE_SOURCES = ("next_data", "initial_state", "nuxt", "apollo")

SECRET_KEY_NAMES = frozenset(
    {
        "cookie",
        "cookies",
        "set-cookie",
        "authorization",
        "auth_token",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "x-api-key",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "bearer",
        "session_id",
        "sessionid",
        "proxy_password",
        "private_key",
    }
)

SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\."),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

_ALWAYS_ALLOWED_KEYS = frozenset({"notes"})

DEFAULT_SESSION: Mapping[str, Any] = {"warmup": False, "ttl_minutes": 30}
DEFAULT_PAGINATION: Mapping[str, Any] = {"expected_count_selector": None, "max_depth": 200}
DEFAULT_RETRY: Mapping[str, Any] = {"max_attempts": 2, "backoff_seconds": 5}
DEFAULT_LIMITS: Mapping[str, Any] = {
    "concurrency_per_domain": 2,
    "paid_attempts_per_url": 0,
    "daily_paid_credits": 0,
}
DEFAULT_FRESHNESS: Mapping[str, Any] = {"max_age_hours": 24, "full_review_days": 7}
DEFAULT_PROMOTE: Mapping[str, Any] = {"min_completeness": 0.95, "max_null_rate_growth": 2.0}


class ProfileError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("invalid site profile:\n" + "\n".join(f"  - {err}" for err in self.errors))


class _Ctx:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def err(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")


def _is_number(value: Any) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def scan_for_secrets(value: Any, path: str, ctx: _Ctx) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_str = str(key)
            child_path = f"{path}.{key_str}" if path else key_str
            is_secret_key = (
                key_str.lower().replace("-", "_") in SECRET_KEY_NAMES
                or key_str.lower() in SECRET_KEY_NAMES
            )
            # The top-level ``authorization`` policy section is legitimate; the
            # schema restricts its content to ``public_data_only``. Any other
            # secret-named key is forbidden regardless of its value.
            if is_secret_key and not (
                key_str.lower() == "authorization" and isinstance(child, Mapping)
            ):
                ctx.err(child_path, "secrets, cookies, and tokens are forbidden in profiles")
            scan_for_secrets(child, child_path, ctx)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            scan_for_secrets(item, f"{path}[{index}]", ctx)
    elif isinstance(value, str):
        for pattern in SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                ctx.err(path, "value matches a secret/token pattern and is forbidden in profiles")
                break


def _check_unknown(
    data: Mapping[str, Any], allowed: frozenset[str] | set[str], path: str, ctx: _Ctx
) -> None:
    for key in data:
        key_str = str(key)
        if key_str in allowed or key_str in _ALWAYS_ALLOWED_KEYS or key_str.startswith("x-"):
            continue
        ctx.err(f"{path}.{key_str}" if path else key_str, "unknown key (typo protection)")


def _string_list(value: Any, path: str, ctx: _Ctx) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    ctx.err(path, "must be a string or a list of strings")
    return ()


def _section(
    data: Mapping[str, Any],
    key: str,
    defaults: Mapping[str, Any],
    path: str,
    ctx: _Ctx,
) -> dict[str, Any]:
    raw = data.get(key)
    if raw is None:
        return dict(defaults)
    if not isinstance(raw, Mapping):
        ctx.err(f"{path}.{key}", "must be a mapping")
        return dict(defaults)
    _check_unknown(raw, set(defaults), f"{path}.{key}", ctx)
    merged = dict(defaults)
    merged.update({str(k): v for k, v in raw.items() if str(k) in defaults})
    return merged


def _registrable_domain(host: str) -> str:
    return ".".join(host.lower().rsplit(".", 2)[-2:])


def _validate_route_url(url: Any, site: str, path: str, ctx: _Ctx) -> None:
    if url is None:
        return  # a route without a URL fetches the target itself
    if not isinstance(url, str) or not url:
        ctx.err(f"{path}.url", "must be a non-empty string")
        return
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        ctx.err(f"{path}.url", "must be an absolute http(s) URL, not a relative path")
        return
    if not parts.hostname:
        ctx.err(f"{path}.url", "is missing a hostname")
        return
    if site and _registrable_domain(parts.hostname) != _registrable_domain(site):
        ctx.err(
            f"{path}.url",
            f"host {parts.hostname!r} does not belong to site {site!r}; "
            "routes must not point at a third-party host",
        )


def _parse_route(raw: Any, path: str, ctx: _Ctx, *, site: str = "") -> Route | None:
    if not isinstance(raw, Mapping):
        ctx.err(path, "route must be a mapping")
        return None
    _check_unknown(raw, {"type", "level", "url", "mode", "provider", "id"}, path, ctx)
    try:
        route_type = RouteType(str(raw.get("type")))
    except ValueError:
        ctx.err(f"{path}.type", f"unknown route type {raw.get('type')!r}")
        return None
    try:
        level = Level(str(raw.get("level")))
    except ValueError:
        ctx.err(f"{path}.level", f"unknown level {raw.get('level')!r}; expected L0..L4")
        return None
    _validate_route_url(raw.get("url"), site, path, ctx)
    declared_id = raw.get("id")
    if declared_id is not None and (not isinstance(declared_id, str) or not declared_id.strip()):
        ctx.err(f"{path}.id", "must be a non-empty string")
        declared_id = None
    try:
        return Route(
            type=route_type,
            level=level,
            url=raw.get("url"),
            mode=raw.get("mode", "single"),
            provider=raw.get("provider"),
            id=declared_id,
        )
    except ValueError as exc:
        ctx.err(path, str(exc))
        return None


def _parse_extractor(raw: Any, path: str, ctx: _Ctx) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        ctx.err(path, "extractor must be a mapping")
        return None
    kind = raw.get("kind")
    if kind not in EXTRACTOR_KINDS:
        ctx.err(
            f"{path}.kind", f"unknown extractor kind {kind!r}; expected one of {EXTRACTOR_KINDS}"
        )
        return None
    if kind == "json_ld":
        _check_unknown(raw, {"kind", "schema_type"}, path, ctx)
    elif kind == "app_state":
        _check_unknown(raw, {"kind", "source", "fields"}, path, ctx)
        source = raw.get("source")
        if source is not None and source not in APP_STATE_SOURCES:
            ctx.err(f"{path}.source", f"unknown app-state source {source!r}")
    elif kind in {"css", "xpath", "meta"}:
        _check_unknown(raw, {"kind", "fields"}, path, ctx)
        fields = raw.get("fields")
        if not isinstance(fields, Mapping) or not fields:
            ctx.err(f"{path}.fields", f"{kind} extractor requires a non-empty fields mapping")
            return None
        if not all(isinstance(v, str) and v for v in fields.values()):
            ctx.err(f"{path}.fields", "every field selector must be a non-empty string")
    else:  # heuristic
        _check_unknown(raw, {"kind"}, path, ctx)
    return {str(key): value for key, value in raw.items()}


def _positive_int(value: Any, minimum: int, path: str, ctx: _Ctx) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        ctx.err(path, f"must be an integer >= {minimum}")


def _non_negative_number(value: Any, path: str, ctx: _Ctx) -> None:
    if not _is_number(value) or value < 0:
        ctx.err(path, "must be a non-negative number")


@dataclass(frozen=True)
class UrlClass:
    name: str
    match_pattern: str
    expected_content_type: str | None
    min_body_bytes: int
    canaries: tuple[str, ...]
    stop_signatures: tuple[str, ...]
    required_fields: tuple[str, ...]
    required_json_paths: tuple[str, ...]
    primary_route: Route
    alternative_routes: tuple[Route, ...]
    extractors: tuple[Mapping[str, Any], ...]
    quorum_fields: tuple[str, ...]
    session: Mapping[str, Any]
    pagination: Mapping[str, Any]
    retry: Mapping[str, Any]
    limits: Mapping[str, Any]
    freshness: Mapping[str, Any]
    promote: Mapping[str, Any]

    @property
    def match(self) -> re.Pattern[str]:
        return re.compile(self.match_pattern)

    def matches(self, url: str) -> bool:
        return bool(self.match.search(url))

    def content_rules(self) -> ContentRules:
        return ContentRules(
            min_body_bytes=self.min_body_bytes,
            canaries=self.canaries,
            expected_content_type=self.expected_content_type,
            required_json_paths=self.required_json_paths,
            stop_signatures=self.stop_signatures,
        )

    @property
    def routes(self) -> tuple[Route, ...]:
        return (self.primary_route, *self.alternative_routes)


@dataclass(frozen=True)
class SiteProfile:
    site: str
    url_classes: Mapping[str, UrlClass]

    def class_for_url(self, url: str) -> UrlClass | None:
        return next((cls for cls in self.url_classes.values() if cls.matches(url)), None)


def _parse_url_class(name: str, raw: Any, ctx: _Ctx, *, site: str = "") -> UrlClass | None:
    path = f"url_classes.{name}"
    if not isinstance(raw, Mapping):
        ctx.err(path, "must be a mapping")
        return None
    _check_unknown(
        raw,
        {
            "match",
            "expected_content_type",
            "validation",
            "routes",
            "extractors",
            "quorum_fields",
            "session",
            "pagination",
            "retry",
            "limits",
            "freshness",
            "promote",
        },
        path,
        ctx,
    )

    match_pattern = raw.get("match")
    if not isinstance(match_pattern, str) or not match_pattern:
        ctx.err(f"{path}.match", "is required and must be a regex string")
        match_pattern = "^$"
    else:
        if not match_pattern.startswith("^"):
            ctx.err(f"{path}.match", "must be anchored with '^' to avoid matching foreign URLs")
        try:
            re.compile(match_pattern)
        except re.error as exc:
            ctx.err(f"{path}.match", f"invalid regex: {exc}")

    expected_content_type = raw.get("expected_content_type")
    if expected_content_type is not None and not isinstance(expected_content_type, str):
        ctx.err(f"{path}.expected_content_type", "must be a string")
        expected_content_type = None

    validation = raw.get("validation")
    min_body_bytes = 200
    canaries: tuple[str, ...] = ()
    stop_signatures: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    required_json_paths: tuple[str, ...] = ()
    if not isinstance(validation, Mapping):
        ctx.err(f"{path}.validation", "is required (canaries, required fields, size limits)")
    else:
        _check_unknown(
            validation,
            {
                "min_body_bytes",
                "canary",
                "canaries",
                "stop_signatures",
                "required_fields",
                "required_json_paths",
            },
            f"{path}.validation",
            ctx,
        )
        raw_min = validation.get("min_body_bytes", 200)
        if not isinstance(raw_min, int) or isinstance(raw_min, bool) or raw_min < 1:
            ctx.err(f"{path}.validation.min_body_bytes", "must be an integer >= 1")
        else:
            min_body_bytes = raw_min
        canaries = _string_list(
            validation.get("canary"), f"{path}.validation.canary", ctx
        ) + _string_list(validation.get("canaries"), f"{path}.validation.canaries", ctx)
        stop_signatures = _string_list(
            validation.get("stop_signatures"), f"{path}.validation.stop_signatures", ctx
        )
        required_fields = _string_list(
            validation.get("required_fields"), f"{path}.validation.required_fields", ctx
        )
        required_json_paths = _string_list(
            validation.get("required_json_paths"), f"{path}.validation.required_json_paths", ctx
        )
        # required_fields is enforced by the extraction layer, not by triage, so
        # it does NOT count as a triage-checkable content proof. Without a canary
        # or a required JSON path, triage would accept any >= min_body_bytes body.
        if not (canaries or required_json_paths):
            ctx.err(
                f"{path}.validation",
                "needs a triage-checkable content proof: canary/canaries or "
                "required_json_paths (required_fields alone is not enough — it is "
                "checked by extraction, not triage)",
            )

    routes_raw = raw.get("routes")
    primary_route: Route | None = None
    alternative_routes: list[Route] = []
    if not isinstance(routes_raw, Mapping):
        ctx.err(f"{path}.routes", "is required with a primary route")
    else:
        _check_unknown(routes_raw, {"primary", "alternatives"}, f"{path}.routes", ctx)
        primary_route = _parse_route(
            routes_raw.get("primary"), f"{path}.routes.primary", ctx, site=site
        )
        alternatives_raw = routes_raw.get("alternatives") or []
        if not isinstance(alternatives_raw, list):
            ctx.err(f"{path}.routes.alternatives", "must be a list of routes")
        else:
            for index, item in enumerate(alternatives_raw):
                route = _parse_route(item, f"{path}.routes.alternatives[{index}]", ctx, site=site)
                if route is not None:
                    alternative_routes.append(route)

    extractors_raw = raw.get("extractors")
    extractors: list[Mapping[str, Any]] = []
    if not isinstance(extractors_raw, list) or not extractors_raw:
        ctx.err(f"{path}.extractors", "is required and must be a non-empty list")
    else:
        for index, item in enumerate(extractors_raw):
            extractor = _parse_extractor(item, f"{path}.extractors[{index}]", ctx)
            if extractor is not None:
                extractors.append(extractor)

    quorum_fields = _string_list(raw.get("quorum_fields"), f"{path}.quorum_fields", ctx)
    if quorum_fields and required_fields and not set(quorum_fields) <= set(required_fields):
        extra = sorted(set(quorum_fields) - set(required_fields))
        ctx.err(
            f"{path}.quorum_fields",
            f"must be a subset of validation.required_fields; extra: {extra}",
        )

    session = _section(raw, "session", DEFAULT_SESSION, path, ctx)
    if not isinstance(session.get("warmup"), bool):
        ctx.err(f"{path}.session.warmup", "must be a boolean")
    _positive_int(session.get("ttl_minutes"), 0, f"{path}.session.ttl_minutes", ctx)

    pagination = _section(raw, "pagination", DEFAULT_PAGINATION, path, ctx)
    _positive_int(pagination.get("max_depth"), 1, f"{path}.pagination.max_depth", ctx)
    selector = pagination.get("expected_count_selector")
    if selector is not None and not isinstance(selector, str):
        ctx.err(f"{path}.pagination.expected_count_selector", "must be a string or null")

    retry = _section(raw, "retry", DEFAULT_RETRY, path, ctx)
    _positive_int(retry.get("max_attempts"), 1, f"{path}.retry.max_attempts", ctx)
    _non_negative_number(retry.get("backoff_seconds"), f"{path}.retry.backoff_seconds", ctx)

    limits = _section(raw, "limits", DEFAULT_LIMITS, path, ctx)
    _positive_int(
        limits.get("concurrency_per_domain"), 1, f"{path}.limits.concurrency_per_domain", ctx
    )
    _positive_int(
        limits.get("paid_attempts_per_url"), 0, f"{path}.limits.paid_attempts_per_url", ctx
    )
    _non_negative_number(limits.get("daily_paid_credits"), f"{path}.limits.daily_paid_credits", ctx)

    freshness = _section(raw, "freshness", DEFAULT_FRESHNESS, path, ctx)
    for field_name in ("max_age_hours", "full_review_days"):
        value = freshness.get(field_name)
        if not _is_number(value) or value <= 0:
            ctx.err(f"{path}.freshness.{field_name}", "must be a positive number")

    promote = _section(raw, "promote", DEFAULT_PROMOTE, path, ctx)
    completeness = promote.get("min_completeness")
    if not _is_number(completeness) or not (0 <= completeness <= 1):
        ctx.err(f"{path}.promote.min_completeness", "must be a number between 0 and 1")
    _non_negative_number(
        promote.get("max_null_rate_growth"), f"{path}.promote.max_null_rate_growth", ctx
    )

    if primary_route is None:
        return None

    # Two routes sharing an identity would share statistics, and the router would
    # read one half-broken route where there are two distinct ones.
    seen_ids: dict[str, str] = {}
    for index, candidate in enumerate([primary_route, *alternative_routes]):
        where = (
            f"{path}.routes.primary" if index == 0 else f"{path}.routes.alternatives[{index - 1}]"
        )
        previous = seen_ids.get(candidate.route_id)
        if previous is not None:
            ctx.err(
                where,
                f"route identity {candidate.route_id!r} is already used by {previous}; "
                "give one of them an explicit unique 'id'",
            )
        seen_ids[candidate.route_id] = where
    return UrlClass(
        name=name,
        match_pattern=str(match_pattern),
        expected_content_type=expected_content_type,
        min_body_bytes=min_body_bytes,
        canaries=canaries,
        stop_signatures=stop_signatures,
        required_fields=required_fields,
        required_json_paths=required_json_paths,
        primary_route=primary_route,
        alternative_routes=tuple(alternative_routes),
        extractors=tuple(extractors),
        quorum_fields=quorum_fields,
        session=session,
        pagination=pagination,
        retry=retry,
        limits=limits,
        freshness=freshness,
        promote=promote,
    )


def parse_profile(data: Any) -> SiteProfile:
    ctx = _Ctx()
    if not isinstance(data, Mapping):
        raise ProfileError(["profile root must be a mapping"])

    scan_for_secrets(data, "", ctx)
    _check_unknown(data, {"site", "authorization", "url_classes"}, "", ctx)

    site = data.get("site")
    if not isinstance(site, str) or not site.strip():
        ctx.err("site", "is required and must be a hostname string")
        site = ""
    elif "://" in site or "/" in site:
        ctx.err("site", "must be a bare hostname, not a URL")

    authorization = data.get("authorization")
    if not isinstance(authorization, Mapping) or authorization.get("public_data_only") is not True:
        ctx.err("authorization.public_data_only", "profiles must explicitly declare 'true'")
    elif isinstance(authorization, Mapping):
        _check_unknown(authorization, {"public_data_only"}, "authorization", ctx)

    classes_raw = data.get("url_classes")
    url_classes: dict[str, UrlClass] = {}
    if not isinstance(classes_raw, Mapping) or not classes_raw:
        ctx.err("url_classes", "is required and must contain at least one URL class")
    else:
        for name, raw in classes_raw.items():
            parsed = _parse_url_class(str(name), raw, ctx, site=str(site))
            if parsed is not None:
                url_classes[str(name)] = parsed
        # Only meaningful once every pattern compiled cleanly.
        if not ctx.errors and len(url_classes) > 1:
            _check_class_overlap(url_classes, ctx)

    if ctx.errors:
        raise ProfileError(ctx.errors)
    return SiteProfile(site=str(site), url_classes=url_classes)


#: Sample paths used to detect ambiguous (overlapping) url-class patterns.
_OVERLAP_PROBES = (
    "/",
    "/index.html",
    "/articles/sample-story",
    "/api/v1/items/1",
    "/catalog/page/2",
    "/feed",
    "/2026/08/18/news",
    "/products/42",
)


_PATTERN_HOST_RE = re.compile(r"\^?(https?)://([^/(\[]+)")


def _pattern_host(pattern: str) -> str | None:
    match = _PATTERN_HOST_RE.match(pattern)
    if not match:
        return None
    return f"{match.group(1)}://{match.group(2).replace(chr(92), '')}"


def _check_class_overlap(url_classes: Mapping[str, UrlClass], ctx: _Ctx) -> None:
    """Flag patterns that both match a probe path: match order would be ambiguous."""

    site_host = next(
        (host for cls in url_classes.values() if (host := _pattern_host(cls.match_pattern))), ""
    )
    for probe in _OVERLAP_PROBES:
        candidate = f"{site_host}{probe}" if site_host else probe
        try:
            matched = [name for name, cls in url_classes.items() if cls.matches(candidate)]
        except re.error:
            return  # a bad regex is already reported elsewhere
        if len(matched) > 1:
            ctx.err(
                "url_classes",
                f"patterns for {sorted(matched)} both match {candidate!r}; "
                "make the match regexes mutually exclusive (class order is not a tie-breaker)",
            )
            return


def _load_yaml(text: str) -> Any:
    try:
        import yaml
    except ImportError:
        from web_scraper.profiles import yamlish

        return yamlish.loads(text)
    return yaml.safe_load(text)


def load_profile(path: str | Path) -> SiteProfile:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    data = json.loads(text) if file_path.suffix.lower() == ".json" else _load_yaml(text)
    return parse_profile(data)
