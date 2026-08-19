"""Data availability: never hand out stale data dressed as fresh.

The clean dataset is the last thing that passed validation — which may be today's
run or a promote from three weeks ago that has been failing ever since. A
consumer that cannot tell the difference will quietly make decisions on old data,
and that is silent corruption by another name.

Every row therefore carries a status, an age, and, when it is stale, the verdict
that explains why the fresh attempt did not land.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from web_scraper.contracts import Verdict


class DataStatus(StrEnum):
    """How much a consumer may trust the age of one record."""

    FRESH = "FRESH"  # refreshed within the freshness window
    NOT_MODIFIED = "NOT_MODIFIED"  # re-checked, unchanged at the source
    STALE_LKG = "STALE_LKG"  # last known good, older than the window
    UNAVAILABLE = "UNAVAILABLE"  # no usable record at all


#: Verdicts that mean the record was confirmed current, not merely old.
_CONFIRMED = frozenset({Verdict.OK.value, Verdict.NOT_MODIFIED.value})


@dataclass(frozen=True)
class RecordAvailability:
    natural_key: str
    url: str | None
    status: DataStatus
    age_seconds: float | None
    last_success_at: float | None
    fresh_failure_verdict: str | None
    data: Mapping[str, Any] | None
    #: Which class's freshness window judged this record. Reported so an
    #: operator can see WHY a record was called stale, not just that it was.
    url_class: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.status is not DataStatus.UNAVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "natural_key": self.natural_key,
            "url": self.url,
            "data_status": self.status.value,
            "data_age_seconds": (
                round(self.age_seconds, 1) if self.age_seconds is not None else None
            ),
            "last_success_at": self.last_success_at,
            "url_class": self.url_class,
            "fresh_failure_verdict": self.fresh_failure_verdict,
            "data": dict(self.data) if self.data is not None else None,
        }


@dataclass(frozen=True)
class AvailabilitySLO:
    """Run-level answer to "how much of the dataset can be trusted right now?"."""

    total: int
    fresh: int
    not_modified: int
    stale: int
    unavailable: int
    oldest_age_seconds: float | None

    @property
    def fresh_availability(self) -> float:
        """Share confirmed current. This is the number an SLO should target."""

        return (self.fresh + self.not_modified) / self.total if self.total else 0.0

    @property
    def fresh_plus_lkg_availability(self) -> float:
        """Share with *any* usable record, fresh or stale."""

        return (self.total - self.unavailable) / self.total if self.total else 0.0

    @property
    def stale_ratio(self) -> float:
        return self.stale / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "fresh": self.fresh,
            "not_modified": self.not_modified,
            "stale_lkg": self.stale,
            "unavailable": self.unavailable,
            "fresh_availability": round(self.fresh_availability, 4),
            "fresh_plus_lkg_availability": round(self.fresh_plus_lkg_availability, 4),
            "stale_ratio": round(self.stale_ratio, 4),
            "oldest_age_seconds": (
                round(self.oldest_age_seconds, 1) if self.oldest_age_seconds is not None else None
            ),
        }


def classify_record(
    *,
    updated_at: float | None,
    now: float,
    max_age_seconds: float,
    last_verdict: str | None,
    has_data: bool,
) -> tuple[DataStatus, float | None]:
    """Decide one record's status from its age and the last attempt's verdict."""

    if not has_data or updated_at is None:
        return DataStatus.UNAVAILABLE, None

    age = max(0.0, now - updated_at)
    if age > max_age_seconds:
        # Beyond the window the record is stale regardless of the last verdict:
        # an old success is still old.
        return DataStatus.STALE_LKG, age
    if last_verdict == Verdict.NOT_MODIFIED.value:
        return DataStatus.NOT_MODIFIED, age
    if last_verdict in _CONFIRMED or last_verdict is None:
        return DataStatus.FRESH, age
    # Inside the window but the latest attempt failed: the data we hold is the
    # last good one, and the consumer is told which failure kept it that way.
    return DataStatus.STALE_LKG, age


def build_availability(
    rows: Sequence[Mapping[str, Any]],
    *,
    now: float,
    max_age_seconds: float,
    verdicts_by_key: Mapping[str, str] | None = None,
    max_age_by_url_class: Mapping[str, float] | None = None,
    url_class_by_key: Mapping[str, str] | None = None,
) -> list[RecordAvailability]:
    """Attach a status to every clean-dataset row.

    ``rows`` come from :meth:`DatasetStore.clean_rows_with_meta`; ``verdicts_by_key``
    maps a record's natural key to the verdict of the most recent attempt.

    Each record is judged against **its own url_class's** freshness window when
    ``max_age_by_url_class`` is supplied. A single global window is wrong in both
    directions: a site with hourly news and monthly guides judged at one hour
    reports every guide as stale, and judged at a month reports day-old news as
    current. ``max_age_seconds`` remains the fallback for records whose class is
    unknown, so a missing mapping degrades to the old behaviour rather than to no
    classification at all.
    """

    verdicts = verdicts_by_key or {}
    windows = max_age_by_url_class or {}
    classes = url_class_by_key or {}
    out: list[RecordAvailability] = []
    for row in rows:
        key = str(row.get("natural_key", ""))
        last_verdict = verdicts.get(key)
        payload = row.get("data")
        url_class = classes.get(key) or str(row.get("url_class") or "")
        window = windows.get(url_class, max_age_seconds)
        status, age = classify_record(
            updated_at=row.get("updated_at"),
            now=now,
            max_age_seconds=window,
            last_verdict=last_verdict,
            has_data=payload is not None,
        )
        out.append(
            RecordAvailability(
                natural_key=key,
                url=row.get("url"),
                status=status,
                age_seconds=age,
                last_success_at=row.get("updated_at"),
                fresh_failure_verdict=(
                    last_verdict if status is DataStatus.STALE_LKG and last_verdict else None
                ),
                data=payload,
                url_class=url_class or None,
            )
        )
    return out


def summarize_by_url_class(
    records: Sequence[RecordAvailability],
) -> dict[str, dict[str, Any]]:
    """Availability per class, because one global number hides the failure.

    A dataset that is 95% fresh overall can be 100% fresh on the large, easy
    class and 0% fresh on the small, important one. The global figure would
    still read as healthy.
    """

    grouped: dict[str, list[RecordAvailability]] = {}
    for record in records:
        grouped.setdefault(record.url_class or "unknown", []).append(record)
    return {
        name: summarize_availability(group).to_dict() for name, group in sorted(grouped.items())
    }


def summarize_availability(records: Sequence[RecordAvailability]) -> AvailabilitySLO:
    counts = dict.fromkeys(DataStatus, 0)
    for record in records:
        counts[record.status] += 1
    ages = [r.age_seconds for r in records if r.age_seconds is not None]
    return AvailabilitySLO(
        total=len(records),
        fresh=counts[DataStatus.FRESH],
        not_modified=counts[DataStatus.NOT_MODIFIED],
        stale=counts[DataStatus.STALE_LKG],
        unavailable=counts[DataStatus.UNAVAILABLE],
        oldest_age_seconds=max(ages) if ages else None,
    )
