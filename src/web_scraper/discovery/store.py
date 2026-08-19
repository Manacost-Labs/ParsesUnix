"""Evidence about discovered endpoints that outlives the run that found it.

Within one run the collector already refuses to validate an endpoint seen on a
single page. But a scheduled crawl is many runs, and evidence that dies with the
process means the threshold is approached and then forgotten, over and over: the
system learns the same thing nightly and never gets to act on it.

This store keeps the *evidence*, never the data. Four rules make that concrete
and they are the reason the schema looks the way it does:

**Shape, not content.** A schema signature says a field is a string, never which
string. No response body, no field value, no query secret, no header is stored.
The store is read by operators, printed into reports and copied into profile
drafts, and anything kept here reaches all three.

**Diversity, not repetition.** Ten renders of one page are one piece of evidence.
Source pages are recorded as hashes in their own table, so the count that matters
is how many *distinct* pages produced the endpoint.

**Evidence expires.** A schema that changes retires the verdict rather than
carrying yesterday's confidence forward, and observations lose weight with age
using the same half-life idea the provider statistics use. An endpoint validated
in March is not validated today because it once was.

**Bounded.** Retention, pruning and per-domain caps, because a store that grows
without limit is a store that eventually stops a run.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from web_scraper.discovery.candidates import (
    CandidateVerdict,
    PaginationHint,
    RouteCandidate,
    SchemaSignature,
)

#: Distinct source pages an endpoint must appear on before it is trusted.
#: One page is a coincidence; the same endpoint answering the same shape for
#: several unrelated pages is a pattern.
DEFAULT_MIN_DISTINCT_PAGES = 3

#: Source page hashes kept per candidate. Enough to prove diversity, few enough
#: that a crawl of a million pages does not store a million rows per endpoint.
MAX_PAGE_HASHES = 64

#: Candidates kept per domain. A chatty application must not be able to fill the
#: store on its own.
MAX_CANDIDATES_PER_DOMAIN = 100

#: Evidence older than this is pruned entirely. A route nobody has seen in three
#: months is not evidence, it is history.
DEFAULT_MAX_AGE_DAYS = 90

#: Evidence half-life, matching the provider statistics. An observation from a
#: month ago counts for half of one from today.
DEFAULT_HALF_LIFE_DAYS = 30.0


class EvidenceState(StrEnum):
    """What the accumulated evidence says about an endpoint today."""

    #: Seen, but not on enough distinct pages yet.
    PROMISING = "PROMISING"
    #: Enough distinct pages, stable schema, no rejection.
    VALIDATED = "VALIDATED"
    #: Was validated; its schema has since changed. Yesterday's confidence does
    #: not carry over — the endpoint must earn the verdict again.
    REVALIDATION_REQUIRED = "REVALIDATION_REQUIRED"
    #: Screened out. Kept so an operator can see what was refused and why.
    REJECTED = "REJECTED"

    @property
    def is_usable(self) -> bool:
        return self is EvidenceState.VALIDATED


@dataclass(frozen=True)
class Evidence:
    """Everything known about one endpoint, across every run that saw it."""

    identity: str
    domain: str
    url_class: str
    method: str
    endpoint: str
    state: EvidenceState
    graphql_operation: str | None = None
    schema_signature: str | None = None
    pagination: dict[str, Any] = field(default_factory=dict)
    matched_fields: dict[str, str] = field(default_factory=dict)
    distinct_pages: int = 0
    observation_count: int = 0
    validated_count: int = 0
    rejected_count: int = 0
    rejection_detail: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    schema_changes: int = 0

    def age_days(self, *, now: float) -> float:
        return max(0.0, (now - self.last_seen) / 86400.0)

    def decay_factor(self, *, now: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
        """Weight this evidence still carries. Same shape as provider stats."""

        if half_life_days <= 0 or not self.last_seen:
            return 1.0
        return float(0.5 ** (self.age_days(now=now) / half_life_days))

    def confidence(self, *, now: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> str:
        """How much an operator should trust this today, not when it was seen."""

        if self.state is not EvidenceState.VALIDATED:
            return "NONE"
        weight = self.decay_factor(now=now, half_life_days=half_life_days)
        score = weight * min(self.distinct_pages / DEFAULT_MIN_DISTINCT_PAGES, 2.0)
        if self.matched_fields:
            score += 0.5 * weight
        if score >= 1.5:
            return "HIGH"
        return "MEDIUM" if score >= 0.7 else "LOW"

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        stamp = now if now is not None else time.time()
        return {
            "identity": self.identity,
            "domain": self.domain,
            "url_class": self.url_class,
            "method": self.method,
            "endpoint": self.endpoint,
            "graphql_operation": self.graphql_operation,
            "state": self.state.value,
            "confidence": self.confidence(now=stamp),
            "evidence_weight": round(self.decay_factor(now=stamp), 4),
            "schema_signature": self.schema_signature,
            "schema_changes": self.schema_changes,
            "pagination": self.pagination,
            "matched_fields": self.matched_fields,
            "distinct_pages": self.distinct_pages,
            "observation_count": self.observation_count,
            "validated_count": self.validated_count,
            "rejected_count": self.rejected_count,
            "rejection_detail": self.rejection_detail,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "age_days": round(self.age_days(now=stamp), 2),
        }


def page_fingerprint(page_url: str) -> str:
    """Identify a source page without storing it.

    A URL can carry a session id or a token in its query, and this table is one
    of the things an operator reads. A hash proves two observations came from
    different pages without recording which pages they were.
    """

    return hashlib.sha256(page_url.encode("utf-8")).hexdigest()[:16]


class DiscoveryStore:
    """SQLite evidence for discovered endpoints, shared across runs."""

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], float] = time.time,
        min_distinct_pages: int = DEFAULT_MIN_DISTINCT_PAGES,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        self.path = Path(path)
        self._now = now
        self.min_distinct_pages = min_distinct_pages
        self.max_age_days = max_age_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discovery_evidence (
                    identity TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    url_class TEXT NOT NULL DEFAULT '',
                    method TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    graphql_operation TEXT,
                    state TEXT NOT NULL,
                    schema_signature TEXT,
                    schema_changes INTEGER NOT NULL DEFAULT 0,
                    pagination TEXT NOT NULL DEFAULT '{}',
                    matched_fields TEXT NOT NULL DEFAULT '{}',
                    observation_count INTEGER NOT NULL DEFAULT 0,
                    validated_count INTEGER NOT NULL DEFAULT 0,
                    rejected_count INTEGER NOT NULL DEFAULT 0,
                    rejection_detail TEXT NOT NULL DEFAULT '',
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL
                )
                """
            )
            # Distinct source pages live in their own table so the primary key
            # does the deduplication: ten renders of one page insert one row.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discovery_pages (
                    identity TEXT NOT NULL,
                    page_hash TEXT NOT NULL,
                    seen_at REAL NOT NULL,
                    PRIMARY KEY (identity, page_hash)
                )
                """
            )
            for statement in (
                "CREATE INDEX IF NOT EXISTS discovery_domain ON discovery_evidence(domain)",
                "CREATE INDEX IF NOT EXISTS discovery_class ON discovery_evidence(url_class)",
                "CREATE INDEX IF NOT EXISTS discovery_state ON discovery_evidence(state)",
                "CREATE INDEX IF NOT EXISTS discovery_last_seen ON discovery_evidence(last_seen)",
                "CREATE INDEX IF NOT EXISTS discovery_pages_id ON discovery_pages(identity)",
            ):
                conn.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # -- writing -----------------------------------------------------------

    def record(
        self,
        candidate: RouteCandidate,
        *,
        domain: str,
        url_class: str = "",
        source_pages: Sequence[str] = (),
    ) -> Evidence:
        """Fold one run's observation of a candidate into the durable evidence."""

        now = self._now()
        identity = candidate.identity
        existing = self.get(identity)
        schema = candidate.schema.signature if candidate.schema else None

        with closing(self._connect()) as conn, conn:
            if existing is None and self._domain_is_full(conn, domain):
                # A chatty application must not be able to fill the store on its
                # own. Existing evidence still updates; only new identities are
                # refused, and the refusal is visible in the count.
                return Evidence(
                    identity=identity,
                    domain=domain,
                    url_class=url_class,
                    method=candidate.method,
                    endpoint=candidate.url,
                    state=EvidenceState.REJECTED,
                    rejection_detail=f"per-domain candidate cap reached for {domain}",
                    first_seen=now,
                    last_seen=now,
                )

            for page in source_pages:
                conn.execute(
                    "INSERT OR IGNORE INTO discovery_pages(identity, page_hash, seen_at) "
                    "VALUES (?, ?, ?)",
                    (identity, page_fingerprint(page), now),
                )
            self._prune_pages(conn, identity)

            # State is decided AFTER this run's pages are in, and counted on the
            # same connection. Deciding first meant the page count lagged by a
            # whole run: an endpoint reached its third distinct page and was
            # still recorded as PROMISING until the run after.
            distinct = int(
                conn.execute(
                    "SELECT COUNT(*) FROM discovery_pages WHERE identity = ?", (identity,)
                ).fetchone()[0]
            )
            state, changes, detail = self._next_state(existing, candidate, schema, distinct)

            conn.execute(
                """
                INSERT INTO discovery_evidence
                    (identity, domain, url_class, method, endpoint, graphql_operation,
                     state, schema_signature, schema_changes, pagination, matched_fields,
                     observation_count, validated_count, rejected_count, rejection_detail,
                     first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET
                    url_class=excluded.url_class,
                    endpoint=excluded.endpoint,
                    state=excluded.state,
                    schema_signature=excluded.schema_signature,
                    schema_changes=excluded.schema_changes,
                    pagination=excluded.pagination,
                    matched_fields=excluded.matched_fields,
                    observation_count=discovery_evidence.observation_count + ?,
                    validated_count=discovery_evidence.validated_count + ?,
                    rejected_count=discovery_evidence.rejected_count + ?,
                    rejection_detail=excluded.rejection_detail,
                    last_seen=excluded.last_seen
                """,
                (
                    identity,
                    domain,
                    url_class,
                    candidate.method,
                    candidate.url,
                    candidate.graphql_operation,
                    state.value,
                    schema,
                    changes,
                    json.dumps(candidate.pagination.to_dict()),
                    json.dumps(candidate.matched_fields),
                    candidate.observed_count,
                    1 if candidate.verdict.is_usable else 0,
                    1 if candidate.verdict.is_rejected else 0,
                    detail,
                    now,
                    now,
                    candidate.observed_count,
                    1 if candidate.verdict.is_usable else 0,
                    1 if candidate.verdict.is_rejected else 0,
                ),
            )

        stored = self.get(identity)
        assert stored is not None
        return stored

    def _next_state(
        self,
        existing: Evidence | None,
        candidate: RouteCandidate,
        schema: str | None,
        distinct_pages: int,
    ) -> tuple[EvidenceState, int, str]:
        """Decide the state, and whether a schema change retires a verdict."""

        if candidate.verdict.is_rejected:
            return (
                EvidenceState.REJECTED,
                (existing.schema_changes if existing else 0),
                (candidate.rejection_detail),
            )

        changes = existing.schema_changes if existing else 0
        if (
            existing is not None
            and existing.schema_signature
            and schema
            and existing.schema_signature != schema
        ):
            # An endpoint that changed shape is not the endpoint we validated.
            # Carrying the old verdict forward is how a profile keeps reading a
            # field that moved.
            return (
                EvidenceState.REVALIDATION_REQUIRED,
                changes + 1,
                ("the endpoint's schema changed since it was validated"),
            )

        if distinct_pages >= self.min_distinct_pages:
            return EvidenceState.VALIDATED, changes, ""
        return EvidenceState.PROMISING, changes, ""

    # -- reading -----------------------------------------------------------

    def get(self, identity: str) -> Evidence | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM discovery_evidence WHERE identity = ?", (identity,)
            ).fetchone()
            if row is None:
                return None
            pages = conn.execute(
                "SELECT COUNT(*) FROM discovery_pages WHERE identity = ?", (identity,)
            ).fetchone()[0]
        return _from_row(row, pages)

    def all_evidence(
        self, *, domain: str | None = None, state: EvidenceState | None = None
    ) -> list[Evidence]:
        # One query per filter combination rather than an assembled WHERE
        # clause. The values were always parameterised, but a SQL string built
        # by concatenation is a pattern worth not having in the file at all.
        base = "SELECT * FROM discovery_evidence"
        order = " ORDER BY last_seen DESC"
        if domain and state:
            query = f"{base} WHERE domain = ? AND state = ?{order}"
            params: tuple[str, ...] = (domain, state.value)
        elif domain:
            query = f"{base} WHERE domain = ?{order}"
            params = (domain,)
        elif state:
            query = f"{base} WHERE state = ?{order}"
            params = (state.value,)
        else:
            query = f"{base}{order}"
            params = ()

        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
            counts = {
                row["identity"]: row["n"]
                for row in conn.execute(
                    "SELECT identity, COUNT(*) AS n FROM discovery_pages GROUP BY identity"
                )
            }
        return [_from_row(row, counts.get(row["identity"], 0)) for row in rows]

    def validated(self, *, domain: str | None = None) -> list[Evidence]:
        return self.all_evidence(domain=domain, state=EvidenceState.VALIDATED)

    # -- housekeeping ------------------------------------------------------

    def _domain_is_full(self, conn: sqlite3.Connection, domain: str) -> bool:
        count = conn.execute(
            "SELECT COUNT(*) FROM discovery_evidence WHERE domain = ?", (domain,)
        ).fetchone()[0]
        return int(count) >= MAX_CANDIDATES_PER_DOMAIN

    @staticmethod
    def _prune_pages(conn: sqlite3.Connection, identity: str) -> None:
        """Keep only the most recent page hashes. Diversity needs a few, not all."""

        conn.execute(
            """
            DELETE FROM discovery_pages
            WHERE identity = ? AND page_hash NOT IN (
                SELECT page_hash FROM discovery_pages
                WHERE identity = ? ORDER BY seen_at DESC LIMIT ?
            )
            """,
            (identity, identity, MAX_PAGE_HASHES),
        )

    def prune(self, *, max_age_days: int | None = None) -> dict[str, int]:
        """Drop evidence nobody has seen recently. A store that grows without
        limit is a store that eventually stops a run."""

        cutoff = self._now() - (max_age_days or self.max_age_days) * 86400
        with closing(self._connect()) as conn, conn:
            stale = [
                row["identity"]
                for row in conn.execute(
                    "SELECT identity FROM discovery_evidence WHERE last_seen < ?", (cutoff,)
                )
            ]
            for identity in stale:
                conn.execute("DELETE FROM discovery_pages WHERE identity = ?", (identity,))
            conn.execute("DELETE FROM discovery_evidence WHERE last_seen < ?", (cutoff,))
        return {"pruned": len(stale)}

    def summary(self, *, now: float | None = None) -> dict[str, Any]:
        stamp = now if now is not None else self._now()
        everything = self.all_evidence()
        by_state: dict[str, int] = {}
        for item in everything:
            by_state[item.state.value] = by_state.get(item.state.value, 0) + 1
        return {
            "discovery_candidates_total": len(everything),
            "discovery_validated_total": by_state.get(EvidenceState.VALIDATED.value, 0),
            "discovery_rejected_total": by_state.get(EvidenceState.REJECTED.value, 0),
            "discovery_revalidation_required": by_state.get(
                EvidenceState.REVALIDATION_REQUIRED.value, 0
            ),
            "by_state": by_state,
            "validated": [
                item.to_dict(now=stamp)
                for item in everything
                if item.state is EvidenceState.VALIDATED
            ],
        }


def _from_row(row: sqlite3.Row, distinct_pages: int) -> Evidence:
    return Evidence(
        identity=row["identity"],
        domain=row["domain"],
        url_class=row["url_class"],
        method=row["method"],
        endpoint=row["endpoint"],
        graphql_operation=row["graphql_operation"],
        state=EvidenceState(row["state"]),
        schema_signature=row["schema_signature"],
        schema_changes=row["schema_changes"],
        pagination=json.loads(row["pagination"]),
        matched_fields=json.loads(row["matched_fields"]),
        distinct_pages=distinct_pages,
        observation_count=row["observation_count"],
        validated_count=row["validated_count"],
        rejected_count=row["rejected_count"],
        rejection_detail=row["rejection_detail"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
    )


def evidence_to_candidate(evidence: Evidence) -> RouteCandidate:
    """Rebuild a candidate from stored evidence, for draft generation.

    The stored form is deliberately lossy — no body, no values — so what comes
    back is enough to propose a route and nothing more.
    """

    return RouteCandidate(
        url=evidence.endpoint,
        method=evidence.method,
        status=200,
        content_type="application/json",
        observed_count=evidence.observation_count,
        schema=(
            SchemaSignature(signature=evidence.schema_signature)
            if evidence.schema_signature
            else None
        ),
        pagination=PaginationHint(
            strategy=str(evidence.pagination.get("strategy", "NONE")),
            parameters=tuple(evidence.pagination.get("parameters", ())),
            cursor_field=evidence.pagination.get("cursor_field"),
            total_field=evidence.pagination.get("total_field"),
            has_more_field=evidence.pagination.get("has_more_field"),
        ),
        graphql_operation=evidence.graphql_operation,
        verdict=(
            CandidateVerdict.VALIDATED
            if evidence.state is EvidenceState.VALIDATED
            else CandidateVerdict.PROMISING
        ),
        matched_fields=dict(evidence.matched_fields),
    )
