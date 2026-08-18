"""Phase A sweep: cheap liveness check that quarantines dead URLs early.

For the free core this is an optimization (the main pass already quarantines
404/410). It becomes a prerequisite before paid providers, which bill for dead
URLs, so the sweep must run before any L3/L4 adapter is enabled.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

DEAD_STATUSES = frozenset({404, 410})

HeadFn = Callable[[str], int | None]


@dataclass(frozen=True)
class SweepResult:
    checked: int
    quarantined: list[str]
    inconclusive: int  # HEAD not honored / network error — left for the main pass


def sweep_dead_urls(
    urls: Iterable[str], *, head: HeadFn, quarantine: Callable[[str, int], None]
) -> SweepResult:
    checked = inconclusive = 0
    quarantined: list[str] = []
    for url in urls:
        checked += 1
        status = head(url)
        if status in DEAD_STATUSES:
            quarantine(url, status)
            quarantined.append(url)
        elif status is None or status == 405:  # HEAD unsupported or transient
            inconclusive += 1
    return SweepResult(checked=checked, quarantined=quarantined, inconclusive=inconclusive)
