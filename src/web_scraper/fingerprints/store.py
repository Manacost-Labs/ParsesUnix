"""Durable fingerprint memory with recovery evidence and retention."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web_scraper.fingerprints.model import FailureFingerprint, FingerprintRecord


@dataclass(frozen=True)
class RecoveryHint:
    """What history suggests for a failure we recognise."""

    digest: str
    label: str
    route_id: str
    successes: int
    observations: int

    @property
    def confidence(self) -> float:
        return self.successes / self.observations if self.observations else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "label": self.label,
            "recommended_route": self.route_id,
            "successes": self.successes,
            "observations": self.observations,
            "confidence": round(self.confidence, 4),
        }


class FingerprintStore:
    """Remembers failure shapes and which route eventually got past them."""

    def __init__(self, path: str | Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS fingerprints (
                    digest TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    routes_seen TEXT NOT NULL DEFAULT '[]',
                    recovery_routes TEXT NOT NULL DEFAULT '{}',
                    successful_recovery_count INTEGER NOT NULL DEFAULT 0,
                    last_recovery REAL,
                    sample TEXT
                );
                CREATE INDEX IF NOT EXISTS fingerprints_last_seen_idx
                    ON fingerprints(last_seen);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # -- writing -----------------------------------------------------------

    def record_failure(
        self, fingerprint: FailureFingerprint, *, route_id: str
    ) -> FingerprintRecord:
        """Note that this failure shape happened again, on this route."""

        now = self._now()
        existing = self.get(fingerprint.digest)
        routes = sorted({*(existing.routes_seen if existing else ()), route_id})
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO fingerprints(
                    digest, label, verdict, first_seen, last_seen, count, routes_seen, sample
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(digest) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    count = fingerprints.count + 1,
                    routes_seen = excluded.routes_seen
                """,
                (
                    fingerprint.digest,
                    fingerprint.label,
                    fingerprint.verdict,
                    now,
                    now,
                    json.dumps(routes),
                    json.dumps(fingerprint.to_dict()),
                ),
            )
        record = self.get(fingerprint.digest)
        assert record is not None  # just written
        return record

    def record_recovery(self, digest: str, *, route_id: str) -> FingerprintRecord | None:
        """Note that ``route_id`` got past a URL that had shown this failure."""

        existing = self.get(digest)
        if existing is None:
            return None
        recoveries = dict(existing.recovery_routes)
        recoveries[route_id] = recoveries.get(route_id, 0) + 1
        now = self._now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE fingerprints
                SET recovery_routes = ?,
                    successful_recovery_count = successful_recovery_count + 1,
                    last_recovery = ?
                WHERE digest = ?
                """,
                (json.dumps(recoveries), now, digest),
            )
        return self.get(digest)

    # -- reading -----------------------------------------------------------

    @staticmethod
    def _from_row(row: sqlite3.Row) -> FingerprintRecord:
        return FingerprintRecord(
            digest=row["digest"],
            label=row["label"],
            verdict=row["verdict"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            count=row["count"],
            routes_seen=tuple(json.loads(row["routes_seen"])),
            recovery_routes=json.loads(row["recovery_routes"]),
            successful_recovery_count=row["successful_recovery_count"],
            last_recovery=row["last_recovery"],
            sample=json.loads(row["sample"]) if row["sample"] else None,
        )

    def get(self, digest: str) -> FingerprintRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM fingerprints WHERE digest = ?", (digest,)).fetchone()
        return self._from_row(row) if row else None

    def recovery_hint(self, fingerprint: FailureFingerprint) -> RecoveryHint | None:
        """The route history says recovers this failure shape, if one does.

        This is a *hint for ordering only*. It cannot change a verdict, and the
        caller still applies the escalation policy — a recovery route at a paid
        level is not permission to spend money.
        """

        record = self.get(fingerprint.digest)
        if record is None or not record.recovery_routes:
            return None
        route_id = record.best_recovery
        assert route_id is not None  # non-empty recovery_routes
        return RecoveryHint(
            digest=record.digest,
            label=record.label,
            route_id=route_id,
            successes=record.recovery_routes[route_id],
            observations=record.count,
        )

    def all_records(self, *, limit: int = 500) -> list[FingerprintRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM fingerprints ORDER BY count DESC, last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    # -- retention ---------------------------------------------------------

    def prune(self, *, max_age_days: float = 90.0, keep_with_recoveries: bool = True) -> int:
        """Drop fingerprints nobody has seen for a while.

        Entries that carry recovery evidence are kept by default: they are the
        cheapest knowledge in the system and cost a single row each.
        """

        cutoff = self._now() - max_age_days * 86_400
        clause = "last_seen < ?"
        if keep_with_recoveries:
            clause += " AND successful_recovery_count = 0"
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(f"DELETE FROM fingerprints WHERE {clause}", (cutoff,))
            return int(cursor.rowcount)
