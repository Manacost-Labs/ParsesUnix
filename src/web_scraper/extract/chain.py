"""The extractor chain and critical-field quorum.

``extract_fields`` walks the profile's extractors in order (most
redesign-resistant first) and takes the first non-null value for each target
field, recording which extractor produced it. ``run_quorum`` cross-checks the
critical fields across independent extractors so a silent mismatch becomes a
visible ``conflict`` instead of a wrong value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from web_scraper.contracts import ContentKind
from web_scraper.extract import dom
from web_scraper.extract.content_kind import detect_content_kind
from web_scraper.extract.json_path import walk_many
from web_scraper.extract.normalize import normalize_value

# schema.org / OpenGraph aliases so a JSON-LD or meta extractor can resolve a
# plain target field name without a per-site mapping.
JSON_LD_ALIASES: Mapping[str, tuple[str, ...]] = {
    "title": ("headline", "name", "title"),
    "name": ("name", "headline"),
    "published_at": ("datePublished", "dateCreated", "uploadDate"),
    "modified_at": ("dateModified",),
    "author": ("author",),
    "description": ("description", "abstract"),
    "price": ("offers.price", "price"),
    "image": ("image",),
}
OG_ALIASES: Mapping[str, tuple[str, ...]] = {
    "title": ("og:title", "twitter:title"),
    "description": ("og:description", "twitter:description", "description"),
    "image": ("og:image", "twitter:image"),
    "published_at": ("article:published_time",),
    "author": ("article:author",),
    "url": ("og:url",),
}

_JSON_LD_RE = re.compile(
    r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([a-zA-Z:_-]+)\s*=\s*[\"']([^\"']*)[\"']")
_NEXT_DATA_RE = re.compile(
    r"<script\b[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
_STATE_RE = re.compile(
    r"(?:window\.)?__(?:INITIAL_STATE|PRELOADED_STATE|NUXT|APOLLO_STATE)__\s*=\s*(\{.*?\})\s*[;<]",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class FieldValue:
    value: Any
    source: str  # json_ld | app_state | meta | css | xpath | heuristic


@dataclass(frozen=True)
class ExtractionResult:
    data: dict[str, Any]
    sources: dict[str, str]
    quorum: dict[str, str] = field(default_factory=dict)  # field -> high|medium|conflict
    conflicts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "data": self.data,
            "sources": self.sources,
            "quorum": self.quorum,
            "conflicts": list(self.conflicts),
        }


def _decode(body: bytes | str) -> str:
    return body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else body


def _walk_json_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _iter_json_ld(text: str) -> list[Any]:
    objects: list[Any] = []
    for match in _JSON_LD_RE.finditer(text):
        try:
            payload = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        objects.extend(payload if isinstance(payload, list) else [payload])
    return objects


def _json_ld_object(text: str, schema_type: str | None) -> dict[str, Any] | None:
    for obj in _iter_json_ld(text):
        if not isinstance(obj, dict):
            continue
        graph = obj.get("@graph")
        candidates = graph if isinstance(graph, list) else [obj]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if schema_type is None:
                return candidate
            declared = candidate.get("@type")
            declared_set = {declared} if isinstance(declared, str) else set(declared or ())
            if schema_type in declared_set:
                return candidate
    return None


def _resolve_alias(
    obj: Mapping[str, Any], field_name: str, aliases: Mapping[str, tuple[str, ...]]
) -> Any:
    for key in aliases.get(field_name, (field_name,)):
        value = _walk_json_path(obj, key) if "." in key else obj.get(key)
        if value is None:
            continue
        if isinstance(value, dict):  # e.g. author: {name: ...}
            value = value.get("name") or value.get("@id") or value.get("url")
        if isinstance(value, list) and value:
            value = value[0]
        if value is not None:
            return value
    return None


def _extract_meta(text: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for tag in _META_RE.findall(text):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        if name and "content" in attrs:
            props.setdefault(name, attrs["content"])
    return props


def _extract_app_state(text: str) -> dict[str, Any] | None:
    match = _NEXT_DATA_RE.search(text)
    if match:
        try:
            parsed: dict[str, Any] = json.loads(match.group(1).strip())
            return parsed
        except json.JSONDecodeError:
            pass
    match = _STATE_RE.search(text)
    if match:
        try:
            state: dict[str, Any] = json.loads(match.group(1))
            return state
        except json.JSONDecodeError:
            pass
    return None


def _one_extractor(
    kind: str,
    spec: Mapping[str, Any],
    *,
    text: str,
    tree: dom.Node,
    app_state: dict[str, Any] | None,
    fields: Sequence[str],
    field_kinds: Mapping[str, str],
    base_url: str | None,
    json_document: Any = None,
) -> dict[str, Any]:
    """Return {field: normalized_value} for the fields this extractor can supply."""

    out: dict[str, Any] = {}
    if kind == "json":
        # A pure JSON response. The document is the data, so there is no DOM to
        # build and no markup to search — the point of treating JSON as a
        # first-class kind rather than as HTML that happens to parse.
        mapping = spec.get("fields") or {}
        document = json_document
        if document is not None:
            for f, path in mapping.items():
                raw = walk_many(document, str(path))
                # A wildcard over rows that no longer carry the key resolves to
                # [None, None, None] — truthy, and completely empty. Recording
                # that as a found value is how a field "keeps working" through
                # the API change that removed it.
                if isinstance(raw, list):
                    raw = [item for item in raw if item is not None]
                if raw is not None and raw != []:
                    out[f] = normalize_value(
                        raw, kind=field_kinds.get(f, "text"), base_url=base_url
                    )
    elif kind == "json_ld":
        obj = _json_ld_object(text, spec.get("schema_type"))
        if obj:
            for f in fields:
                raw = _resolve_alias(obj, f, JSON_LD_ALIASES)
                if raw is not None:
                    out[f] = normalize_value(
                        raw, kind=field_kinds.get(f, "text"), base_url=base_url
                    )
    elif kind == "app_state":
        mapping = spec.get("fields") or {}
        if app_state is not None:
            for f, path in mapping.items():
                raw = _walk_json_path(app_state, str(path))
                if raw is not None:
                    out[f] = normalize_value(
                        raw, kind=field_kinds.get(f, "text"), base_url=base_url
                    )
    elif kind == "meta":
        meta = _extract_meta(text)
        mapping = spec.get("fields")
        for f in fields:
            keys = [mapping[f]] if mapping and f in mapping else list(OG_ALIASES.get(f, ()))
            for key in keys:
                if key.lower() in meta:
                    out[f] = normalize_value(
                        meta[key.lower()], kind=field_kinds.get(f, "text"), base_url=base_url
                    )
                    break
    elif kind in {"css", "xpath"}:
        mapping = spec.get("fields") or {}
        for f, selector in mapping.items():
            raw = dom.query_value(tree, str(selector)) if kind == "css" else None
            if raw is not None:
                out[f] = normalize_value(raw, kind=field_kinds.get(f, "text"), base_url=base_url)
    elif kind == "heuristic":
        title = dom.query_value(tree, "h1::text") or dom.query_value(tree, "title::text")
        if title and "title" in fields:
            out["title"] = normalize_value(
                title, kind=field_kinds.get("title", "text"), base_url=base_url
            )
    return out


def extract_fields(
    body: bytes | str,
    *,
    extractors: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
    field_kinds: Mapping[str, str] | None = None,
    base_url: str | None = None,
    content_kind: ContentKind | None = None,
) -> ExtractionResult:
    """First-non-null-wins across the extractor chain, with provenance.

    When the body is JSON, no DOM is built and no markup is searched. That is
    the practical point of treating JSON as a first-class kind: parsing a large
    API response into an HTML tree in order to find a field that a dotted path
    already names is work with no purpose.
    """

    text = _decode(body)
    field_kinds = field_kinds or {}
    is_json = content_kind is ContentKind.JSON

    json_document: Any = None
    if is_json:
        try:
            # lstrip covers the BOM after decode, plus any leading whitespace.
            json_document = json.loads(text.lstrip("\ufeff \t\r\n"))
        except json.JSONDecodeError:
            # Declared JSON that will not parse. The extractors that need a
            # document produce nothing; a configured html extractor still gets
            # its chance below, because a JSON endpoint answering with an error
            # page is exactly the case worth surviving.
            json_document = None
            is_json = False

    # Built lazily: an HTML tree for a JSON response is pure cost.
    tree = dom.Node("", {}, []) if is_json else dom.parse_html(text)
    app_state = None if is_json else _extract_app_state(text)

    data: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for spec in extractors:
        kind = str(spec.get("kind"))
        produced = _one_extractor(
            kind,
            spec,
            text=text,
            tree=tree,
            app_state=app_state,
            fields=fields,
            field_kinds=field_kinds,
            base_url=base_url,
            json_document=json_document,
        )
        for f, value in produced.items():
            if f not in data and value is not None and value != "":
                data[f] = value
                # "json_path" rather than "json": the drift gate ranks sources,
                # and it needs to see a field falling from a named path to a
                # heuristic as the degradation it is.
                sources[f] = "json_path" if kind == "json" else kind
    return ExtractionResult(data=data, sources=sources)


def extract_response(
    body: bytes,
    *,
    headers: Mapping[str, str] | None = None,
    extractors: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
    field_kinds: Mapping[str, str] | None = None,
    base_url: str | None = None,
) -> tuple[ExtractionResult, ContentKind]:
    """Detect what the response is, then extract accordingly.

    The one entry point a caller holding a raw response should use. It returns
    the detected kind alongside the result so a report can say *why* nothing was
    extracted — "the endpoint answered with a PDF" is an answer; an empty result
    with no explanation is not.
    """

    kind = detect_content_kind(body, headers)
    if kind is ContentKind.BINARY:
        # Never handed to an extractor. Decoding a video to look for a headline
        # is expensive and cannot succeed.
        return ExtractionResult(data={}, sources={}), kind

    return (
        extract_fields(
            body,
            extractors=extractors,
            fields=fields,
            field_kinds=field_kinds,
            base_url=base_url,
            content_kind=kind,
        ),
        kind,
    )


def run_quorum(
    body: bytes | str,
    *,
    extractors: Sequence[Mapping[str, Any]],
    quorum_fields: Sequence[str],
    field_kinds: Mapping[str, str] | None = None,
    base_url: str | None = None,
) -> ExtractionResult:
    """Cross-check critical fields across every extractor that can supply them."""

    text = _decode(body)
    tree = dom.parse_html(text)
    app_state = _extract_app_state(text)
    field_kinds = field_kinds or {}

    # Collect, per field, the value each extractor produced.
    per_field: dict[str, list[tuple[str, Any]]] = {f: [] for f in quorum_fields}
    all_values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for spec in extractors:
        kind = str(spec.get("kind"))
        produced = _one_extractor(
            kind,
            spec,
            text=text,
            tree=tree,
            app_state=app_state,
            fields=quorum_fields,
            field_kinds=field_kinds,
            base_url=base_url,
        )
        for f, value in produced.items():
            if value is not None and value != "":
                per_field.setdefault(f, []).append((kind, value))

    quorum: dict[str, str] = {}
    conflicts: list[str] = []
    for f in quorum_fields:
        observations = per_field.get(f, [])
        distinct = {v for _, v in observations}
        if not observations:
            quorum[f] = "missing"
            continue
        chosen_kind, chosen_value = observations[0]
        all_values[f] = chosen_value
        sources[f] = chosen_kind
        if len(distinct) == 1 and len(observations) >= 2:
            quorum[f] = "high"
        elif len(distinct) == 1:
            quorum[f] = "medium"
        else:
            quorum[f] = "conflict"
            conflicts.append(f)
    return ExtractionResult(
        data=all_values, sources=sources, quorum=quorum, conflicts=tuple(conflicts)
    )
