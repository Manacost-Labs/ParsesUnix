"""Importable core of the web-scraper skill.

The scripts in ``.agents/skills/web-scraper/scripts/`` are thin CLI
wrappers over this package; all logic lives here.
"""

from web_scraper.contracts import (
    PAID_ESCALATION_VERDICTS,
    Attempt,
    ContentRules,
    Level,
    Result,
    Route,
    RouteType,
    TriageResult,
    Verdict,
)

__version__ = "0.1.0"

__all__ = [
    "PAID_ESCALATION_VERDICTS",
    "Attempt",
    "ContentRules",
    "Level",
    "Result",
    "Route",
    "RouteType",
    "TriageResult",
    "Verdict",
    "__version__",
]
