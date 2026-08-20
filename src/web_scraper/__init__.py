"""Importable core of the web-scraper skill.

The scripts in ``.agents/skills/web-scraper/scripts/`` are thin CLI
wrappers over this package; all logic lives here.
"""

from web_scraper.contracts import (
    FREE_ESCALATION_VERDICTS,
    PAID_ESCALATION_VERDICTS,
    Attempt,
    ContentKind,
    ContentRules,
    Cost,
    CostCertainty,
    Level,
    Result,
    Route,
    RouteType,
    TriageResult,
    Verdict,
)
from web_scraper.embedded import (
    ResponseContract,
    ValidatedResponse,
    fetch_validated,
    validate_response,
)

__version__ = "0.10.0"

__all__ = [
    "FREE_ESCALATION_VERDICTS",
    "PAID_ESCALATION_VERDICTS",
    "Attempt",
    "ContentKind",
    "ContentRules",
    "Cost",
    "CostCertainty",
    "Level",
    "ResponseContract",
    "Result",
    "Route",
    "RouteType",
    "TriageResult",
    "ValidatedResponse",
    "Verdict",
    "__version__",
    "fetch_validated",
    "validate_response",
]
