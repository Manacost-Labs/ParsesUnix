"""Baseline-vs-current comparison for one URL."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

from web_scraper.contracts import ContentRules, Verdict
from web_scraper.extract.chain import extract_fields
from web_scraper.probe import analysis
from web_scraper.triage import classify_response

#: Severity ladder. ``critical`` means data we used to collect is gone.
SEVERITY_NONE = "none"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

_MAX_PATHS = 4000
_MAX_LIST_SCAN = 3
_MAX_DEPTH = 8


@dataclass(frozen=True)
class FieldChange:
    """One extracted field that behaves differently than in the baseline."""

    field: str
    kind: str  # lost | gained | value_changed | source_drift
    before: Any = None
    after: Any = None
    before_source: str | None = None
    after_source: str | None = None

    @property
    def severity(self) -> str:
        return SEVERITY_CRITICAL if self.kind == "lost" else SEVERITY_WARNING

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["severity"] = self.severity
        return payload


@dataclass(frozen=True)
class StructureChange:
    """A structural signal that changed (often the cause behind a lost field)."""

    kind: str
    detail: str
    severity: str = SEVERITY_WARNING
    replacement_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegressionReport:
    url: str
    baseline_verdict: str
    current_verdict: str
    baseline_rendering: str
    current_rendering: str
    field_changes: tuple[FieldChange, ...]
    structure_changes: tuple[StructureChange, ...]
    summary: str

    @property
    def severity(self) -> str:
        levels = (
            [change.severity for change in self.field_changes]
            + [change.severity for change in self.structure_changes]
        )
        if SEVERITY_CRITICAL in levels:
            return SEVERITY_CRITICAL
        if SEVERITY_WARNING in levels:
            return SEVERITY_WARNING
        return SEVERITY_NONE

    @property
    def regressed(self) -> bool:
        return self.severity != SEVERITY_NONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "severity": self.severity,
            "regressed": self.regressed,
            "summary": self.summary,
            "baseline_verdict": self.baseline_verdict,
            "current_verdict": self.current_verdict,
            "baseline_rendering": self.baseline_rendering,
            "current_rendering": self.current_rendering,
            "field_changes": [change.to_dict() for change in self.field_changes],
            "structure_changes": [change.to_dict() for change in self.structure_changes],
        }


def json_paths(value: Any, *, prefix: str = "", depth: int = 0) -> set[str]:
    """Leaf paths of a JSON document, with ``[]`` standing in for list indices."""

    paths: set[str] = set()
    if depth > _MAX_DEPTH or len(paths) > _MAX_PATHS:
        return paths
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(child, (Mapping, list)):
                paths |= json_paths(child, prefix=path, depth=depth + 1)
            else:
                paths.add(path)
    elif isinstance(value, list):
        for item in value[:_MAX_LIST_SCAN]:
            paths |= json_paths(item, prefix=f"{prefix}[]", depth=depth + 1)
    elif prefix:
        paths.add(prefix)
    return paths


def _leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1].replace("[]", "")


def _suggest_replacement(lost_path: str, current_paths: Iterable[str]) -> str | None:
    """A current path whose leaf key matches the lost one is the likely successor."""

    target = _leaf(lost_path)
    candidates = sorted(
        (path for path in current_paths if _leaf(path) == target),
        key=len,
    )
    return candidates[0] if candidates else None


def _parse_json(body: bytes) -> Any | None:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _content_type(headers: Mapping[str, str] | None) -> str:
    return next(
        (str(v) for k, v in (headers or {}).items() if str(k).lower() == "content-type"), ""
    )


def compare_bodies(
    *,
    url: str,
    baseline_body: bytes,
    current_body: bytes,
    baseline_status: int | None = 200,
    current_status: int | None = 200,
    baseline_headers: Mapping[str, str] | None = None,
    current_headers: Mapping[str, str] | None = None,
    rules: ContentRules | None = None,
    extractors: Sequence[Mapping[str, Any]] = (),
    fields: Sequence[str] = (),
) -> RegressionReport:
    """Compare a recorded baseline response against a current one."""

    rules = rules or ContentRules(min_body_bytes=1)
    baseline_triage = classify_response(
        status=baseline_status, body=baseline_body, headers=baseline_headers, rules=rules
    )
    current_triage = classify_response(
        status=current_status, body=current_body, headers=current_headers, rules=rules
    )

    baseline_ct = _content_type(baseline_headers)
    current_ct = _content_type(current_headers)
    baseline_discovery = analysis.discover(baseline_body, url, baseline_ct)
    current_discovery = analysis.discover(current_body, url, current_ct)
    baseline_render = analysis.classify_rendering(
        baseline_body, baseline_ct, baseline_discovery["app_state"]
    )["classification"]
    current_render = analysis.classify_rendering(
        current_body, current_ct, current_discovery["app_state"]
    )["classification"]

    structure: list[StructureChange] = []

    if baseline_triage.verdict is Verdict.OK and current_triage.verdict is not Verdict.OK:
        structure.append(
            StructureChange(
                kind="verdict_regressed",
                detail=(
                    f"baseline was OK, now {current_triage.verdict.value}: {current_triage.reason}"
                ),
                severity=SEVERITY_CRITICAL,
            )
        )

    if baseline_render != current_render:
        # SSR -> CSR is the classic "our HTML route stopped carrying data" break.
        severity = (
            SEVERITY_CRITICAL
            if baseline_render in {"ssr", "hybrid"} and current_render == "csr"
            else SEVERITY_WARNING
        )
        structure.append(
            StructureChange(
                kind="rendering_changed",
                detail=f"rendering went {baseline_render} -> {current_render}",
                severity=severity,
                replacement_hint=(
                    "run browser recon to find the JSON API the page now uses"
                    if current_render == "csr"
                    else None
                ),
            )
        )

    if baseline_discovery["canonical_url"] != current_discovery["canonical_url"]:
        structure.append(
            StructureChange(
                kind="canonical_changed",
                detail=(
                    f"{baseline_discovery['canonical_url']!r} -> "
                    f"{current_discovery['canonical_url']!r}"
                ),
            )
        )

    lost_types = set(baseline_discovery["json_ld_types"]) - set(current_discovery["json_ld_types"])
    for schema_type in sorted(lost_types):
        structure.append(
            StructureChange(
                kind="json_ld_type_lost",
                detail=f"JSON-LD @type {schema_type!r} is no longer published",
            )
        )

    for source, present in baseline_discovery["app_state"].items():
        if present and not current_discovery["app_state"].get(source):
            structure.append(
                StructureChange(
                    kind="app_state_lost",
                    detail=f"embedded app state {source!r} is gone",
                )
            )

    baseline_feeds = {feed["url"] for feed in baseline_discovery["alternates"]["rss_atom"]}
    current_feeds = {feed["url"] for feed in current_discovery["alternates"]["rss_atom"]}
    for feed in sorted(baseline_feeds - current_feeds):
        structure.append(
            StructureChange(kind="feed_lost", detail=f"declared feed {feed} disappeared")
        )

    # JSON routes: a vanished path is the single most actionable signal, and the
    # replacement hint is usually the whole fix (data moved, not disappeared).
    baseline_json = _parse_json(baseline_body)
    current_json = _parse_json(current_body)
    if baseline_json is not None and current_json is not None:
        before_paths = json_paths(baseline_json)
        after_paths = json_paths(current_json)
        for lost in sorted(before_paths - after_paths):
            structure.append(
                StructureChange(
                    kind="json_path_lost",
                    detail=f"JSON path ${lost} is no longer present",
                    severity=SEVERITY_CRITICAL,
                    replacement_hint=(
                        f"${hint}" if (hint := _suggest_replacement(lost, after_paths)) else None
                    ),
                )
            )

    field_changes = _diff_extraction(
        baseline_body=baseline_body,
        current_body=current_body,
        url=url,
        extractors=extractors,
        fields=fields,
    )

    report = RegressionReport(
        url=url,
        baseline_verdict=baseline_triage.verdict.value,
        current_verdict=current_triage.verdict.value,
        baseline_rendering=baseline_render,
        current_rendering=current_render,
        field_changes=tuple(field_changes),
        structure_changes=tuple(structure),
        summary="",
    )
    return RegressionReport(**{**report.__dict__, "summary": _summarize(report)})


def _diff_extraction(
    *,
    baseline_body: bytes,
    current_body: bytes,
    url: str,
    extractors: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
) -> list[FieldChange]:
    if not extractors or not fields:
        return []

    before = extract_fields(baseline_body, extractors=extractors, fields=fields, base_url=url)
    after = extract_fields(current_body, extractors=extractors, fields=fields, base_url=url)

    changes: list[FieldChange] = []
    for field in fields:
        had, has = field in before.data, field in after.data
        if had and not has:
            changes.append(
                FieldChange(
                    field=field,
                    kind="lost",
                    before=before.data.get(field),
                    before_source=before.sources.get(field),
                )
            )
        elif has and not had:
            changes.append(
                FieldChange(
                    field=field, kind="gained", after=after.data.get(field),
                    after_source=after.sources.get(field),
                )
            )
        elif had and has:
            before_source = before.sources.get(field)
            after_source = after.sources.get(field)
            if before_source != after_source:
                # The field survived but is now coming from a less stable source.
                changes.append(
                    FieldChange(
                        field=field, kind="source_drift",
                        before=before.data[field], after=after.data[field],
                        before_source=before_source, after_source=after_source,
                    )
                )
            elif before.data[field] != after.data[field]:
                changes.append(
                    FieldChange(
                        field=field, kind="value_changed",
                        before=before.data[field], after=after.data[field],
                        before_source=before_source, after_source=after_source,
                    )
                )
    return changes


def _summarize(report: RegressionReport) -> str:
    lost = [c.field for c in report.field_changes if c.kind == "lost"]
    drift = [c.field for c in report.field_changes if c.kind == "source_drift"]
    critical = [c for c in report.structure_changes if c.severity == SEVERITY_CRITICAL]

    if not report.field_changes and not report.structure_changes:
        return "no regression detected"

    parts: list[str] = []
    if lost:
        parts.append(f"lost field(s): {', '.join(lost)}")
    if critical:
        parts.append(critical[0].detail)
    if drift:
        parts.append(f"extractor source drift on {', '.join(drift)}")
    if not parts:
        parts.append(f"{len(report.structure_changes)} structural change(s), fields still extracted")
    hint = next(
        (c.replacement_hint for c in report.structure_changes if c.replacement_hint), None
    )
    if hint:
        parts.append(f"possible replacement: {hint}")
    return "; ".join(parts)


def compare_saved_to_current(
    saved: Any,
    *,
    current_body: bytes,
    current_status: int | None = 200,
    current_headers: Mapping[str, str] | None = None,
    extractors: Sequence[Mapping[str, Any]] = (),
    fields: Sequence[str] = (),
) -> RegressionReport:
    """Compare a :class:`~web_scraper.storage.fixtures.SavedResponse` baseline."""

    return compare_bodies(
        url=saved.url,
        baseline_body=saved.body,
        current_body=current_body,
        baseline_status=saved.status,
        current_status=current_status,
        baseline_headers=saved.headers,
        current_headers=current_headers,
        rules=saved.rules,
        extractors=extractors,
        fields=fields,
    )
