"""Per-URL freshness state and the conditional-request / interval logic."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

_WS_RE = re.compile(rb"\s+")


def content_hash(body: bytes) -> str:
    """Whitespace-normalized hash so cosmetic reflow is not seen as a change."""

    return hashlib.sha256(_WS_RE.sub(b" ", body).strip()).hexdigest()


@dataclass(frozen=True)
class FreshnessRecord:
    url: str
    etag: str | None
    last_modified: str | None
    content_hash: str | None
    last_checked: float
    last_changed: float
    interval_seconds: float


class FreshnessStore:
    MIN_INTERVAL = 900.0  # 15 min
    MAX_INTERVAL = 30 * 86400.0  # 30 days

    def __init__(self, path: str | Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS freshness (
                    url TEXT PRIMARY KEY,
                    etag TEXT, last_modified TEXT, content_hash TEXT,
                    last_checked REAL NOT NULL, last_changed REAL NOT NULL,
                    interval_seconds REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get(self, url: str) -> FreshnessRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM freshness WHERE url = ?", (url,)).fetchone()
        if not row:
            return None
        return FreshnessRecord(
            url=row["url"], etag=row["etag"], last_modified=row["last_modified"],
            content_hash=row["content_hash"], last_checked=row["last_checked"],
            last_changed=row["last_changed"], interval_seconds=row["interval_seconds"],
        )

    def conditional_headers(self, url: str) -> dict[str, str]:
        record = self.get(url)
        headers: dict[str, str] = {}
        if record and record.etag:
            headers["If-None-Match"] = record.etag
        if record and record.last_modified:
            headers["If-Modified-Since"] = record.last_modified
        return headers

    def is_due(self, url: str, *, full_review: bool = False) -> bool:
        """Should this URL be fetched now? Unknown URLs are always due."""

        if full_review:
            return True
        record = self.get(url)
        if record is None:
            return True
        return (self._now() - record.last_checked) >= record.interval_seconds

    def record_result(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        not_modified: bool = False,
    ) -> tuple[bool, str | None]:
        """Update freshness state. Returns (changed, new_content_hash).

        The interval widens when content is unchanged and resets to the minimum
        when it changes — cheap adaptivity without a statistics store.
        """

        now = self._now()
        prev = self.get(url)
        header_map = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        etag = header_map.get("etag", prev.etag if prev else None)
        last_modified = header_map.get("last-modified", prev.last_modified if prev else None)

        if not_modified:
            new_hash = prev.content_hash if prev else None
            changed = False
        elif body is not None:
            new_hash = content_hash(body)
            changed = prev is None or new_hash != prev.content_hash
        else:
            new_hash = prev.content_hash if prev else None
            changed = True

        prev_interval = prev.interval_seconds if prev else self.MIN_INTERVAL
        if changed:
            interval = self.MIN_INTERVAL
            last_changed = now
        else:
            interval = min(prev_interval * 1.5, self.MAX_INTERVAL) if prev else self.MIN_INTERVAL
            last_changed = prev.last_changed if prev else now

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO freshness(url, etag, last_modified, content_hash,
                                      last_checked, last_changed, interval_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    etag=excluded.etag, last_modified=excluded.last_modified,
                    content_hash=excluded.content_hash, last_checked=excluded.last_checked,
                    last_changed=excluded.last_changed, interval_seconds=excluded.interval_seconds
                """,
                (url, etag, last_modified, new_hash, now, last_changed, interval),
            )
        return changed, new_hash
