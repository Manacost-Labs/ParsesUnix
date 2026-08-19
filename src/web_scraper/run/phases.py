"""Staged execution: drain the free work before any paid work begins.

Escalating each URL to a provider the moment it fails is what makes a large
crawl expensive. Consider ten thousand URLs during a five-minute origin wobble:
eight hundred come back ``ORIGIN_DOWN``. Per-URL escalation buys eight hundred
paid calls to fetch pages that a free retry twenty minutes later would have
returned for nothing.

So a run drains phases in order, and each phase only sees what the previous one
could not resolve:

.. code-block:: text

    A  free            every URL, L0-L2, no paid call is reachable
    B  free retry      only failures that plausibly heal on their own
    C  cheap paid      what is still unresolved, on affordable strategies
    D  expensive paid  only what no cheaper strategy can serve

Phase B is where most of the savings are, and its admission rule is the part
worth getting right. A transient network error, a rate limit whose window has
passed, an origin that was briefly down — those heal. A dead URL, an auth wall
or a page that parsed into nothing will fail identically in twenty minutes;
retrying them for free is merely slow, but *escalating* them is the expensive
mistake, and both are prevented by the same list.

Phase state is persisted. A process that dies during phase C must resume in
phase C: restarting the paid phases would pay twice for the same URLs, which is
the one error the whole budget system exists to prevent.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from web_scraper.contracts import Verdict


class Phase(StrEnum):
    """Ordered. Comparison is by :attr:`rank`, never by name."""

    FREE = "A:free"
    FREE_RETRY = "B:free-retry"
    CHEAP_PAID = "C:cheap-paid"
    EXPENSIVE_PAID = "D:expensive-paid"

    @property
    def rank(self) -> int:
        return _PHASE_ORDER.index(self)

    @property
    def is_paid(self) -> bool:
        return self in {Phase.CHEAP_PAID, Phase.EXPENSIVE_PAID}

    @property
    def next(self) -> Phase | None:
        index = self.rank + 1
        return _PHASE_ORDER[index] if index < len(_PHASE_ORDER) else None


_PHASE_ORDER: tuple[Phase, ...] = (
    Phase.FREE,
    Phase.FREE_RETRY,
    Phase.CHEAP_PAID,
    Phase.EXPENSIVE_PAID,
)

#: Failures that plausibly heal on their own, so a delayed free retry is worth
#: the wait. Everything else either succeeded or will fail the same way again.
RETRYABLE_FREE_VERDICTS = frozenset(
    {
        Verdict.ORIGIN_DOWN,
        Verdict.RATE_LIMITED,
        Verdict.PROVIDER_ERROR,
    }
)

#: Verdicts that must never reach phase B *or* the paid phases. A dead URL does
#: not come back, an auth wall is not ours to open, and a page that parsed into
#: nothing will parse into nothing again — paying for any of them is spending
#: with no mechanism of working.
TERMINAL_VERDICTS = frozenset(
    {
        Verdict.DEAD_URL,
        Verdict.AUTH_REQUIRED,
        Verdict.ACCESS_DENIED,
    }
)

#: Only these justify a paid attempt. Kept aligned with the escalator's own
#: policy — this list narrows what reaches it, never widens it.
PAID_ELIGIBLE_VERDICTS = frozenset({Verdict.BLOCKED, Verdict.SOFT_BLOCK})

#: How long phase B waits before retrying. Long enough for a wobble to pass,
#: short enough to stay inside a run window.
DEFAULT_RETRY_DELAY_SECONDS = 900.0


def admits(phase: Phase, verdict: Verdict) -> bool:
    """Does this phase take a URL that ended on this verdict?"""

    if verdict in TERMINAL_VERDICTS or verdict is Verdict.OK:
        return False
    if phase is Phase.FREE_RETRY:
        return verdict in RETRYABLE_FREE_VERDICTS
    if phase.is_paid:
        return verdict in PAID_ELIGIBLE_VERDICTS
    return True


@dataclass
class PhaseState:
    """Where a run is, and what each phase did."""

    run_id: str
    current: Phase = Phase.FREE
    completed: list[str] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    started_at: float = 0.0
    updated_at: float = 0.0

    @property
    def is_complete(self) -> bool:
        return len(self.completed) == len(_PHASE_ORDER)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "current": self.current.value,
            "completed": list(self.completed),
            "counts": self.counts,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "complete": self.is_complete,
        }


class PhaseStore:
    """SQLite phase state, so a crash resumes rather than restarts.

    Restarting the paid phases after a crash would pay twice for the same URLs.
    That is the single error the budget system exists to prevent, and it would
    be reintroduced here by simply forgetting where the run was.
    """

    def __init__(self, path: str | Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_phases (
                    run_id TEXT PRIMARY KEY,
                    current TEXT NOT NULL,
                    completed TEXT NOT NULL DEFAULT '[]',
                    counts TEXT NOT NULL DEFAULT '{}',
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def load(self, run_id: str) -> PhaseState | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM run_phases WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return PhaseState(
            run_id=row["run_id"],
            current=Phase(row["current"]),
            completed=list(json.loads(row["completed"])),
            counts=dict(json.loads(row["counts"])),
            started_at=row["started_at"],
            updated_at=row["updated_at"],
        )

    def save(self, state: PhaseState) -> None:
        state.updated_at = self._now()
        if not state.started_at:
            state.started_at = state.updated_at
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO run_phases
                    (run_id, current, completed, counts, started_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    current=excluded.current,
                    completed=excluded.completed,
                    counts=excluded.counts,
                    updated_at=excluded.updated_at
                """,
                (
                    state.run_id,
                    state.current.value,
                    json.dumps(state.completed),
                    json.dumps(state.counts),
                    state.started_at,
                    state.updated_at,
                ),
            )


@dataclass
class PhaseController:
    """Walks a run through the phases, remembering where it got to."""

    run_id: str
    store: PhaseStore | None = None
    #: Phases this run may execute at all. A run with no funded budget passes
    #: only the free phases, so paid work is impossible rather than merely
    #: unlikely.
    allowed: tuple[Phase, ...] = _PHASE_ORDER
    state: PhaseState = field(init=False)

    def __post_init__(self) -> None:
        loaded = self.store.load(self.run_id) if self.store else None
        # A completed cycle must not block the next scheduled run. Only an
        # UNFINISHED cycle is resumed; a finished one starts over, which is what
        # distinguishes "resume after a crash" from "run again tomorrow".
        if loaded is not None and loaded.is_complete:
            loaded = None
        self.state = loaded or PhaseState(run_id=self.run_id)
        if self.store:
            self.store.save(self.state)

    @property
    def current(self) -> Phase:
        return self.state.current

    def remaining(self) -> list[Phase]:
        """Phases still to run, resuming from where a crash left off."""

        done = set(self.state.completed)
        return [
            phase
            for phase in _PHASE_ORDER
            if phase.value not in done
            and phase in self.allowed
            and phase.rank >= self.state.current.rank
        ]

    def enter(self, phase: Phase) -> None:
        self.state.current = phase
        if self.store:
            self.store.save(self.state)

    def complete(self, phase: Phase, *, counts: dict[str, int] | None = None) -> None:
        if phase.value not in self.state.completed:
            self.state.completed.append(phase.value)
        if counts:
            self.state.counts[phase.value] = counts
        following = phase.next
        if following is not None:
            self.state.current = following
        if self.store:
            self.store.save(self.state)

    def select(self, phase: Phase, candidates: Sequence[tuple[str, Verdict]]) -> list[str]:
        """Which URLs this phase takes, given how each one last ended."""

        return [url for url, verdict in candidates if admits(phase, verdict)]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.state.to_dict(),
            "allowed": [p.value for p in self.allowed],
            "remaining": [p.value for p in self.remaining()],
        }
