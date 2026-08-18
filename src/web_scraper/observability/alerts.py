"""Alert sink abstraction. Default is a structured log line; Telegram optional.

Alerts fire on circuit-breaker trips, quorum conflicts, promote rejections, and
dead zones. No secrets are ever included in an alert payload.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger("web_scraper.alerts")


@dataclass(frozen=True)
class AlertEvent:
    kind: str  # circuit_breaker | quorum_conflict | promote_rejected | dead_zone | budget_exceeded
    message: str
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "context": dict(self.context)}


class Alerter(Protocol):
    def send(self, event: AlertEvent) -> None: ...


class LoggingAlerter:
    """Emits one structured WARNING log line per alert (the safe default)."""

    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    def send(self, event: AlertEvent) -> None:
        self.events.append(event)
        logger.warning("ALERT %s", json.dumps(event.to_dict(), ensure_ascii=False))


class MultiAlerter:
    def __init__(self, *sinks: Alerter) -> None:
        self._sinks = sinks

    def send(self, event: AlertEvent) -> None:
        for sink in self._sinks:
            try:
                sink.send(event)
            except Exception:  # an alert sink must never break a run
                logger.exception("alert sink failed for %s", event.kind)
