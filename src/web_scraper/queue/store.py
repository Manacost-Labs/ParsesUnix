"""The run queue: one durable row per URL, safe to interrupt and resume.

Design goals (Stage 2/4 acceptance: "no silent skips; a re-run creates no
duplicates"):

* every URL is added at most once (normalized-URL primary key);
* every URL always carries a status, so nothing vanishes silently;
* a crash mid-run resumes from the queue, not from zero — ``claim_batch``
  only hands out PENDING/RETRY rows and marks them IN_PROGRESS;
* 404/410 go to ``quarantine``; "not fetchable by anything" go to
  ``dead_zones`` — both visible to a human, never dissolved into stats.

All writes go through short transactions with ``closing`` so the store never
leaks connections over a long run.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from web_scraper.queue.normalize import normalize_url


class UrlStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    RETRY = "RETRY"
    QUARANTINED = "QUARANTINED"  # 404/410 — resource is gone
    DEAD_ZONE = "DEAD_ZONE"  # not fetchable by any route/level — a system defect
    FAILED = "FAILED"  # terminal non-OK with a recorded verdict


#: Statuses a run may still act on.
ACTIVE_STATUSES = (UrlStatus.PENDING, UrlStatus.RETRY)


@dataclass(frozen=True)
class QueuedUrl:
    url: str
    url_class: str | None
    status: UrlStatus
    verdict: str | None
    attempts: int
    natural_key: str | None
    content_hash: str | None
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "QueuedUrl":
        return cls(
            url=row["url"],
            url_class=row["url_class"],
            status=UrlStatus(row["status"]),
            verdict=row["verdict"],
            attempts=row["attempts"],
            natural_key=row["natural_key"],
            content_hash=row["content_hash"],
            updated_at=row["updated_at"],
        )


class QueueStore:
    def __init__(self, path: str | Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS urls (
                    url TEXT PRIMARY KEY,
                    url_class TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    verdict TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    natural_key TEXT,
                    content_hash TEXT,
                    not_before REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS urls_status_idx ON urls(status, not_before);
                CREATE INDEX IF NOT EXISTS urls_natural_key_idx ON urls(natural_key);

                CREATE TABLE IF NOT EXISTS attempts_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    level TEXT,
                    reason TEXT,
                    at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempts_url_idx ON attempts_log(url);

                CREATE TABLE IF NOT EXISTS quarantine (
                    url TEXT PRIMARY KEY,
                    first_seen_dead REAL NOT NULL,
                    last_check REAL NOT NULL,
                    checks INTEGER NOT NULL DEFAULT 1,
                    last_status INTEGER
                );

                CREATE TABLE IF NOT EXISTS dead_zones (
                    url TEXT PRIMARY KEY,
                    verdict_history TEXT NOT NULL,
                    last_snapshot TEXT,
                    since REAL NOT NULL
                );
                """
            )

    # -- enqueue -----------------------------------------------------------

    def add(self, url: str, *, url_class: str | None = None, natural_key: str | None = None) -> bool:
        """Add one URL. Returns True if newly inserted, False if already present."""

        normalized = normalize_url(url)
        now = self._now()
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO urls(url, url_class, natural_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(url) DO NOTHING
                """,
                (normalized, url_class, natural_key, now, now),
            )
            return cursor.rowcount > 0

    def add_many(self, urls: Iterable[str], *, url_class: str | None = None) -> dict[str, int]:
        added = skipped = 0
        for url in urls:
            if self.add(url, url_class=url_class):
                added += 1
            else:
                skipped += 1
        return {"added": added, "skipped_duplicate": skipped}

    # -- claim / complete --------------------------------------------------

    def claim_batch(self, limit: int = 20) -> list[QueuedUrl]:
        """Atomically move up to ``limit`` runnable rows to IN_PROGRESS."""

        now = self._now()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM urls
                WHERE status IN ('PENDING', 'RETRY') AND not_before <= ?
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            claimed = [QueuedUrl.from_row(row) for row in rows]
            for row in rows:
                conn.execute(
                    "UPDATE urls SET status = 'IN_PROGRESS', updated_at = ? WHERE url = ?",
                    (now, row["url"]),
                )
            conn.commit()
        return claimed

    def mark_done(
        self,
        url: str,
        *,
        verdict: str,
        content_hash: str | None = None,
        natural_key: str | None = None,
    ) -> None:
        self._set_status(
            url, UrlStatus.DONE, verdict=verdict, content_hash=content_hash, natural_key=natural_key
        )

    def mark_failed(self, url: str, *, verdict: str) -> None:
        self._set_status(url, UrlStatus.FAILED, verdict=verdict)

    def schedule_retry(self, url: str, *, verdict: str, delay_seconds: float) -> None:
        now = self._now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE urls
                SET status = 'RETRY', verdict = ?, attempts = attempts + 1,
                    not_before = ?, updated_at = ?
                WHERE url = ?
                """,
                (verdict, now + delay_seconds, now, normalize_url(url)),
            )

    def quarantine_url(self, url: str, *, status_code: int | None = None) -> None:
        normalized = normalize_url(url)
        now = self._now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO quarantine(url, first_seen_dead, last_check, checks, last_status)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(url) DO UPDATE SET
                    last_check = excluded.last_check,
                    checks = quarantine.checks + 1,
                    last_status = excluded.last_status
                """,
                (normalized, now, now, status_code),
            )
            conn.execute(
                "UPDATE urls SET status = 'QUARANTINED', verdict = 'DEAD_URL', updated_at = ? WHERE url = ?",
                (now, normalized),
            )

    def mark_dead_zone(
        self, url: str, *, verdict_history: Sequence[str], last_snapshot: str | None = None
    ) -> None:
        normalized = normalize_url(url)
        now = self._now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO dead_zones(url, verdict_history, last_snapshot, since)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    verdict_history = excluded.verdict_history,
                    last_snapshot = excluded.last_snapshot
                """,
                (normalized, json.dumps(list(verdict_history)), last_snapshot, now),
            )
            conn.execute(
                "UPDATE urls SET status = 'DEAD_ZONE', updated_at = ? WHERE url = ?",
                (now, normalized),
            )

    def log_attempt(self, url: str, *, verdict: str, level: str | None, reason: str | None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO attempts_log(url, verdict, level, reason, at) VALUES (?, ?, ?, ?, ?)",
                (normalize_url(url), verdict, level, reason, self._now()),
            )

    def _set_status(
        self,
        url: str,
        status: UrlStatus,
        *,
        verdict: str | None = None,
        content_hash: str | None = None,
        natural_key: str | None = None,
    ) -> None:
        now = self._now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE urls
                SET status = ?, verdict = COALESCE(?, verdict),
                    content_hash = COALESCE(?, content_hash),
                    natural_key = COALESCE(?, natural_key),
                    attempts = attempts + 1, updated_at = ?
                WHERE url = ?
                """,
                (status.value, verdict, content_hash, natural_key, now, normalize_url(url)),
            )

    # -- recovery / inspection --------------------------------------------

    def reset_stale_in_progress(self) -> int:
        """Return IN_PROGRESS rows (a crashed run) to PENDING so a re-run continues."""

        now = self._now()
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE urls SET status = 'PENDING', updated_at = ? WHERE status = 'IN_PROGRESS'",
                (now,),
            )
            return cursor.rowcount

    def get(self, url: str) -> QueuedUrl | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM urls WHERE url = ?", (normalize_url(url),)
            ).fetchone()
        return QueuedUrl.from_row(row) if row else None

    def counts_by_status(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM urls GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    def pending_count(self) -> int:
        now = self._now()
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM urls
                WHERE status IN ('PENDING', 'RETRY') AND not_before <= ?
                """,
                (now,),
            ).fetchone()
        return int(row["n"])

    def dead_zones(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM dead_zones ORDER BY since").fetchall()
        return [
            {
                "url": row["url"],
                "verdict_history": json.loads(row["verdict_history"]),
                "last_snapshot": row["last_snapshot"],
                "since": row["since"],
            }
            for row in rows
        ]

    def quarantined(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM quarantine ORDER BY first_seen_dead").fetchall()
        return [dict(row) for row in rows]

    def all_rows(self) -> list[QueuedUrl]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM urls ORDER BY created_at").fetchall()
        return [QueuedUrl.from_row(row) for row in rows]
