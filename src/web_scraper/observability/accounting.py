"""URL accounting: the ledger that makes a silent drop impossible.

The absolute invariant of a run is that **every input URL is accounted for**.
Not every URL can be fetched — a deleted page, an origin outage, or a login wall
are legitimate outcomes — but each one must land in exactly one named bucket. A
URL that simply disappears from the report is a defect in the system, not a
property of the internet.

``UrlAccounting`` reconciles the queue (which holds one durable row per input
URL) against the buckets. ``unaccounted`` is the difference, and a run whose
``unaccounted`` is non-zero is defective by definition.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Queue statuses that represent a settled outcome for the run.
TERMINAL_STATUSES = ("DONE", "FAILED", "QUARANTINED", "DEAD_ZONE")

#: Queue statuses that mean the run ended before this URL was settled. These are
#: accounted for (they are carried to the next run), never silently dropped.
CARRIED_STATUSES = ("PENDING", "RETRY")

#: A URL left here when the run ended was claimed but never resolved — the one
#: state that indicates a genuine loss rather than a deliberate outcome.
LOST_STATUS = "IN_PROGRESS"


@dataclass(frozen=True)
class UrlAccounting:
    """Every input URL in exactly one bucket, with the remainder made explicit."""

    input_urls: int
    settled: Mapping[str, int]
    carried: Mapping[str, int]
    lost: int
    missing_from_queue: tuple[str, ...] = ()

    @property
    def accounted(self) -> int:
        return sum(self.settled.values()) + sum(self.carried.values()) + self.lost

    @property
    def unaccounted(self) -> int:
        """URLs the ledger cannot place. Must be zero; anything else is a defect."""

        return self.input_urls - self.accounted + len(self.missing_from_queue)

    @property
    def is_complete(self) -> bool:
        return self.unaccounted == 0 and self.lost == 0 and not self.missing_from_queue

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_urls": self.input_urls,
            "settled": dict(self.settled),
            "carried_to_next_run": dict(self.carried),
            "lost_in_progress": self.lost,
            "missing_from_queue": list(self.missing_from_queue),
            "accounted": self.accounted,
            "unaccounted": self.unaccounted,
            "complete": self.is_complete,
        }


def build_accounting(
    status_counts: Mapping[str, int],
    *,
    seeded_urls: Mapping[str, bool] | None = None,
) -> UrlAccounting:
    """Reconcile a queue status census into an accounting ledger.

    ``seeded_urls`` maps each URL the caller handed in to whether it reached the
    queue; anything False is reported as ``missing_from_queue`` so a URL that was
    never even enqueued cannot vanish between the caller and the ledger.
    """

    settled = {name: status_counts.get(name, 0) for name in TERMINAL_STATUSES}
    carried = {name: status_counts.get(name, 0) for name in CARRIED_STATUSES}
    lost = status_counts.get(LOST_STATUS, 0)

    known = set(TERMINAL_STATUSES) | set(CARRIED_STATUSES) | {LOST_STATUS}
    # An unknown status must not silently inflate "accounted": count it as its own
    # settled bucket so the total still reconciles and the name is visible.
    for name, count in status_counts.items():
        if name not in known:
            settled[name] = count

    missing = tuple(sorted(url for url, present in (seeded_urls or {}).items() if not present))
    return UrlAccounting(
        input_urls=sum(status_counts.values()),
        settled=settled,
        carried=carried,
        lost=lost,
        missing_from_queue=missing,
    )
