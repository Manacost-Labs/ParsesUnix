"""Observability: run metrics, a per-URL run report, and an alert sink.

Data freshness is tracked separately from HTTP success (a 200 that failed
content validation is not a success), and every metric is secret-free.
"""

from web_scraper.observability.alerts import Alerter, AlertEvent, LoggingAlerter
from web_scraper.observability.metrics import RunMetrics, RunReport

__all__ = ["AlertEvent", "Alerter", "LoggingAlerter", "RunMetrics", "RunReport"]
