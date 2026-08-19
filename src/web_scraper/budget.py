"""Idempotent SQLite ledger for daily paid scraping budgets."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from web_scraper.budget_state import (
    EVENT_CREATED,
    EVENT_MARKED_UNKNOWN,
    EVENT_OVERSPEND,
    EVENT_RECONCILED,
    EVENT_RELEASED,
    EVENT_SETTLED,
    EVENT_SUBMITTED,
    BudgetState,
    ReservationState,
)


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Usage:
    day: str
    credits: Decimal
    money: Decimal
    requests: int

    def to_dict(self) -> dict[str, str | int]:
        result = asdict(self)
        result["credits"] = str(self.credits)
        result["money"] = str(self.money)
        return result


def _decimal(value: Decimal | int | float | str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("budget values must be finite and non-negative")
    return parsed


def _utc_day() -> str:
    """Today's billing day in UTC.

    Providers bill on a UTC day boundary, and two hosts in different timezones
    must agree on "today" — a local date would enforce the daily cap twice or
    not at all around midnight.
    """

    return datetime.now(UTC).date().isoformat()


def scrape_do_request_cost(headers: Mapping[str, str]) -> Decimal:
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    raw = normalized.get("scrape.do-request-cost")
    if raw is None:
        raise ValueError("Scrape.do-Request-Cost header is missing")
    return _decimal(raw)


class BudgetLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        daily_credit_limit: Decimal | int | float | str | None,
        daily_money_limit: Decimal | int | float | str | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._now = now
        self.path = Path(path)
        self.daily_credit_limit = (
            None if daily_credit_limit is None else _decimal(daily_credit_limit)
        )
        self.daily_money_limit = None if daily_money_limit is None else _decimal(daily_money_limit)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        # ``sqlite3.connect`` as a context manager only commits/rolls back the
        # transaction — it does NOT close the connection. Every caller wraps the
        # connection in ``closing`` so a long run does not leak file descriptors
        # and pin WAL read marks.
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    request_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    credits TEXT NOT NULL,
                    money TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS usage_events_day_idx ON usage_events(day, provider)"
            )
            # Reservations close the window between "we checked the budget" and
            # "the provider charged us". Without them N concurrent workers all
            # pass the check and all spend.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    strategy_id TEXT,
                    target_hash TEXT,
                    credits TEXT NOT NULL,
                    actual_credits TEXT,
                    state TEXT NOT NULL DEFAULT 'RESERVED',
                    provider_request_id TEXT,
                    created_at REAL NOT NULL DEFAULT 0,
                    submitted_at REAL,
                    settled_at REAL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS reservations_state_idx ON reservations(state)"
            )
            # Audit trail: every transition, so an operator can reconstruct what
            # happened to money after an incident.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reservation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reservation_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT,
                    at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS reservation_events_idx "
                "ON reservation_events(reservation_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS reservations_day_idx ON reservations(day, provider)"
            )

    @staticmethod
    def _sum_rows(rows: list[tuple[str, str]]) -> tuple[Decimal, Decimal]:
        credits = sum((_decimal(row[0]) for row in rows), Decimal("0"))
        money = sum((_decimal(row[1]) for row in rows), Decimal("0"))
        return credits, money

    def usage(self, *, day: str | None = None, provider: str | None = None) -> Usage:
        selected_day = day or _utc_day()
        query = "SELECT credits, money FROM usage_events WHERE day = ?"
        params: list[str] = [selected_day]
        if provider:
            query += " AND provider = ?"
            params.append(provider)
        with closing(self._connect()) as connection:
            rows = list(connection.execute(query, params))
        credits, money = self._sum_rows(rows)
        return Usage(selected_day, credits, money, len(rows))

    def _log(
        self,
        connection: sqlite3.Connection,
        reservation_id: str,
        event: str,
        detail: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO reservation_events(reservation_id, event, detail, at) VALUES (?,?,?,?)",
            (reservation_id, event, detail, self._now()),
        )

    def reserve(
        self,
        *,
        provider: str,
        credits: Decimal | int | float | str,
        strategy_id: str | None = None,
        target_hash: str | None = None,
        reservation_id: str | None = None,
        day: str | None = None,
    ) -> Reservation:
        """Hold the WORST-CASE cost before a paid call is made.

        The amount must be what the strategy could cost, not what we hope it
        costs: a hold of 1 against an actual charge of 10 is how a limit is
        breached while every individual check passed.

        Refuses outright when the budget is in an incident state — an
        unexplained overspend must stop further spending, not be spent past.
        """

        if not provider.strip():
            raise ValueError("provider must not be empty")
        selected_day = day or _utc_day()
        amount = _decimal(credits)
        holder = reservation_id or str(uuid.uuid4())

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state_locked(connection, selected_day)
            if not state.allows_paid_work:
                connection.rollback()
                raise BudgetExceeded(
                    f"budget state is {state.value}: no further paid work is permitted"
                )
            committed, _money = self._sum_rows(
                list(
                    connection.execute(
                        "SELECT credits, money FROM usage_events WHERE day = ?", (selected_day,)
                    )
                )
            )
            held = self._held_locked(connection, selected_day)
            projected = committed + held + amount
            if self.daily_credit_limit is not None and projected > self.daily_credit_limit:
                connection.rollback()
                raise BudgetExceeded(
                    f"daily credit limit {self.daily_credit_limit} would be exceeded: "
                    f"{committed} spent + {held} held + {amount} requested"
                )
            now = self._now()
            connection.execute(
                """
                INSERT INTO reservations(
                    reservation_id, day, provider, strategy_id, target_hash,
                    credits, state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    holder,
                    selected_day,
                    provider.strip(),
                    strategy_id,
                    target_hash,
                    str(amount),
                    ReservationState.RESERVED.value,
                    now,
                ),
            )
            self._log(connection, holder, EVENT_CREATED, f"{amount} credits held")
            connection.commit()
        return Reservation(
            holder,
            provider.strip(),
            selected_day,
            amount,
            state=ReservationState.RESERVED,
            strategy_id=strategy_id,
            target_hash=target_hash,
            created_at=now,
        )

    def mark_submitted(
        self, reservation: Reservation, *, provider_request_id: str | None = None
    ) -> Reservation:
        """Record that the request has left, BEFORE waiting for the answer.

        This is the line that makes crash recovery possible. A reservation still
        RESERVED after a crash never reached the provider and costs nothing; one
        marked SUBMITTED may have been billed and must not be released on a guess.

        Idempotent: marking twice does not change anything.
        """

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT state FROM reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            ).fetchone()
            if row is None or row[0] != ReservationState.RESERVED.value:
                return reservation  # already past this point, or gone
            now = self._now()
            connection.execute(
                "UPDATE reservations SET state = ?, submitted_at = ?, provider_request_id = ? "
                "WHERE reservation_id = ?",
                (
                    ReservationState.SUBMITTED.value,
                    now,
                    provider_request_id,
                    reservation.reservation_id,
                ),
            )
            self._log(
                connection,
                reservation.reservation_id,
                EVENT_SUBMITTED,
                provider_request_id or "",
            )
        return replace(
            reservation,
            state=ReservationState.SUBMITTED,
            submitted_at=self._now(),
            provider_request_id=provider_request_id,
        )

    def settle(
        self,
        reservation: Reservation,
        *,
        actual_credits: Decimal | int | float | str | None,
        money: Decimal | int | float | str = Decimal("0"),
    ) -> Usage:
        """Record what the call actually cost. The truth wins over the estimate.

        ``actual_credits=None`` means the provider told us nothing. That is not
        free: the reservation becomes UNKNOWN, keeps holding its amount, and the
        budget stops permitting paid work until a human resolves it.

        When the real cost exceeds what was held the excess is recorded anyway —
        hiding it to protect the limit would make the ledger lie — and the budget
        moves to OVERSPENT, which is a hard stop.

        Idempotent: settling an already-settled reservation changes nothing.
        """

        if actual_credits is None:
            self.mark_unknown(reservation, detail="provider reported no cost")
            return self.usage(day=reservation.day)

        actual = _decimal(actual_credits)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, credits FROM reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            ).fetchone()
            if row is not None and row[0] in {
                ReservationState.SETTLED.value,
                ReservationState.RELEASED.value,
            }:
                connection.rollback()
                return self.usage(day=reservation.day)  # already final

            held = _decimal(row[1]) if row is not None else reservation.credits
            now = self._now()
            connection.execute(
                """
                UPDATE reservations
                SET state = ?, actual_credits = ?, settled_at = ?
                WHERE reservation_id = ?
                """,
                (ReservationState.SETTLED.value, str(actual), now, reservation.reservation_id),
            )
            connection.execute(
                """
                INSERT INTO usage_events(request_id, day, provider, credits, money)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO NOTHING
                """,
                (
                    reservation.reservation_id,
                    reservation.day,
                    reservation.provider,
                    str(actual),
                    str(_decimal(money)),
                ),
            )
            self._log(connection, reservation.reservation_id, EVENT_SETTLED, str(actual))
            if actual > held:
                # Recorded, never hidden. The limit was breached by the provider,
                # and the operator has to know before more money is committed.
                self._log(
                    connection,
                    reservation.reservation_id,
                    EVENT_OVERSPEND,
                    f"held {held}, charged {actual}",
                )
            connection.commit()
        return self.usage(day=reservation.day)

    def mark_unknown(self, reservation: Reservation, *, detail: str = "") -> None:
        """Spend we cannot account for. The hold stays; paid work stops."""

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT state FROM reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            ).fetchone()
            if row is None or row[0] in {
                ReservationState.SETTLED.value,
                ReservationState.RELEASED.value,
            }:
                return
            connection.execute(
                "UPDATE reservations SET state = ? WHERE reservation_id = ?",
                (ReservationState.UNKNOWN.value, reservation.reservation_id),
            )
            self._log(connection, reservation.reservation_id, EVENT_MARKED_UNKNOWN, detail)

    def reconcile(
        self, reservation_id: str, *, actual_credits: Decimal | int | float | str, detail: str = ""
    ) -> None:
        """Resolve an UNKNOWN reservation once its real cost is established."""

        actual = _decimal(actual_credits)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT day, provider, state FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None or row[2] != ReservationState.UNKNOWN.value:
                return
            day, provider = row[0], row[1]
            connection.execute(
                "UPDATE reservations SET state = ?, actual_credits = ?, settled_at = ? "
                "WHERE reservation_id = ?",
                (ReservationState.SETTLED.value, str(actual), self._now(), reservation_id),
            )
            connection.execute(
                """
                INSERT INTO usage_events(request_id, day, provider, credits, money)
                VALUES (?, ?, ?, ?, '0')
                ON CONFLICT(request_id) DO NOTHING
                """,
                (reservation_id, day, provider, str(actual)),
            )
            self._log(connection, reservation_id, EVENT_RECONCILED, detail or str(actual))

    def release(self, reservation: Reservation) -> bool:
        """Drop a hold for a call that never reached the provider.

        Refuses to release anything already submitted: that money may genuinely
        have been spent, and releasing it would silently under-count the budget.
        Returns whether the hold was dropped. Idempotent.
        """

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT state FROM reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            ).fetchone()
            if row is None:
                return False
            if not ReservationState(row[0]).safe_to_release:
                return False
            connection.execute(
                "UPDATE reservations SET state = ? WHERE reservation_id = ?",
                (ReservationState.RELEASED.value, reservation.reservation_id),
            )
            self._log(connection, reservation.reservation_id, EVENT_RELEASED)
            return True

    @staticmethod
    def _held_locked(connection: sqlite3.Connection, day: str) -> Decimal:
        """Credits still held. UNKNOWN reservations keep holding on purpose."""

        open_states = tuple(s.value for s in ReservationState if s.is_open)
        placeholders = ",".join("?" for _ in open_states)
        rows = connection.execute(
            f"SELECT credits FROM reservations WHERE day = ? AND state IN ({placeholders})",  # noqa: S608
            (day, *open_states),
        ).fetchall()
        return sum((_decimal(row[0]) for row in rows), Decimal("0"))

    def held_credits(self, *, day: str | None = None) -> Decimal:
        selected_day = day or _utc_day()
        with closing(self._connect()) as connection:
            return self._held_locked(connection, selected_day)

    def _state_locked(self, connection: sqlite3.Connection, day: str) -> BudgetState:
        """The budget's state, derived from the ledger rather than stored.

        Derived on purpose: a stored flag can disagree with the rows it claims to
        summarise, and this is the one place where being wrong costs money.
        """

        unknown = connection.execute(
            "SELECT COUNT(*) FROM reservations WHERE state = ?",
            (ReservationState.UNKNOWN.value,),
        ).fetchone()[0]
        if unknown:
            return BudgetState.UNKNOWN_SPEND

        overspent = connection.execute(
            "SELECT COUNT(*) FROM reservation_events WHERE event = ?", (EVENT_OVERSPEND,)
        ).fetchone()[0]
        if overspent:
            return BudgetState.OVERSPENT

        if self.daily_credit_limit is None:
            return BudgetState.OK
        committed, _money = self._sum_rows(
            list(
                connection.execute("SELECT credits, money FROM usage_events WHERE day = ?", (day,))
            )
        )
        used = committed + self._held_locked(connection, day)
        if used >= self.daily_credit_limit:
            return BudgetState.EXHAUSTED
        if used >= self.daily_credit_limit * Decimal("0.8"):
            return BudgetState.WARNING
        return BudgetState.OK

    def state(self, *, day: str | None = None) -> BudgetState:
        """Whether more paid work may start, and why not if it may not."""

        selected_day = day or _utc_day()
        with closing(self._connect()) as connection:
            return self._state_locked(connection, selected_day)

    def open_reservations(self, *, states: Sequence[str] | None = None) -> list[Reservation]:
        """Reservations still holding money — the input to crash recovery."""

        wanted = tuple(states or [s.value for s in ReservationState if s.is_open])
        placeholders = ",".join("?" for _ in wanted)
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM reservations WHERE state IN ({placeholders}) ORDER BY created_at",  # noqa: S608
                wanted,
            ).fetchall()
        return [
            Reservation(
                reservation_id=row["reservation_id"],
                provider=row["provider"],
                day=row["day"],
                credits=_decimal(row["credits"]),
                state=ReservationState(row["state"]),
                strategy_id=row["strategy_id"],
                target_hash=row["target_hash"],
                actual_credits=(
                    _decimal(row["actual_credits"]) if row["actual_credits"] is not None else None
                ),
                provider_request_id=row["provider_request_id"],
                created_at=row["created_at"] or 0.0,
                submitted_at=row["submitted_at"],
            )
            for row in rows
        ]

    def recover_after_crash(self) -> dict[str, Any]:
        """Resolve reservations left open by a process that died mid-flight.

        The split is the whole point. A reservation still RESERVED never reached
        the provider, so releasing it is safe and correct. One already SUBMITTED
        may have been billed; guessing either way is wrong, so it becomes
        UNKNOWN — it keeps holding its money and stops further paid work until a
        human or a provider reconciliation resolves it.
        """

        released: list[str] = []
        unknown: list[str] = []
        for reservation in self.open_reservations():
            if reservation.state is ReservationState.RESERVED:
                if self.release(reservation):
                    released.append(reservation.reservation_id)
            elif reservation.state is ReservationState.SUBMITTED:
                self.mark_unknown(
                    reservation, detail="process crashed after submission; spend unconfirmed"
                )
                unknown.append(reservation.reservation_id)
        return {
            "released": released,
            "marked_unknown": unknown,
            "state": self.state().value,
        }

    def events(self, reservation_id: str) -> list[dict[str, Any]]:
        """The audit trail for one reservation."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT event, detail, at FROM reservation_events "
                "WHERE reservation_id = ? ORDER BY id",
                (reservation_id,),
            ).fetchall()
        return [{"event": r[0], "detail": r[1], "at": r[2]} for r in rows]

    def record(
        self,
        *,
        provider: str,
        credits: Decimal | int | float | str,
        money: Decimal | int | float | str = Decimal("0"),
        request_id: str | None = None,
        day: str | None = None,
    ) -> Usage:
        if not provider.strip():
            raise ValueError("provider must not be empty")
        selected_day = day or _utc_day()
        selected_id = request_id or str(uuid.uuid4())
        credit_value = _decimal(credits)
        money_value = _decimal(money)

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT credits, money FROM usage_events WHERE request_id = ?", (selected_id,)
            ).fetchone()
            rows = list(
                connection.execute(
                    "SELECT credits, money FROM usage_events WHERE day = ?", (selected_day,)
                )
            )
            current_credits, current_money = self._sum_rows(rows)
            if duplicate:
                connection.rollback()
                # A replayed request_id is idempotent, but a DIFFERENT amount for
                # the same id is a caller bug (a real new charge needs a new id).
                if _decimal(duplicate[0]) != credit_value or _decimal(duplicate[1]) != money_value:
                    raise ValueError(
                        f"request_id {selected_id!r} already recorded with a different amount "
                        f"({duplicate[0]}/{duplicate[1]} vs {credit_value}/{money_value}); "
                        "use a fresh request_id for a new charge"
                    )
                return Usage(selected_day, current_credits, current_money, len(rows))

            next_credits = current_credits + credit_value
            next_money = current_money + money_value
            if self.daily_credit_limit is not None and next_credits > self.daily_credit_limit:
                connection.rollback()
                raise BudgetExceeded(
                    f"daily credit limit {self.daily_credit_limit} would be exceeded by {next_credits}"
                )
            if self.daily_money_limit is not None and next_money > self.daily_money_limit:
                connection.rollback()
                raise BudgetExceeded(
                    f"daily money limit {self.daily_money_limit} would be exceeded by {next_money}"
                )
            connection.execute(
                """
                INSERT INTO usage_events(request_id, day, provider, credits, money)
                VALUES (?, ?, ?, ?, ?)
                """,
                (selected_id, selected_day, provider.strip(), str(credit_value), str(money_value)),
            )
            connection.commit()
        return Usage(selected_day, next_credits, next_money, len(rows) + 1)


@dataclass(frozen=True)
class Reservation:
    """Credits held for a paid call, and where that call has got to."""

    reservation_id: str
    provider: str
    day: str
    credits: Decimal
    state: ReservationState = ReservationState.RESERVED
    strategy_id: str | None = None
    target_hash: str | None = None
    actual_credits: Decimal | None = None
    provider_request_id: str | None = None
    created_at: float = 0.0
    submitted_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "provider": self.provider,
            "strategy_id": self.strategy_id,
            "day": self.day,
            "credits": str(self.credits),
            "actual_credits": str(self.actual_credits) if self.actual_credits is not None else None,
            "state": self.state.value,
            "provider_request_id": self.provider_request_id,
            "target_hash": self.target_hash,
            "created_at": self.created_at,
            "submitted_at": self.submitted_at,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--daily-credit-limit")
    parser.add_argument("--daily-money-limit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--provider", required=True)
    record_parser.add_argument("--credits", required=True)
    record_parser.add_argument("--money", default="0")
    record_parser.add_argument("--request-id")
    record_parser.add_argument("--day")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--provider")
    status_parser.add_argument("--day")

    subparsers.add_parser("pending", help="Reservations still holding money.")

    recover_parser = subparsers.add_parser(
        "recover", help="Resolve reservations left open by a crashed process."
    )
    recover_parser.add_argument("--dry-run", action="store_true")

    reconcile_parser = subparsers.add_parser(
        "reconcile", help="Record the real cost of an UNKNOWN reservation."
    )
    reconcile_parser.add_argument("--reservation-id", required=True)
    reconcile_parser.add_argument("--actual-credits", required=True)
    reconcile_parser.add_argument("--detail", default="manual reconciliation")
    args = parser.parse_args(argv)

    ledger = BudgetLedger(
        args.db,
        daily_credit_limit=args.daily_credit_limit,
        daily_money_limit=args.daily_money_limit,
    )
    if args.command == "pending":
        pending = [r.to_dict() for r in ledger.open_reservations()]
        print(
            json.dumps(
                {
                    "ok": True,
                    "state": ledger.state().value,
                    "held_credits": str(ledger.held_credits()),
                    "pending": pending,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "recover":
        if args.dry_run:
            open_now = ledger.open_reservations()
            plan = {
                "release": [r.reservation_id for r in open_now if r.state.safe_to_release],
                "mark_unknown": [r.reservation_id for r in open_now if not r.state.safe_to_release],
            }
            print(json.dumps({"ok": True, "dry_run": True, "plan": plan}, indent=2))
            return 0
        outcome = ledger.recover_after_crash()
        print(json.dumps({"ok": True, "recovery": outcome}, ensure_ascii=False, indent=2))
        # A non-zero exit tells an operator that money is still unaccounted for.
        return 1 if outcome["marked_unknown"] else 0

    if args.command == "reconcile":
        ledger.reconcile(
            args.reservation_id, actual_credits=args.actual_credits, detail=args.detail
        )
        print(
            json.dumps(
                {"ok": True, "state": ledger.state().value, "held": str(ledger.held_credits())},
                indent=2,
            )
        )
        return 0

    try:
        if args.command == "record":
            usage = ledger.record(
                provider=args.provider,
                credits=args.credits,
                money=args.money,
                request_id=args.request_id,
                day=args.day,
            )
        else:
            usage = ledger.usage(provider=args.provider, day=args.day)
    except BudgetExceeded as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "usage": usage.to_dict()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
