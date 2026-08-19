"""Schema drift gate: what changed between the last healthy dataset and this one.

The failure this exists to catch is not a crash. It is a run that completes,
passes every per-row validation, and publishes a dataset in which a field
silently became empty because the site renamed a CSS class. Nothing errors. The
row count is right. The data is wrong, and the consumer finds out days later.

So the comparison is against the **last known good** dataset, and it looks at
shape rather than values:

* records vanishing in bulk;
* a field that used to be populated becoming absent or always-null;
* a field whose *type* changed — a string where an object used to be;
* null rates growing on fields the profile marks critical;
* pagination that stopped short of what the listing claimed;
* **extraction provenance degrading** — a field that used to come from JSON-LD
  now arriving from a heuristic. The value may still look plausible, which is
  precisely why this is worth an alert: heuristics are the layer that produces
  confident nonsense.

Two things are deliberately *not* blocking. A brand-new field is reported and
allowed, because sites add things and refusing to publish would be a
self-inflicted outage. And a first run with no baseline passes, because there is
nothing to drift from — inventing a comparison there would be theatre.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: Fraction of the baseline record count below which the dataset is suspect.
DEFAULT_MIN_RECORD_RATIO = 0.7

#: Null-rate growth on a critical field that stops a promotion.
DEFAULT_MAX_NULL_GROWTH = 2.0

#: Extraction sources, best first. A field moving down this list has degraded
#: even when its value still looks reasonable.
PROVENANCE_RANK = {
    "json_ld": 0,
    "app_state": 1,
    "microdata": 2,
    "meta": 3,
    "css": 4,
    "xpath": 4,
    "heuristic": 5,
}


class DriftVerdict(StrEnum):
    PASS = "PASS"  # noqa: S105 - a gate verdict, not a credential
    WARN = "WARN"
    BLOCK_PROMOTION = "BLOCK_PROMOTION"

    @property
    def allows_promotion(self) -> bool:
        return self is not DriftVerdict.BLOCK_PROMOTION


@dataclass(frozen=True)
class DriftFinding:
    kind: str
    detail: str
    severity: str
    observed: str
    baseline: str | None = None
    field_name: str | None = None

    @property
    def blocks(self) -> bool:
        return self.severity == "block"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "field": self.field_name,
            "detail": self.detail,
            "severity": self.severity,
            "observed": self.observed,
            "baseline": self.baseline,
        }


@dataclass(frozen=True)
class SchemaSnapshot:
    """The shape of a dataset, without its contents.

    Values are deliberately excluded: this is compared across runs and stored
    alongside them, and dataset values can carry personal data.
    """

    record_count: int
    #: field -> the set of JSON type names seen for it.
    types: dict[str, frozenset[str]] = field(default_factory=dict)
    #: field -> fraction of rows where it is missing, None or "".
    null_rates: dict[str, float] = field(default_factory=dict)
    #: field -> which extractor produced it, and how often.
    provenance: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        provenance_key: str = "_extractor_source",
    ) -> SchemaSnapshot:
        count = len(rows)
        types: dict[str, set[str]] = {}
        missing: dict[str, int] = {}
        provenance: dict[str, dict[str, int]] = {}

        for row in rows:
            sources = row.get(provenance_key) or {}
            for name, value in row.items():
                if name.startswith("_"):
                    continue
                types.setdefault(name, set()).add(_type_name(value))
                if value in (None, ""):
                    missing[name] = missing.get(name, 0) + 1
                if isinstance(sources, Mapping):
                    source = sources.get(name)
                    if source:
                        bucket = provenance.setdefault(name, {})
                        bucket[str(source)] = bucket.get(str(source), 0) + 1

        return cls(
            record_count=count,
            types={k: frozenset(v) for k, v in types.items()},
            null_rates={k: (missing.get(k, 0) / count if count else 1.0) for k in types},
            provenance=provenance,
        )

    @property
    def fields(self) -> frozenset[str]:
        return frozenset(self.types)

    def dominant_source(self, name: str) -> str | None:
        counts = self.provenance.get(name)
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: kv[1])[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_count": self.record_count,
            "types": {k: sorted(v) for k, v in sorted(self.types.items())},
            "null_rates": {k: round(v, 4) for k, v in sorted(self.null_rates.items())},
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class DriftReport:
    verdict: DriftVerdict
    findings: tuple[DriftFinding, ...] = ()
    baseline: SchemaSnapshot | None = None
    current: SchemaSnapshot | None = None

    @property
    def blocking(self) -> tuple[DriftFinding, ...]:
        return tuple(f for f in self.findings if f.blocks)

    def explain(self) -> str:
        lines = [f"schema drift: {self.verdict.value}"]
        if self.baseline is None:
            lines.append("no healthy baseline to compare against; nothing to drift from")
            return "\n".join(lines)
        assert self.current is not None
        lines.append(
            f"records {self.current.record_count} vs baseline {self.baseline.record_count}"
        )
        if not self.findings:
            lines.append("no differences worth reporting")
        for finding in self.findings:
            mark = "BLOCK" if finding.blocks else finding.severity.upper()
            where = f" [{finding.field_name}]" if finding.field_name else ""
            lines.append(
                f"  {mark}{where} {finding.detail} (now {finding.observed}"
                + (f", was {finding.baseline}" if finding.baseline else "")
                + ")"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "allows_promotion": self.verdict.allows_promotion,
            "findings": [f.to_dict() for f in self.findings],
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "current": self.current.to_dict() if self.current else None,
            "explanation": self.explain(),
        }


def check_drift(
    current: SchemaSnapshot,
    baseline: SchemaSnapshot | None,
    *,
    critical_fields: Sequence[str] = (),
    min_record_ratio: float = DEFAULT_MIN_RECORD_RATIO,
    max_null_growth: float = DEFAULT_MAX_NULL_GROWTH,
    pagination_complete: bool | None = None,
) -> DriftReport:
    """Compare a staged dataset against the last healthy one."""

    if baseline is None:
        # A first run has nothing to drift from. Manufacturing a comparison
        # here would produce a gate that always passes and looks like it checked.
        return DriftReport(DriftVerdict.PASS, (), None, current)

    findings: list[DriftFinding] = []
    critical = set(critical_fields)

    # 1. Bulk record loss.
    if baseline.record_count > 0:
        ratio = current.record_count / baseline.record_count
        if ratio < min_record_ratio:
            findings.append(
                DriftFinding(
                    kind="record_count_collapse",
                    detail="the dataset lost a large share of its records",
                    severity="block",
                    observed=f"{current.record_count} ({ratio:.0%} of baseline)",
                    baseline=str(baseline.record_count),
                )
            )

    # 2. Fields that vanished, and fields that appeared.
    for name in sorted(baseline.fields - current.fields):
        findings.append(
            DriftFinding(
                kind="field_disappeared",
                detail="a field present in the last healthy dataset is gone",
                severity="block" if name in critical else "warn",
                observed="absent",
                baseline="present",
                field_name=name,
            )
        )
    for name in sorted(current.fields - baseline.fields):
        findings.append(
            DriftFinding(
                kind="field_added",
                detail="a new field appeared; reported, not blocked",
                severity="info",
                observed="present",
                baseline="absent",
                field_name=name,
            )
        )

    # 3. Type changes on shared fields.
    for name in sorted(baseline.fields & current.fields):
        before, after = baseline.types[name], current.types[name]
        meaningful_before = before - {"null"}
        meaningful_after = after - {"null"}
        if meaningful_before and meaningful_after and meaningful_before != meaningful_after:
            findings.append(
                DriftFinding(
                    kind="type_changed",
                    detail="the field's type changed shape",
                    severity="block" if name in critical else "warn",
                    observed=",".join(sorted(meaningful_after)),
                    baseline=",".join(sorted(meaningful_before)),
                    field_name=name,
                )
            )

    # 4. Null-rate growth, weighted by whether the field matters.
    for name in sorted(baseline.fields & current.fields):
        base_rate = baseline.null_rates.get(name, 0.0)
        rate = current.null_rates.get(name, 0.0)
        grew = (base_rate > 0 and rate > base_rate * max_null_growth) or (
            base_rate == 0 and rate > 0
        )
        if grew:
            findings.append(
                DriftFinding(
                    kind="null_rate_growth",
                    detail="a field is empty far more often than it used to be",
                    severity="block" if name in critical else "warn",
                    observed=f"{rate:.1%}",
                    baseline=f"{base_rate:.1%}",
                    field_name=name,
                )
            )

    # 5. Provenance degradation — the quiet one.
    for name in sorted(baseline.fields & current.fields):
        before_source = baseline.dominant_source(name)
        after_source = current.dominant_source(name)
        if not before_source or not after_source or before_source == after_source:
            continue
        before_rank = PROVENANCE_RANK.get(before_source, 99)
        after_rank = PROVENANCE_RANK.get(after_source, 99)
        if after_rank > before_rank:
            findings.append(
                DriftFinding(
                    kind="provenance_degraded",
                    detail=(
                        "this field is now coming from a weaker extractor; the values "
                        "may still look plausible, which is why it is worth checking"
                    ),
                    severity="block" if name in critical else "warn",
                    observed=after_source,
                    baseline=before_source,
                    field_name=name,
                )
            )

    # 6. Pagination that stopped short.
    if pagination_complete is False:
        findings.append(
            DriftFinding(
                kind="pagination_incomplete",
                detail="traversal ended on our own ceiling, not at the end of the listing",
                severity="block",
                observed="incomplete",
            )
        )

    if any(f.blocks for f in findings):
        verdict = DriftVerdict.BLOCK_PROMOTION
    elif any(f.severity == "warn" for f in findings):
        verdict = DriftVerdict.WARN
    else:
        verdict = DriftVerdict.PASS
    return DriftReport(verdict, tuple(findings), baseline, current)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__
