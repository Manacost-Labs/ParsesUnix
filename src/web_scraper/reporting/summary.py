"""Aggregate per-URL results into a run summary."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from web_scraper.contracts import PAID_ESCALATION_VERDICTS, Result, Verdict


def summarize(results: Iterable[Result | Mapping[str, Any]]) -> dict[str, Any]:
    """Count verdicts and list every unresolved URL — no silent gaps."""

    total = 0
    by_verdict: dict[str, int] = {}
    unresolved: list[str] = []
    paid_candidates: list[str] = []
    for item in results:
        result = item if isinstance(item, Result) else Result.from_dict(item)
        total += 1
        by_verdict[result.verdict.value] = by_verdict.get(result.verdict.value, 0) + 1
        if not result.resolved:
            unresolved.append(result.url)
        if result.verdict in PAID_ESCALATION_VERDICTS:
            paid_candidates.append(result.url)
    return {
        "total": total,
        "resolved": by_verdict.get(Verdict.OK.value, 0),
        "by_verdict": by_verdict,
        "unresolved_urls": unresolved,
        "paid_escalation_candidates": paid_candidates,
    }
