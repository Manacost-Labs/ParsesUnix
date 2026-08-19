"""Cost planning, canaries and spend analysis for the paid layer.

Four questions an operator must be able to answer before and after a paid run:

* *What will this cost?* — :mod:`.estimate`, computed without spending anything.
* *Is it safe to start?* — :mod:`.canary`, a few real calls that can veto a batch.
* *Was it worth it?* — :mod:`.analysis`, cost per valid result and what a simpler
  policy would have cost instead.
* *Did anything go wrong?* — :mod:`.analysis`, anomalies worth waking someone for.
"""

from web_scraper.finops.analysis import (
    CostAnomaly,
    CounterfactualSavings,
    SpendReport,
    counterfactual_savings,
    detect_anomalies,
    summarise_spend,
)
from web_scraper.finops.canary import CanaryOutcome, CanaryStatus, PaidCanary, select_canary_urls
from web_scraper.finops.estimate import CostEstimate, PhasePlan, estimate_run_cost
from web_scraper.finops.free_canary import (
    CanaryUrl,
    FreeCanary,
    FreeCanaryOutcome,
    FreeCanaryStatus,
    stratified_sample,
)

__all__ = [
    "CanaryOutcome",
    "CanaryStatus",
    "CanaryUrl",
    "CostAnomaly",
    "CostEstimate",
    "CounterfactualSavings",
    "FreeCanary",
    "FreeCanaryOutcome",
    "FreeCanaryStatus",
    "PaidCanary",
    "PhasePlan",
    "SpendReport",
    "counterfactual_savings",
    "detect_anomalies",
    "estimate_run_cost",
    "select_canary_urls",
    "stratified_sample",
    "summarise_spend",
]
