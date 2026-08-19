"""The artifact a calibration session leaves behind.

Two audiences, one set of numbers. The JSON is what a later session compares
against; the Markdown is what a person reads before deciding to change where
money goes. Both carry the same reproducibility block, because a benchmark whose
conditions cannot be reconstructed is an anecdote with decimal places.

What is recorded is identity and measurement — commit, Python, corpus
fingerprint, tariff versions, sample sizes, outcomes. What is never recorded is
content: no bodies, no headers, no keys. An artifact gets copied into tickets
and chat windows, and everything in it should be safe there.
"""

from __future__ import annotations

import json
import platform
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from web_scraper.calibration.corpus import Corpus
from web_scraper.calibration.harness import AttemptOutcome
from web_scraper.calibration.metrics import (
    aggregate,
    by_fingerprint,
    concentration,
    rank,
    segment_winners,
    totals,
)
from web_scraper.observability.manifest import git_commit
from web_scraper.providers.pricing import PricingBook


@dataclass
class CalibrationReport:
    """Everything one session measured, ready to write out."""

    session: str
    corpus: Corpus
    outcomes: Sequence[AttemptOutcome]
    pricing: PricingBook
    caps: Mapping[str, Any]
    fairness: Mapping[str, Any]
    live: bool
    minimum_confidence: float
    recommendations: Sequence[Mapping[str, Any]] = ()
    started_at: str = ""
    finished_at: str = ""
    notes: tuple[str, ...] = ()
    _repo: Path | None = field(default=None, repr=False)

    # -- assembly ----------------------------------------------------------

    def reproducibility(self) -> dict[str, Any]:
        """What would have to be equal for another run to be comparable."""

        snapshots = self.pricing.to_dict()
        return {
            "git_commit": git_commit(self._repo),
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "corpus_fingerprint": self.corpus.fingerprint,
            "corpus_name": self.corpus.name,
            "workload": "live network" if self.live else "scripted transports (no network)",
            "session": self.session,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pricing_versions": {
                name: {
                    "version": snapshot["version"],
                    "docs_verified_at": snapshot["docs_verified_at"],
                    "age_days": snapshot["age_days"],
                }
                for name, snapshot in snapshots.items()
            },
        }

    def to_dict(self) -> dict[str, Any]:
        per_strategy = aggregate(self.outcomes)
        return {
            "report": "provider calibration",
            "reproducibility": self.reproducibility(),
            "caps": dict(self.caps),
            "fairness": dict(self.fairness),
            "corpus": self.corpus.to_dict(),
            "totals": totals(self.outcomes),
            "strategies": [m.to_dict() for m in _ordered(per_strategy.values())],
            "segment_winners": segment_winners(
                self.outcomes, minimum_confidence=self.minimum_confidence
            ),
            "concentration": concentration(self.outcomes),
            "failure_fingerprints": by_fingerprint(self.outcomes),
            "discovery": self.discovery(),
            "router_recommendations": [dict(r) for r in self.recommendations],
            "notes": list(self.notes),
        }

    def discovery(self) -> dict[str, Any]:
        """What each capture-capable strategy actually found.

        Counted as candidates, never as savings. A PROMISING endpoint is a lead;
        claiming avoided browser calls for it would be inventing a benefit from
        a route nobody has approved.
        """

        found: dict[str, dict[str, Any]] = {}
        for outcome in self.outcomes:
            if not outcome.attempted or outcome.discovery_observed == 0:
                continue
            entry = found.setdefault(
                f"{outcome.provider}:{outcome.strategy}",
                {"pages": 0, "observed": 0, "candidates": 0, "validated": 0, "domains": set()},
            )
            entry["pages"] += 1
            entry["observed"] += outcome.discovery_observed
            entry["candidates"] += outcome.discovery_candidates
            entry["validated"] += outcome.discovery_validated
            entry["domains"].add(outcome.domain)
        return {
            ref: {**entry, "domains": sorted(entry["domains"])}
            for ref, entry in sorted(found.items())
        }

    # -- output ------------------------------------------------------------

    def write(self, directory: str | Path) -> tuple[Path, Path]:
        """Write both files, named by session so nothing is overwritten."""

        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        json_path = out / f"provider-calibration-{self.session}.json"
        json_path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=False), encoding="utf-8"
        )
        md_path = out / f"provider-calibration-{self.session}.md"
        md_path.write_text(self.describe(), encoding="utf-8")
        return json_path, md_path

    def describe(self) -> str:
        payload = self.to_dict()
        repro, total = payload["reproducibility"], payload["totals"]
        lines = [
            "# Provider calibration",
            "",
            f"**Session** `{self.session}` · **{repro['workload']}` · "
            f"commit `{repro['git_commit'] or 'unknown'}`",
            f"**Corpus** `{self.corpus.name}` ({repro['corpus_fingerprint']}) — "
            f"{len(self.corpus.targets)} target(s) across {len(self.corpus.domains)} domain(s)",
            "",
            "## Session totals",
            "",
            f"- planned calls: {total['planned_calls']}",
            f"- attempted: {total['attempted']}  ·  ineligible: {total['ineligible']}"
            f"  ·  early-stopped: {total['skipped_early']}",
            f"- validated results: {total['validated']}",
            f"- spend: exact ${total['exact_usd']}, provisional ${total['provisional_usd']}, "
            f"{total['unknown_cost_calls']} call(s) with unknown cost",
            f"- USD per validated result (session): {total['usd_per_validated_result'] or 'not computable'}",
            f"- status fidelity: {_pct(total['status_fidelity'])}",
            "",
        ]

        if self.corpus.skipped_by_policy:
            lines += ["## Skipped by policy", ""]
            lines += [
                f"- `{d}` — {why}" for d, why in sorted(self.corpus.skipped_by_policy.items())
            ]
            lines.append("")

        lines += ["## Strategies", "", _strategy_table(payload["strategies"]), ""]

        lines += ["## Winner by segment", ""]
        for kind, block in payload["segment_winners"].items():
            lines.append(f"**{kind}** — {block['winner'] or 'no winner'}: {block['reason']}")
        lines.append("")

        conc = payload["concentration"]
        if conc["top_provider"]:
            lines += [
                "## Vendor concentration",
                "",
                f"{_pct(conc['top_provider_share'])} of paid calls went to "
                f"`{conc['top_provider']}`. Reported, not balanced: whether that "
                "concentration is acceptable is the operator's call, not the router's.",
                "",
            ]

        if payload["discovery"]:
            lines += ["## Discovery", ""]
            for ref, entry in payload["discovery"].items():
                lines.append(
                    f"- `{ref}`: {entry['candidates']} candidate(s), "
                    f"{entry['validated']} validated, from {entry['observed']} observed "
                    f"request(s) over {entry['pages']} page(s)"
                )
            lines.append("")

        if self.recommendations:
            lines += ["## What the router would now do", ""]
            for rec in self.recommendations:
                lines.append(f"```\n{rec.get('explanation', '')}\n```")
            lines.append("")

        if self.notes:
            lines += ["## Notes", "", *[f"- {n}" for n in self.notes], ""]
        return "\n".join(lines)


def _ordered(metrics: Iterable[Any]) -> list[Any]:
    return sorted(metrics, key=lambda m: (m.provider, m.strategy, m.domain, m.url_class))


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _strategy_table(rows: Sequence[Mapping[str, Any]]) -> str:
    header = (
        "| strategy | domain/class | validated | Wilson LB | p50 | p95 | "
        "USD/request | USD/validated |\n|---|---|---:|---:|---:|---:|---:|---:|"
    )
    body = []
    for row in rows:
        if not row["attempts"]:
            continue
        body.append(
            f"| `{row['ref']}` | {row['domain']}/{row['url_class']} | "
            f"{row['validated_successes']}/{row['scored_attempts']} | "
            f"{row['confidence_bound']:.3f} | "
            f"{row['p50_ms'] or '-'} | {row['p95_ms'] or '-'} | "
            f"{row['usd_per_request'] or '-'} | "
            f"{row['usd_per_validated_result'] or '**' + (row['cost_unavailable_reason'] or '-') + '**'} |"
        )
    return "\n".join([header, *body]) if body else "_no strategy was called_"


def session_id(now: datetime | None = None) -> str:
    """A sortable name. Time is passed in so tests are not clock-dependent."""

    stamp = now or datetime.now(tz=UTC)
    return stamp.strftime("%Y%m%dT%H%M%SZ")


def ranked_table(
    outcomes: Sequence[AttemptOutcome], *, minimum_confidence: float
) -> list[dict[str, Any]]:
    """The whole fleet on one scale, for callers that want just the ranking."""

    return [
        r.to_dict()
        for r in rank(aggregate(outcomes).values(), minimum_confidence=minimum_confidence)
    ]
