"""Failure diagnosis: why is this domain only collecting N% today?

Triage answers "what is this one response?". Diagnosis answers the operational
question above: it groups a run's failures by signature, quantifies each group,
names the likely root cause, and — critically — states the *correct* remedy
under this project's escalation policy.

That last part is the point. The expensive mistake in scraping is answering an
origin outage or a wall of 404s by escalating to a paid provider. The
recommendation for every group is derived from the verdict, so a `5xx` group can
never be labelled "escalate", no matter how large it is.
"""

from web_scraper.diagnose.analyze import (
    FailureGroup,
    Diagnosis,
    diagnose_attempts,
    diagnose_queue,
)

__all__ = ["Diagnosis", "FailureGroup", "diagnose_attempts", "diagnose_queue"]
