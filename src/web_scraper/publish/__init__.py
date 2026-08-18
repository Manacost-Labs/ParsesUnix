"""Data publication: staging -> validate -> atomic promote, or reject + keep LKG.

A half-updated dataset is the worst outcome because nobody notices. This layer
guarantees the opposite: a run's rows land in staging, are validated as a whole
against the profile's promote thresholds, and are promoted atomically only if
they pass. On failure the clean dataset is untouched and the last-known-good
(LKG) snapshot remains the served version.
"""

from web_scraper.publish.store import (
    DatasetStore,
    PromoteDecision,
    validate_staging,
)

__all__ = ["DatasetStore", "PromoteDecision", "validate_staging"]
