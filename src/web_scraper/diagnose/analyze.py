"""Group failures, name root causes, and prescribe policy-correct remedies."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

from web_scraper.contracts import PAID_ESCALATION_VERDICTS, Verdict

#: Verdicts that are not failures at all.
_SUCCESS_VERDICTS = frozenset({Verdict.OK.value, Verdict.NOT_MODIFIED.value})

#: (root cause, remedy, may_escalate_to_paid) per verdict.
#:
#: ``may_escalate_to_paid`` is derived from the contract's own
#: ``PAID_ESCALATION_VERDICTS`` rather than restated here, so a policy change in
#: one place cannot silently disagree with the advice given to an operator.
_PLAYBOOK: Mapping[str, tuple[str, str]] = {
    Verdict.ORIGIN_DOWN.value: (
        "the origin server is failing or unreachable (5xx / timeout / DNS)",
        "retry on the next sweep with backoff; the site is down, not blocking us",
    ),
    Verdict.DEAD_URL.value: (
        "the resource is gone (404/410)",
        "keep in quarantine and re-check rarely; never re-fetch in a tight loop",
    ),
    Verdict.RATE_LIMITED.value: (
        "we are asking too fast (429)",
        "honor Retry-After, lower concurrency_per_domain and raise the pacer interval",
    ),
    Verdict.BLOCKED.value: (
        "anti-bot mitigation served a block or challenge",
        "try alternative routes at the same level, then a browser (L2) with a warmed session",
    ),
    Verdict.SOFT_BLOCK.value: (
        "a 2xx response that carries a challenge or substituted content",
        "same as BLOCKED: alternative routes, then L2; verify the canary still matches real content",
    ),
    Verdict.ACCESS_DENIED.value: (
        "the site is refusing on access-control grounds",
        "check whether the data is public at all; do not attempt to bypass access control",
    ),
    Verdict.AUTH_REQUIRED.value: (
        "the resource requires authentication",
        "out of scope for automated collection; obtain an authorized interface instead",
    ),
    Verdict.PARSE_FAIL.value: (
        "the response arrived but the expected content/fields were not found",
        "a profile problem, not a network one: run ws-regress against the baseline fixture",
    ),
    Verdict.CSR_REQUIRED.value: (
        "the page is a client-rendered shell: markup arrived, data is script-loaded",
        "render it (L2) and, better, use browser recon to find the JSON endpoint "
        "the page itself calls, then move the route to L0",
    ),
    Verdict.THIN_CONTENT.value: (
        "a 2xx response too small to be the real page",
        "check the route returns full content (pagination/redirect/consent wall), not a stub",
    ),
    Verdict.PROVIDER_ERROR.value: (
        "the fetching provider or proxy failed, not the target",
        "check provider health and credentials; do not read this as a target verdict",
    ),
}

_UNKNOWN = ("unclassified failure", "inspect the saved snapshot for this group")

_NUM_RE = re.compile(r"\d+")


def _normalize_reason(reason: str | None) -> str:
    """Collapse numbers so 'HTTP 502' and 'HTTP 503' group by shape, not value."""

    if not reason:
        return ""
    return _NUM_RE.sub("N", reason.strip().lower())[:120]


@dataclass(frozen=True)
class FailureGroup:
    verdict: str
    reason: str
    level: str | None
    count: int
    share: float
    root_cause: str
    remedy: str
    may_escalate_to_paid: bool
    sample_urls: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["sample_urls"] = list(self.sample_urls)
        return payload


@dataclass(frozen=True)
class Diagnosis:
    total_attempts: int
    failures: int
    success_rate: float
    groups: tuple[FailureGroup, ...]
    by_domain: Mapping[str, int] = field(default_factory=dict)
    headline: str = ""

    @property
    def paid_escalation_share(self) -> float:
        """Share of failures that policy would even allow to reach a paid level."""

        if not self.failures:
            return 0.0
        eligible = sum(group.count for group in self.groups if group.may_escalate_to_paid)
        return eligible / self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_attempts": self.total_attempts,
            "failures": self.failures,
            "success_rate": round(self.success_rate, 4),
            "paid_escalation_share": round(self.paid_escalation_share, 4),
            "headline": self.headline,
            "groups": [group.to_dict() for group in self.groups],
            "by_domain": dict(self.by_domain),
        }


def _domain(url: str) -> str:
    return urlsplit(url).netloc or "?"


def diagnose_attempts(attempts: Iterable[Mapping[str, Any]], *, sample_urls: int = 3) -> Diagnosis:
    """Group attempt records (``{url, verdict, level, reason}``) into a diagnosis."""

    records = list(attempts)
    total = len(records)
    failures = [r for r in records if str(r.get("verdict")) not in _SUCCESS_VERDICTS]

    buckets: dict[tuple[str, str, str | None], list[Mapping[str, Any]]] = defaultdict(list)
    for record in failures:
        key = (
            str(record.get("verdict")),
            _normalize_reason(record.get("reason")),
            record.get("level"),
        )
        buckets[key].append(record)

    failure_count = len(failures)
    groups: list[FailureGroup] = []
    for (verdict, reason, level), members in buckets.items():
        root_cause, remedy = _PLAYBOOK.get(verdict, _UNKNOWN)
        groups.append(
            FailureGroup(
                verdict=verdict,
                reason=reason,
                level=level,
                count=len(members),
                share=len(members) / failure_count if failure_count else 0.0,
                root_cause=root_cause,
                remedy=remedy,
                may_escalate_to_paid=verdict in {v.value for v in PAID_ESCALATION_VERDICTS},
                sample_urls=tuple(str(m.get("url")) for m in members[:sample_urls]),
            )
        )
    groups.sort(key=lambda group: group.count, reverse=True)

    by_domain = Counter(_domain(str(record.get("url", ""))) for record in failures)
    success_rate = (total - failure_count) / total if total else 0.0
    return Diagnosis(
        total_attempts=total,
        failures=failure_count,
        success_rate=success_rate,
        groups=tuple(groups),
        by_domain=dict(by_domain),
        headline=_headline(groups, success_rate, failure_count),
    )


def _headline(groups: Sequence[FailureGroup], success_rate: float, failures: int) -> str:
    if not failures:
        return f"no failures; success rate {success_rate:.1%}"
    top = groups[0]
    verdict_note = (
        "paid escalation would be permitted for this group"
        if top.may_escalate_to_paid
        else "this group must NOT be escalated to a paid provider"
    )
    return (
        f"success rate {success_rate:.1%}; largest failure group is {top.verdict} "
        f"({top.share:.0%} of failures) — {top.root_cause}; {verdict_note}"
    )


def diagnose_queue(queue: Any, *, limit: int = 5000, sample_urls: int = 3) -> Diagnosis:
    """Diagnose from a :class:`~web_scraper.queue.QueueStore`'s attempt log."""

    return diagnose_attempts(queue.attempts(limit=limit), sample_urls=sample_urls)
