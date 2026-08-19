"""Where calibration evidence lives — deliberately not where production looks.

A calibration session is an experiment. It calls strategies nobody trusts yet,
on targets chosen to be hard, sometimes with caps that cut a provider off
mid-corpus. Folding that straight into the statistics the router consults would
mean the next production run routes on evidence gathered under conditions it
knows nothing about.

So calibration writes to its own directory, and the router only ever sees it if
an operator reads the promotion preview and says yes. See
:mod:`web_scraper.calibration.promote`.

Two things are stored:

``provider_stats.sqlite3``
    The ordinary :class:`~web_scraper.providers.stats.ProviderStatsStore`, in
    the calibration namespace. Same schema on purpose — promotion is then a
    copy of like into like, not a translation that could distort the numbers.

``attempts`` in ``calibration.sqlite3``
    One row per call, which the aggregate cannot reconstruct: latency
    percentiles, status fidelity, the reason a strategy was skipped. No bodies
    and no headers — an artifact that gets shared must not carry the page.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from web_scraper.providers.stats import ProviderStatsStore

#: Filenames, fixed so an operator can find them without reading the code.
STATS_DB = "provider_stats.sqlite3"
ATTEMPTS_DB = "calibration.sqlite3"


class CalibrationStore:
    """The isolated home of one machine's calibration history."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stats = ProviderStatsStore(self.directory / STATS_DB)
        self.path = self.directory / ATTEMPTS_DB
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    provider TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    url_class TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS attempts_session ON attempts(session, provider)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def record(self, session: str, outcome: Any) -> None:
        """Append one attempt. ``outcome`` supplies its own dict form."""

        payload = outcome.to_dict()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO attempts
                    (session, recorded_at, provider, strategy, domain, url_class,
                     target_kind, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session,
                    float(payload.get("recorded_at") or 0.0),
                    str(payload.get("provider") or ""),
                    str(payload.get("strategy") or ""),
                    str(payload.get("domain") or ""),
                    str(payload.get("url_class") or ""),
                    str(payload.get("target_kind") or ""),
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def attempts(self, session: str | None = None) -> Iterator[dict[str, Any]]:
        sql = "SELECT payload FROM attempts"
        args: tuple[Any, ...] = ()
        if session is not None:
            sql += " WHERE session=?"
            args = (session,)
        sql += " ORDER BY id"
        with closing(self._connect()) as conn:
            for row in conn.execute(sql, args):
                parsed = json.loads(row["payload"])
                if isinstance(parsed, dict):
                    yield parsed

    def sessions(self) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT session, MAX(recorded_at) AS last FROM attempts "
                "GROUP BY session ORDER BY last DESC"
            )
            return [str(row["session"]) for row in rows]
