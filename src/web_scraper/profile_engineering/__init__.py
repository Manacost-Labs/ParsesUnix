"""Making a Site Profile something that has to be earned rather than written.

A profile is a claim about how a website behaves. This package is the machinery
that turns a claim into a contract: a lifecycle a profile has to walk, an
acceptance corpus it has to survive, deterministic checks it has to pass, and a
record of what was actually measured. Nothing here can decide a profile is good;
the only path to CERTIFIED runs the checks.
"""

from web_scraper.profile_engineering.acceptance import run_case, run_corpus
from web_scraper.profile_engineering.builder import (
    DiscoveredRoute,
    ObservedPage,
    ProfileDraft,
    build_draft,
)
from web_scraper.profile_engineering.certification import (
    ApiRouteEvidence,
    CaseOutcome,
    CertificationReport,
    Check,
    MutationOutcome,
    Severity,
    Verdict,
    certify,
)
from web_scraper.profile_engineering.corpus import (
    AcceptanceCorpus,
    CaseKind,
    CorpusCase,
    NotApplicable,
    load_corpus,
)
from web_scraper.profile_engineering.fragility import (
    ExtractorJudgement,
    Reliability,
    judge_extractor,
    judge_json_path,
    judge_selector,
)
from web_scraper.profile_engineering.health import (
    HealthReport,
    HealthState,
    HealthThresholds,
    RunSample,
    assess_health,
)
from web_scraper.profile_engineering.model import (
    PROFILE_SCHEMA_VERSION,
    LastKnownGood,
    LifecycleError,
    ProfileIdentity,
    ProfilePackage,
    ProfileState,
    transition,
)
from web_scraper.profile_engineering.mutation import (
    Expectation,
    Mutation,
    MutationKind,
    default_mutations,
    mutate,
    run_mutations,
)
from web_scraper.profile_engineering.registry import ProfileRegistry, RegistryEntry
from web_scraper.profile_engineering.repair import (
    BrokenField,
    RepairCandidate,
    RepairKind,
    may_replace_last_known_good,
    propose_repairs,
)

__all__ = [
    "PROFILE_SCHEMA_VERSION",
    "AcceptanceCorpus",
    "ApiRouteEvidence",
    "BrokenField",
    "CaseKind",
    "CaseOutcome",
    "CertificationReport",
    "Check",
    "CorpusCase",
    "DiscoveredRoute",
    "Expectation",
    "ExtractorJudgement",
    "HealthReport",
    "HealthState",
    "HealthThresholds",
    "LastKnownGood",
    "LifecycleError",
    "Mutation",
    "MutationKind",
    "MutationOutcome",
    "NotApplicable",
    "ObservedPage",
    "ProfileDraft",
    "ProfileIdentity",
    "ProfilePackage",
    "ProfileRegistry",
    "ProfileState",
    "RegistryEntry",
    "Reliability",
    "RepairCandidate",
    "RepairKind",
    "RunSample",
    "Severity",
    "Verdict",
    "assess_health",
    "build_draft",
    "certify",
    "default_mutations",
    "judge_extractor",
    "judge_json_path",
    "judge_selector",
    "load_corpus",
    "may_replace_last_known_good",
    "mutate",
    "propose_repairs",
    "run_case",
    "run_corpus",
    "run_mutations",
    "transition",
]
