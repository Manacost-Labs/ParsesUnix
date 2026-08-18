"""Idempotent SQLite ledger for daily paid scraping budgets."""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


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
    ) -> None:
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--daily-credit-limit", required=True)
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
    args = parser.parse_args(argv)

    ledger = BudgetLedger(
        args.db,
        daily_credit_limit=args.daily_credit_limit,
        daily_money_limit=args.daily_money_limit,
    )
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
