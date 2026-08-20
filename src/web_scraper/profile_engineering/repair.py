"""Proposing a fix for a degraded profile — and never installing it.

When a profile degrades, the tempting move is to find the selector that stopped
matching and write a new one. It is tempting because it works, once, and it is
usually the wrong move twice over: the replacement is often *more* fragile than
what it replaced (a longer chain, a deeper nesting, one more assumption about
today's layout), and the reason the first one broke is that the site is being
redesigned — which will happen again next quarter.

So repair does two things differently.

**It prefers a sturdier source over a working one.** If the DOM path broke and
discovery has a validated JSON endpoint carrying the same field, the proposal is
to move the field to the endpoint, not to write a better CSS selector. A
migration to structure is the only repair that makes the *next* redesign
cheaper.

**It never activates.** The output is a candidate version. It goes through the
same corpus, the same mutations and the same certification as any other profile,
and it replaces the last known good version only if it is at least as good — a
repair that certifies worse than what it replaces is a regression with a
reassuring name.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from web_scraper.profile_engineering.certification import (
    CertificationReport,
    Verdict,
)
from web_scraper.profile_engineering.fragility import Reliability, judge_extractor
from web_scraper.profile_engineering.model import LastKnownGood


class RepairKind(StrEnum):
    """What a proposal actually changes."""

    #: The best outcome: a field moves from markup to a structure the site
    #: publishes on purpose.
    MIGRATE_TO_STRUCTURED_ROUTE = "migrate_to_structured_route"
    #: A second source is added so the field survives the next change.
    ADD_QUORUM_SOURCE = "add_quorum_source"
    #: Same source, new locator. Accepted only when nothing sturdier exists.
    REPLACE_SELECTOR = "replace_selector"
    #: The field is no longer on the page at all. Somebody has to decide whether
    #: the dataset still means what it used to.
    DEMOTE_FIELD = "demote_field"
    #: Nothing can be proposed from the evidence available.
    NEEDS_INVESTIGATION = "needs_investigation"


@dataclass(frozen=True)
class RepairProposal:
    """One suggested change, with the reason and what it costs in fragility."""

    kind: RepairKind
    url_class: str
    field: str
    detail: str
    from_source: str = ""
    to_source: str = ""
    reliability_before: str = ""
    reliability_after: str = ""

    @property
    def improves_reliability(self) -> bool:
        order = {
            r.value: i
            for i, r in enumerate((Reliability.FRAGILE, Reliability.MEDIUM, Reliability.STABLE))
        }
        return order.get(self.reliability_after, -1) > order.get(self.reliability_before, -1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "url_class": self.url_class,
            "field": self.field,
            "detail": self.detail,
            "from": self.from_source,
            "to": self.to_source,
            "reliability_before": self.reliability_before,
            "reliability_after": self.reliability_after,
            "improves_reliability": self.improves_reliability,
        }


@dataclass
class RepairCandidate:
    """A proposed next version of a profile. Not a profile in use."""

    domain: str
    base_version: int
    proposals: list[RepairProposal] = field(default_factory=list)
    #: Set once the candidate has been through the corpus and the mutations.
    certification: CertificationReport | None = None

    @property
    def candidate_version(self) -> int:
        return self.base_version + 1

    @property
    def is_activatable(self) -> bool:
        """Never true on the strength of the proposals alone."""

        return self.certification is not None and self.certification.verdict.may_activate

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "base_version": self.base_version,
            "candidate_version": self.candidate_version,
            "proposals": [p.to_dict() for p in self.proposals],
            "certification": (None if self.certification is None else self.certification.to_dict()),
            "activatable": self.is_activatable,
        }

    def describe(self) -> str:
        lines = [
            f"{self.domain}: repair candidate v{self.candidate_version} "
            f"(from v{self.base_version})",
            "",
        ]
        for proposal in self.proposals:
            arrow = f"{proposal.from_source} -> {proposal.to_source}" if proposal.to_source else ""
            lines.append(
                f"  [{proposal.url_class}] {proposal.field}: {proposal.kind.value}"
                + (f"  {arrow}" if arrow else "")
            )
            lines.append(f"      {proposal.detail}")
        if self.certification is None:
            lines += ["", "Not certified. This candidate is a proposal, not a profile in use."]
        else:
            lines += ["", f"certification: {self.certification.verdict.value}"]
            if not self.is_activatable:
                lines.append("The last known good version stays active.")
        return "\n".join(lines)


@dataclass(frozen=True)
class BrokenField:
    """A field that stopped arriving, and what supplied it before."""

    url_class: str
    name: str
    importance: str
    current_kind: str
    current_locator: str
    availability: float


def propose_repairs(
    domain: str,
    base_version: int,
    broken: Sequence[BrokenField],
    *,
    validated_routes: Sequence[Mapping[str, Any]] = (),
) -> RepairCandidate:
    """Turn observed breakage into proposals, sturdiest option first.

    ``validated_routes`` is what discovery has already proved — endpoints with a
    stable schema seen across several distinct pages. It is consulted *before*
    anything else, because a field that can move to a validated endpoint should
    move there rather than get a cleverer selector.
    """

    candidate = RepairCandidate(domain=domain, base_version=base_version)

    for item in broken:
        before = judge_extractor(
            field_name=item.name,
            kind=item.current_kind,
            locator=item.current_locator,
            importance=item.importance,
        ).judgement.reliability.value

        route = _route_supplying(validated_routes, item.name)
        if route is not None:
            candidate.proposals.append(
                RepairProposal(
                    kind=RepairKind.MIGRATE_TO_STRUCTURED_ROUTE,
                    url_class=item.url_class,
                    field=item.name,
                    from_source=f"{item.current_kind}:{item.current_locator}",
                    to_source=f"json:{route.get('id', 'discovered-endpoint')}",
                    detail=(
                        f"{route.get('id')} is a validated endpoint carrying this field on "
                        f"{route.get('distinct_pages')} distinct pages. Moving there survives "
                        "the next redesign; a new selector only survives this one."
                    ),
                    reliability_before=before,
                    reliability_after=Reliability.STABLE.value,
                )
            )
            continue

        if item.availability > 0.0:
            candidate.proposals.append(
                RepairProposal(
                    kind=RepairKind.ADD_QUORUM_SOURCE,
                    url_class=item.url_class,
                    field=item.name,
                    from_source=f"{item.current_kind}:{item.current_locator}",
                    detail=(
                        f"the field still arrives on {item.availability:.0%} of pages, so the "
                        "page changed shape rather than dropping it. A second source would "
                        "have kept the field and made this visible on the first page instead "
                        "of the hundredth."
                    ),
                    reliability_before=before,
                )
            )
            continue

        candidate.proposals.append(
            RepairProposal(
                kind=RepairKind.NEEDS_INVESTIGATION,
                url_class=item.url_class,
                field=item.name,
                from_source=f"{item.current_kind}:{item.current_locator}",
                detail=(
                    "the field is absent from every sampled page and no validated route "
                    "supplies it. Whether the site removed it or renamed it is a question "
                    "about the site, not one a patch can answer."
                ),
                reliability_before=before,
            )
        )
    return candidate


def _route_supplying(
    routes: Sequence[Mapping[str, Any]], field_name: str
) -> Mapping[str, Any] | None:
    for route in routes:
        if route.get("state") != "VALIDATED":
            continue
        fields = route.get("fields") or route.get("critical_fields_found") or ()
        if field_name in fields:
            return route
    return None


def may_replace_last_known_good(
    candidate: CertificationReport | None,
    incumbent: LastKnownGood | None,
) -> tuple[bool, str]:
    """Should this candidate take over from what is currently trusted?

    The comparison is against the *incumbent's* certification, not against a
    fixed bar. A profile that certifies with three new warnings is worse than
    the one it would replace, even though both are technically certified — and
    "technically certified" is how a regression ships.
    """

    if candidate is None:
        return False, "the candidate has not been certified"
    if not candidate.verdict.may_activate:
        return False, f"the candidate is {candidate.verdict.value}"
    if incumbent is None:
        return True, "nothing is currently trusted; the candidate becomes the baseline"

    if candidate.verdict is Verdict.CERTIFIED_WITH_WARNINGS and incumbent.verdict == (
        Verdict.CERTIFIED.value
    ):
        return False, (
            "the incumbent certified cleanly and the candidate only certifies with "
            "warnings; replacing it would be a regression with a reassuring name"
        )
    if len(candidate.warnings) > incumbent.warnings:
        return False, (
            f"the candidate has {len(candidate.warnings)} warning(s) against the "
            f"incumbent's {incumbent.warnings}"
        )
    return True, (
        f"the candidate certifies {candidate.verdict.value} with "
        f"{len(candidate.warnings)} warning(s), against the incumbent's {incumbent.warnings}"
    )
