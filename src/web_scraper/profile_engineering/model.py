"""The lifecycle a Site Profile has to walk, and the package that carries it.

A Site Profile is not configuration. It is a claim about how a website behaves,
and the whole point of this module is that a claim has to be *earned* before
anything routes traffic through it. So a profile has a state, the states form a
graph, and the graph has exactly one edge into ``CERTIFIED`` — the one that runs
the deterministic checks.

.. code-block:: text

    DRAFT -> PROBING -> VALIDATING -> CERTIFIED
                                        |
                                   regression
                                        v
                                     DEGRADED -> QUARANTINED
                                        ^            |
                                        +-- repair --+

What is deliberately absent is a way for a model — or a person in a hurry — to
declare a profile good. "It looks right" is how a profile that extracts nothing
runs for a month before anyone notices, because a scraper that returns empty
fields does not crash; it returns empty fields, quietly, at scale.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from web_scraper.observability.manifest import stable_hash

#: Bumped when the on-disk shape of a profile package changes incompatibly.
#: A reader that finds a version it does not know refuses the package rather
#: than guessing which half of it still means what it used to.
PROFILE_SCHEMA_VERSION = 1

#: Files that make a directory a profile package.
PROFILE_FILE = "profile.yaml"
CORPUS_FILE = "corpus.yaml"
EVIDENCE_FILE = "evidence.json"
README_FILE = "README.md"


class ProfileState(StrEnum):
    """Where a profile is in its life."""

    #: Written, never run against the site.
    DRAFT = "DRAFT"
    #: Being investigated: probes, discovery, content-kind detection.
    PROBING = "PROBING"
    #: Runs against the acceptance corpus, not yet passing every check.
    VALIDATING = "VALIDATING"
    #: Passed the deterministic certification. The only state that may be
    #: activated for production traffic.
    CERTIFIED = "CERTIFIED"
    #: Was certified; production evidence says it is getting worse.
    DEGRADED = "DEGRADED"
    #: Stopped. Something is wrong badly enough that running it would produce
    #: bad data rather than no data, which is the more expensive failure.
    QUARANTINED = "QUARANTINED"

    @property
    def is_production_ready(self) -> bool:
        return self is ProfileState.CERTIFIED

    @property
    def needs_attention(self) -> bool:
        return self in {ProfileState.DEGRADED, ProfileState.QUARANTINED}


#: The graph. Every transition a profile may make, and nothing else.
#:
#: ``CERTIFIED`` is reachable from exactly three places and each of them runs
#: the certification checks first. There is no edge that means "we decided it
#: was fine".
ALLOWED_TRANSITIONS: Mapping[ProfileState, frozenset[ProfileState]] = {
    ProfileState.DRAFT: frozenset({ProfileState.PROBING, ProfileState.QUARANTINED}),
    ProfileState.PROBING: frozenset(
        {ProfileState.VALIDATING, ProfileState.DRAFT, ProfileState.QUARANTINED}
    ),
    ProfileState.VALIDATING: frozenset(
        {ProfileState.CERTIFIED, ProfileState.PROBING, ProfileState.QUARANTINED}
    ),
    ProfileState.CERTIFIED: frozenset(
        {ProfileState.DEGRADED, ProfileState.VALIDATING, ProfileState.QUARANTINED}
    ),
    ProfileState.DEGRADED: frozenset(
        {ProfileState.CERTIFIED, ProfileState.QUARANTINED, ProfileState.VALIDATING}
    ),
    ProfileState.QUARANTINED: frozenset({ProfileState.VALIDATING, ProfileState.DRAFT}),
}

#: Transitions that may only happen as the *result* of certification. Anything
#: else asking for them is refused, which is what stops "looks good" from being
#: a state change.
CERTIFICATION_ONLY = frozenset({ProfileState.CERTIFIED})


class LifecycleError(RuntimeError):
    """An illegal transition, named rather than silently ignored."""


def transition(
    current: ProfileState, target: ProfileState, *, certified_by_checks: bool = False
) -> ProfileState:
    """Move a profile, or refuse and say why.

    ``certified_by_checks`` is not a courtesy flag. Only
    :func:`~web_scraper.profile_engineering.certification.certify` sets it, and
    without it the edge into CERTIFIED does not exist at all.
    """

    if target is current:
        return current
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        legal = ", ".join(sorted(s.value for s in allowed)) or "nothing"
        raise LifecycleError(
            f"{current.value} -> {target.value} is not a legal transition; "
            f"from {current.value} a profile may go to: {legal}"
        )
    if target in CERTIFICATION_ONLY and not certified_by_checks:
        raise LifecycleError(
            f"{target.value} is only reachable by passing certification. "
            "Run the checks; a judgement that it looks correct is not one of them."
        )
    return target


@dataclass(frozen=True)
class ProfileIdentity:
    """What a package is, independent of what it currently contains."""

    domain: str
    profile_version: int = 1
    profile_schema_version: int = PROFILE_SCHEMA_VERSION
    state: ProfileState = ProfileState.DRAFT
    created_at: str = ""
    last_verified_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "profile_version": self.profile_version,
            "profile_schema_version": self.profile_schema_version,
            "status": self.state.value,
            "created_at": self.created_at,
            "last_verified_at": self.last_verified_at,
        }


@dataclass
class ProfilePackage:
    """One site's directory: the profile, its corpus, and what was measured.

    The package is the unit that gets certified, versioned and rolled back —
    never the YAML on its own. A profile without the corpus it passed is a claim
    with the evidence removed.
    """

    root: Path
    identity: ProfileIdentity

    @property
    def profile_path(self) -> Path:
        return self.root / PROFILE_FILE

    @property
    def corpus_path(self) -> Path:
        return self.root / CORPUS_FILE

    @property
    def evidence_path(self) -> Path:
        return self.root / EVIDENCE_FILE

    @property
    def readme_path(self) -> Path:
        return self.root / README_FILE

    @property
    def fixtures_dir(self) -> Path:
        return self.root / "fixtures"

    @property
    def history_dir(self) -> Path:
        return self.root / "history"

    def exists(self) -> bool:
        return self.profile_path.exists()

    def missing_files(self) -> list[str]:
        """Which of the four required files are absent."""

        return [
            name
            for name, path in (
                (PROFILE_FILE, self.profile_path),
                (CORPUS_FILE, self.corpus_path),
                (EVIDENCE_FILE, self.evidence_path),
                (README_FILE, self.readme_path),
            )
            if not path.exists()
        ]

    def profile_hash(self) -> str | None:
        """Identity of the profile's *content*, for LKG comparison."""

        if not self.profile_path.exists():
            return None
        return stable_hash(self.profile_path.read_text(encoding="utf-8"))

    def load_evidence(self) -> dict[str, Any]:
        if not self.evidence_path.exists():
            return {}
        payload = json.loads(self.evidence_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    def write_evidence(self, payload: Mapping[str, Any]) -> Path:
        self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_path.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return self.evidence_path


@dataclass(frozen=True)
class LastKnownGood:
    """The profile version that is currently trusted with production traffic.

    Stored as identity plus hashes, not as a copy of the file: git already keeps
    every version of the YAML, and a second copy that can drift from it is worse
    than no copy. What git cannot answer is *which* version was the one that
    passed, and that is exactly what this records.
    """

    profile_version: int
    profile_hash: str
    certified_at: str
    evidence_hash: str
    verdict: str = ""
    warnings: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_version": self.profile_version,
            "profile_hash": self.profile_hash,
            "certified_at": self.certified_at,
            "evidence_hash": self.evidence_hash,
            "verdict": self.verdict,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> LastKnownGood:
        return cls(
            profile_version=int(payload.get("profile_version", 0)),
            profile_hash=str(payload.get("profile_hash", "")),
            certified_at=str(payload.get("certified_at", "")),
            evidence_hash=str(payload.get("evidence_hash", "")),
            verdict=str(payload.get("verdict", "")),
            warnings=int(payload.get("warnings", 0)),
        )


def utc_now() -> str:
    """One spelling of 'now', so timestamps compare as strings."""

    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ProfileReport:
    """What a human reads after any lifecycle action."""

    domain: str
    state: ProfileState
    lines: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> str:
        return "\n".join((f"{self.domain}: {self.state.value}", *self.lines))
