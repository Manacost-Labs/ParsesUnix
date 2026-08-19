"""Measuring which provider and strategy to use, and what the answer cost.

The fleet knew how to call five vendors. What it did not know was which one to
call for a given site, and the difference between those two things is the whole
of this package: evidence, gathered on one corpus, priced in USD per VALIDATED
result rather than per request, and kept out of production statistics until an
operator says otherwise.
"""

from web_scraper.calibration.caps import SpendCaps
from web_scraper.calibration.corpora import EXAMPLE_CORPUS
from web_scraper.calibration.corpus import Corpus, CorpusTarget, TargetKind, load_corpus
from web_scraper.calibration.harness import (
    AttemptOutcome,
    CalibrationHarness,
    PlannedCall,
    outcome_from_dict,
)
from web_scraper.calibration.metrics import (
    StrategyMetrics,
    aggregate,
    concentration,
    rank,
    segment_winners,
    totals,
)
from web_scraper.calibration.promote import apply_promotion, plan_promotion
from web_scraper.calibration.report import CalibrationReport
from web_scraper.calibration.store import CalibrationStore

__all__ = [
    "EXAMPLE_CORPUS",
    "AttemptOutcome",
    "CalibrationHarness",
    "CalibrationReport",
    "CalibrationStore",
    "Corpus",
    "CorpusTarget",
    "PlannedCall",
    "SpendCaps",
    "StrategyMetrics",
    "TargetKind",
    "aggregate",
    "apply_promotion",
    "concentration",
    "load_corpus",
    "outcome_from_dict",
    "plan_promotion",
    "rank",
    "segment_winners",
    "totals",
]
