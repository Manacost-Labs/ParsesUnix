"""SQLite dataset store with a staging table and an atomic promote.

Tables:
* ``clean``   — the served dataset (one row per natural key);
* ``staging`` — the current run's candidate rows;
* ``lkg``     — last-known-good copy, written just before each promote so a
  later bad run can be diagnosed against the version it would have replaced.

Promotion runs inside a single transaction: either every staged row replaces
its clean counterpart, or nothing changes.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PromoteDecision:
    ok: bool
    reason: str
    staged: int
    clean_before: int
    completeness: float
    null_rate: dict[str, float]
    conflicts: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "staged": self.staged,
            "clean_before": self.clean_before,
            "completeness": self.completeness,
            "null_rate": self.null_rate,
            "conflicts": self.conflicts,
        }


def validate_staging(
    rows: Sequence[Mapping[str, Any]],
    *,
    required_fields: Sequence[str],
    expected_count: int | None,
    min_completeness: float,
    baseline_null_rate: Mapping[str, float] | None = None,
    max_null_rate_growth: float = 2.0,
) -> PromoteDecision:
    """Whole-dataset validation: volume, required-field completeness, null drift."""

    staged = len(rows)
    baseline = baseline_null_rate or {}
    conflicts = sum(1 for r in rows if r.get("_conflict"))

    # Completeness = fraction of rows with every required field present & non-empty.
    def complete(row: Mapping[str, Any]) -> bool:
        return all(row.get(f) not in (None, "") for f in required_fields)

    completeness = (sum(1 for r in rows if complete(r)) / staged) if staged else 0.0

    null_rate: dict[str, float] = {}
    for f in required_fields:
        missing = sum(1 for r in rows if r.get(f) in (None, ""))
        null_rate[f] = (missing / staged) if staged else 1.0

    def decision(ok: bool, reason: str) -> PromoteDecision:
        return PromoteDecision(
            ok=ok,
            reason=reason,
            staged=staged,
            clean_before=0,
            completeness=round(completeness, 4),
            null_rate=null_rate,
            conflicts=conflicts,
        )

    if staged == 0:
        return decision(False, "no staged rows")
    if expected_count is not None and expected_count > 0:
        volume_ratio = staged / expected_count
        if volume_ratio < min_completeness:
            return decision(
                False,
                f"volume {staged}/{expected_count} = {volume_ratio:.2%} below min_completeness "
                f"{min_completeness:.0%}",
            )
    if completeness < min_completeness:
        return decision(
            False, f"required-field completeness {completeness:.2%} below {min_completeness:.0%}"
        )
    for f, rate in null_rate.items():
        base = baseline.get(f, 0.0)
        if base > 0 and rate > base * max_null_rate_growth:
            return decision(
                False,
                f"null-rate for {f!r} grew {rate:.2%} vs baseline {base:.2%} (> x{max_null_rate_growth})",
            )
        if base == 0 and rate > 0 and min_completeness >= 1.0:
            return decision(False, f"null-rate for required field {f!r} is {rate:.2%}, expected 0")
    return decision(True, "validation passed")


class DatasetStore:
    def __init__(self, path: str | Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _initialize(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS clean (
                    natural_key TEXT PRIMARY KEY, url TEXT, data TEXT NOT NULL,
                    content_hash TEXT, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS staging (
                    natural_key TEXT PRIMARY KEY, url TEXT, data TEXT NOT NULL,
                    content_hash TEXT, conflict INTEGER NOT NULL DEFAULT 0, staged_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lkg (
                    natural_key TEXT PRIMARY KEY, url TEXT, data TEXT NOT NULL,
                    content_hash TEXT, saved_at REAL NOT NULL
                );
                """
            )

    def reset_staging(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM staging")

    def stage(
        self,
        natural_key: str,
        *,
        url: str,
        data: Mapping[str, Any],
        content_hash: str | None = None,
        conflict: bool = False,
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO staging(natural_key, url, data, content_hash, conflict, staged_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(natural_key) DO UPDATE SET
                    url=excluded.url, data=excluded.data, content_hash=excluded.content_hash,
                    conflict=excluded.conflict, staged_at=excluded.staged_at
                """,
                (
                    natural_key,
                    url,
                    json.dumps(dict(data), ensure_ascii=False),
                    content_hash,
                    int(conflict),
                    self._now(),
                ),
            )

    def staged_rows(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM staging").fetchall()
        out = []
        for row in rows:
            record = json.loads(row["data"])
            record["_conflict"] = bool(row["conflict"])
            record["_natural_key"] = row["natural_key"]
            record["_url"] = row["url"]
            record["_content_hash"] = row["content_hash"]
            out.append(record)
        return out

    def clean_count(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM clean").fetchone()["n"])

    def baseline_null_rate(self, required_fields: Sequence[str]) -> dict[str, float]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT data FROM clean").fetchall()
        total = len(rows)
        if total == 0:
            return dict.fromkeys(required_fields, 0.0)
        records = [json.loads(r["data"]) for r in rows]
        return {
            f: sum(1 for rec in records if rec.get(f) in (None, "")) / total
            for f in required_fields
        }

    def promote(
        self,
        *,
        required_fields: Sequence[str],
        expected_count: int | None = None,
        min_completeness: float = 0.95,
        max_null_rate_growth: float = 2.0,
    ) -> PromoteDecision:
        """Validate staging as a whole; on pass, atomically replace clean rows."""

        rows = self.staged_rows()
        decision = validate_staging(
            rows,
            required_fields=required_fields,
            expected_count=expected_count,
            min_completeness=min_completeness,
            baseline_null_rate=self.baseline_null_rate(required_fields),
            max_null_rate_growth=max_null_rate_growth,
        )
        clean_before = self.clean_count()
        decision = PromoteDecision(
            ok=decision.ok,
            reason=decision.reason,
            staged=decision.staged,
            clean_before=clean_before,
            completeness=decision.completeness,
            null_rate=decision.null_rate,
            conflicts=decision.conflicts,
        )
        if not decision.ok:
            return decision  # reject: clean dataset untouched, staging kept for review

        now = self._now()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            # Snapshot current clean into LKG before overwriting.
            conn.execute("DELETE FROM lkg")
            conn.execute(
                "INSERT INTO lkg(natural_key, url, data, content_hash, saved_at) "
                "SELECT natural_key, url, data, content_hash, ? FROM clean",
                (now,),
            )
            for row in conn.execute("SELECT * FROM staging").fetchall():
                conn.execute(
                    """
                    INSERT INTO clean(natural_key, url, data, content_hash, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(natural_key) DO UPDATE SET
                        url=excluded.url, data=excluded.data,
                        content_hash=excluded.content_hash, updated_at=excluded.updated_at
                    """,
                    (row["natural_key"], row["url"], row["data"], row["content_hash"], now),
                )
            conn.execute("DELETE FROM staging")
            conn.commit()
        return decision

    def clean_rows_with_meta(self) -> list[dict[str, Any]]:
        """Clean rows plus the metadata a consumer needs to judge their age.

        ``clean_rows`` is convenient but deliberately anonymous about freshness;
        anything user-facing should go through availability instead.
        """

        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM clean ORDER BY natural_key").fetchall()
        return [
            {
                "natural_key": r["natural_key"],
                "url": r["url"],
                "updated_at": r["updated_at"],
                "content_hash": r["content_hash"],
                "data": json.loads(r["data"]),
            }
            for r in rows
        ]

    def clean_rows(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM clean ORDER BY natural_key").fetchall()
        return [
            {"natural_key": r["natural_key"], "url": r["url"], **json.loads(r["data"])}
            for r in rows
        ]
