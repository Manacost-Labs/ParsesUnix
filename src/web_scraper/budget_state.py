"""States a budget and a reservation can be in.

The guarantee this project can honestly make is not "we never exceed the limit":
an external provider can bill more than we estimated *after* the request has
left. What it can guarantee is narrower and actually achievable:

* no paid call is ever knowingly started without a sufficient hold;
* what was actually spent is always recorded truthfully;
* spend we cannot account for blocks further spending rather than being
  quietly written off;
* a crash never leads to paying blindly a second time.

These enums are how that guarantee is expressed in the ledger.
"""

from __future__ import annotations

from enum import StrEnum


class BudgetState(StrEnum):
    """Whether more paid work may start."""

    OK = "OK"
    WARNING = "WARNING"  # close to the limit; still permitted
    EXHAUSTED = "EXHAUSTED"  # the limit is reached; free work continues
    OVERSPENT = "OVERSPENT"  # a settled cost exceeded what was held: hard stop
    UNKNOWN_SPEND = "UNKNOWN_SPEND"  # spend we cannot account for: hard stop

    @property
    def allows_paid_work(self) -> bool:
        return self in {BudgetState.OK, BudgetState.WARNING}

    @property
    def is_incident(self) -> bool:
        """Needs a human. Not a state a run may clear by itself."""

        return self in {BudgetState.OVERSPENT, BudgetState.UNKNOWN_SPEND}


class ReservationState(StrEnum):
    """Where one reservation is in its life.

    The distinction that matters after a crash is SUBMITTED: a reservation that
    was never submitted cost nothing and can be released safely, while one that
    was submitted may have been billed and must not be released on a guess.
    """

    RESERVED = "RESERVED"  # money held, nothing sent yet
    SUBMITTED = "SUBMITTED"  # the request has left; the provider may have billed
    SETTLED = "SETTLED"  # the real cost is recorded
    RELEASED = "RELEASED"  # the call never happened; nothing charged
    UNKNOWN = "UNKNOWN"  # submitted, never settled — spend status unknown

    @property
    def is_open(self) -> bool:
        """Still holding money."""

        return self in {
            ReservationState.RESERVED,
            ReservationState.SUBMITTED,
            ReservationState.UNKNOWN,
        }

    @property
    def safe_to_release(self) -> bool:
        """Only a reservation that never reached the provider is free to drop."""

        return self is ReservationState.RESERVED


#: Ledger events, for the audit trail.
EVENT_CREATED = "created"
EVENT_SUBMITTED = "submitted"
EVENT_SETTLED = "settled"
EVENT_RELEASED = "released"
EVENT_MARKED_UNKNOWN = "marked_unknown"
EVENT_RECONCILED = "reconciled"
EVENT_OVERSPEND = "overspend"
