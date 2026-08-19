"""Hard ceilings on what a calibration session may spend.

Calibration is the one workload that deliberately calls strategies nobody has
evidence for, on purpose, repeatedly. That is exactly the shape of a run that
empties an account by lunchtime, so the ceiling is not advisory: a call that
could breach it does not happen.

Three rules, and each exists because the obvious alternative loses money:

* **Hold the worst case, settle the actual.** The same discipline the budget
  ledger uses. Checking the typical price and being billed the premium one is
  how every individual check passes while the total does not.
* **An unknown settlement stays charged at the ceiling.** A provider that
  reported nothing did not do it for free, and dropping the hold would let the
  session spend past its cap while the arithmetic looked fine.
* **An unbounded strategy is not callable unattended.** If no tariff can put a
  number on the worst case, there is no cap arithmetic to do. An operator may
  still fire one deliberately, in the open, with ``allow_unbounded``.

The caps come from the environment, so raising them is a visible act by whoever
owns the invoice rather than an edit to a default in a source file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

#: Ceiling for the whole session, across every vendor.
TOTAL_CAP_ENV = "MAX_PROVIDER_CALIBRATION_USD"

#: And per vendor, because one provider exhausting the shared pot would leave
#: the others uncalibrated and the comparison unfinished.
PROVIDER_CAP_ENV: Mapping[str, str] = {
    "scrape.do": "MAX_SCRAPE_DO_CALIBRATION_USD",
    "firecrawl": "MAX_FIRECRAWL_CALIBRATION_USD",
    "brightdata": "MAX_BRIGHTDATA_CALIBRATION_USD",
    "zenrows": "MAX_ZENROWS_CALIBRATION_USD",
    "zyte": "MAX_ZYTE_CALIBRATION_USD",
}

#: What a session may spend when the operator names no figure at all. Small
#: enough to be an accident nobody minds, large enough to calibrate the cheap
#: strategies that matter most.
DEFAULT_TOTAL_CAP_USD = Decimal("1.00")

STOP = "STOP_PROVIDER_CALIBRATION"


@dataclass(frozen=True)
class CapDecision:
    """Whether one prospective call may happen, and why not when it may not."""

    allowed: bool
    reason: str
    #: What would be held for this call. Zero when nothing may be spent.
    hold_usd: Decimal = Decimal("0")
    stop: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "hold_usd": str(self.hold_usd),
            "stop": self.stop,
        }


@dataclass
class SpendCaps:
    """The session's ceilings and what has been spent against them."""

    total_usd: Decimal = DEFAULT_TOTAL_CAP_USD
    per_provider_usd: dict[str, Decimal] = field(default_factory=dict)
    allow_unbounded: bool = False
    _committed: dict[str, Decimal] = field(default_factory=dict, repr=False)
    _stopped: dict[str, str] = field(default_factory=dict, repr=False)
    #: Set when the SESSION ceiling can no longer fund a call. Distinct from a
    #: single provider being finished: one vendor hitting its own cap leaves the
    #: comparison running, the session ceiling ends it.
    _session_stop: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, allow_unbounded: bool = False
    ) -> SpendCaps:
        env = os.environ if environ is None else environ
        total = _decimal(env.get(TOTAL_CAP_ENV), DEFAULT_TOTAL_CAP_USD) or DEFAULT_TOTAL_CAP_USD
        per_provider: dict[str, Decimal] = {}
        for provider, name in PROVIDER_CAP_ENV.items():
            value = _decimal(env.get(name), None)
            if value is not None:
                per_provider[provider] = value
        return cls(total_usd=total, per_provider_usd=per_provider, allow_unbounded=allow_unbounded)

    # -- accounting --------------------------------------------------------

    @property
    def spent_usd(self) -> Decimal:
        return sum(self._committed.values(), Decimal("0"))

    def spent_by(self, provider: str) -> Decimal:
        return self._committed.get(provider, Decimal("0"))

    def remaining_usd(self) -> Decimal:
        return max(Decimal("0"), self.total_usd - self.spent_usd)

    def remaining_for(self, provider: str) -> Decimal:
        cap = self.per_provider_usd.get(provider)
        room = self.remaining_usd()
        if cap is None:
            return room
        return min(room, max(Decimal("0"), cap - self.spent_by(provider)))

    def stopped_providers(self) -> dict[str, str]:
        return dict(self._stopped)

    @property
    def session_stopped(self) -> str | None:
        """The reason the session ended, if the ceiling ended it."""

        return self._session_stop

    # -- the gate ----------------------------------------------------------

    def admit(self, provider: str, worst_case_usd: Decimal | None) -> CapDecision:
        """May one call of this strategy happen?

        ``worst_case_usd`` is the tariff's upper bound, not its typical price.
        Admitting on the typical price and being billed the premium one is the
        failure mode this whole module exists to prevent.
        """

        if self._session_stop is not None:
            return CapDecision(False, self._session_stop, stop=True)
        if provider in self._stopped:
            return CapDecision(False, self._stopped[provider], stop=True)

        if worst_case_usd is None:
            if not self.allow_unbounded:
                return CapDecision(
                    False,
                    "no tariff bounds this strategy; unsafe for unattended paid calibration",
                )
            # Deliberate, operator-initiated, and it still cannot be counted:
            # the session's arithmetic is marked incomplete rather than wrong.
            return CapDecision(True, "unbounded cost, admitted by explicit operator request")

        if worst_case_usd > self.remaining_usd():
            self._session_stop = (
                f"{STOP}: a call worth up to ${worst_case_usd} does not fit the "
                f"${self.remaining_usd()} left of the ${self.total_usd} session cap"
            )
            return CapDecision(False, self._session_stop, stop=True)

        if worst_case_usd > self.remaining_for(provider):
            cap = self.per_provider_usd.get(provider)
            self._stopped[provider] = (
                f"{STOP}: a call worth up to ${worst_case_usd} does not fit the "
                f"${self.remaining_for(provider)} left of {provider}'s ${cap} cap"
            )
            return CapDecision(False, self._stopped[provider], stop=True)

        return CapDecision(True, "within caps", hold_usd=worst_case_usd)

    def commit(self, provider: str, *, hold_usd: Decimal, settled_usd: Decimal | None) -> Decimal:
        """Record what the call actually cost, replacing its hold.

        ``settled_usd`` of ``None`` means the provider told us nothing. The hold
        stays charged: an unknown cost is not a free one, and releasing it would
        let the session keep calling on money it may already have spent.
        """

        charged = hold_usd if settled_usd is None else max(settled_usd, Decimal("0"))
        self._committed[provider] = self.spent_by(provider) + charged
        return charged

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_usd": str(self.total_usd),
            "per_provider_usd": {k: str(v) for k, v in sorted(self.per_provider_usd.items())},
            "spent_usd": str(self.spent_usd),
            "spent_by_provider": {k: str(v) for k, v in sorted(self._committed.items())},
            "remaining_usd": str(self.remaining_usd()),
            "allow_unbounded": self.allow_unbounded,
            "stopped": dict(self._stopped),
            "session_stop": self._session_stop,
        }


def _decimal(raw: str | None, fallback: Decimal | None) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return fallback
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return fallback
    return value if value >= 0 else fallback
