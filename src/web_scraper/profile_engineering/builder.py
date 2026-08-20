"""Assembling a first draft from evidence, and refusing to pretend it is more.

The output of this module is a DRAFT. Not a profile, not a recommendation — a
starting point built from what a probe actually saw, with every gap left visibly
empty so the next step is obvious to whoever reads it.

Two refusals shape it:

**No invented selectors.** A field with no evidence behind it is written as a
TODO, not as a plausible-looking CSS path. A guessed selector is worse than an
empty one, because an empty one gets filled in and a guessed one gets trusted.

**No certification, ever, from here.** A builder that could emit a certified
profile would make certification a formality. The only route to CERTIFIED runs
the checks.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from web_scraper.contracts import ContentKind, FieldImportance
from web_scraper.profile_engineering.model import (
    PROFILE_SCHEMA_VERSION,
    ProfileState,
    utc_now,
)

#: Written into every draft where a human decision is required. Deliberately
#: something a linter, a reviewer and a search can all find.
TODO = "TODO"


@dataclass(frozen=True)
class ObservedPage:
    """What a probe learned about one URL. The only input this module trusts."""

    url: str
    content_kind: ContentKind
    status: int
    body_bytes: int
    #: Extractor kinds that produced something on this page, best first.
    available_sources: tuple[str, ...] = ()
    #: Strings unique enough to prove the page rendered.
    canary_candidates: tuple[str, ...] = ()
    requires_javascript: bool = False


@dataclass(frozen=True)
class DiscoveredRoute:
    """A structured endpoint discovery has already judged."""

    id: str
    url: str
    method: str = "GET"
    state: str = "PROMISING"
    distinct_pages: int = 0
    fields: tuple[str, ...] = ()

    @property
    def is_validated(self) -> bool:
        return self.state == "VALIDATED"


@dataclass
class ProfileDraft:
    """A profile in progress, plus the questions it cannot answer itself."""

    domain: str
    url_classes: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "profile_schema_version": PROFILE_SCHEMA_VERSION,
            "profile_version": 1,
            "site": self.domain,
            "status": ProfileState.DRAFT.value,
            "created_at": self.evidence.get("created_at", ""),
            "authorization": {"public_data_only": True},
            "url_classes": self.url_classes,
        }

    def render(self) -> str:
        """YAML with the open questions at the top, where they cannot be missed."""

        header = [
            f"# DRAFT profile for {self.domain}. Not certified, not usable in production.",
            "# Every TODO below is a decision this tool refused to guess.",
        ]
        header += [f"#   - {q}" for q in self.open_questions]
        body = json.dumps(self.to_mapping(), indent=2, sort_keys=False)
        # JSON is valid YAML, and emitting it avoids a hand-rolled serialiser
        # producing something subtly different from what the loader expects.
        return "\n".join(header) + "\n" + body + "\n"


def infer_url_class(url: str) -> str:
    """A name for the kind of page this is, from its path.

    Path-shape only. Guessing semantics from a URL is how one extractor ends up
    applied to a whole domain — the exact failure a URL class exists to prevent.
    """

    path = urlsplit(url).path.strip("/")
    if not path:
        return "home"
    segments = [s for s in path.split("/") if s]
    named = [s for s in segments if not re.fullmatch(r"[\d\-_.]+", s)]
    if not named:
        return "entity"
    candidate = named[0].lower()
    if len(segments) > len(named):
        # /rankings/2015 — a collection with an identifier under it.
        return re.sub(r"[^a-z0-9]+", "_", candidate)
    if len(named) > 1:
        return re.sub(r"[^a-z0-9]+", "_", named[-1].lower())
    return re.sub(r"[^a-z0-9]+", "_", candidate)


def _match_pattern(domain: str, url: str) -> str:
    path = urlsplit(url).path
    prefix = "/".join(path.split("/")[:2]) or "/"
    return rf"^https://{re.escape(domain)}{re.escape(prefix)}"


def build_draft(
    domain: str,
    pages: Sequence[ObservedPage],
    *,
    wanted_fields: Sequence[str] = (),
    critical_fields: Sequence[str] = (),
    routes: Sequence[DiscoveredRoute] = (),
) -> ProfileDraft:
    """Turn probe output into a draft, marking everything unproven as unproven."""

    draft = ProfileDraft(domain=domain)
    draft.evidence = {"created_at": utc_now(), "pages_observed": len(pages)}
    if not pages:
        draft.open_questions.append(
            "no page was probed: a profile built without observing the site is a guess"
        )
        return draft

    grouped: dict[str, list[ObservedPage]] = {}
    for page in pages:
        grouped.setdefault(infer_url_class(page.url), []).append(page)

    validated = [r for r in routes if r.is_validated]
    promising = [r for r in routes if not r.is_validated]
    for route in promising:
        draft.open_questions.append(
            f"{route.id} was observed on {route.distinct_pages} page(s) and is not validated; "
            "it may not be a route yet"
        )

    for name, class_pages in sorted(grouped.items()):
        sample = class_pages[0]
        chosen: DiscoveredRoute | None = _route_for(validated, wanted_fields)
        draft.url_classes[name] = _class_draft(
            domain=domain,
            name=name,
            sample=sample,
            pages=class_pages,
            wanted_fields=wanted_fields,
            critical_fields=critical_fields,
            route=chosen,
            questions=draft.open_questions,
        )
    return draft


def _route_for(
    validated: Sequence[DiscoveredRoute], wanted: Sequence[str]
) -> DiscoveredRoute | None:
    """Prefer a validated endpoint that actually carries the fields we want."""

    if not validated:
        return None
    if not wanted:
        return validated[0]
    scored = sorted(
        validated,
        key=lambda r: (-len(set(wanted) & set(r.fields)), -r.distinct_pages),
    )
    best = scored[0]
    return best if set(wanted) & set(best.fields) else None


def _class_draft(
    *,
    domain: str,
    name: str,
    sample: ObservedPage,
    pages: Sequence[ObservedPage],
    wanted_fields: Sequence[str],
    critical_fields: Sequence[str],
    route: DiscoveredRoute | None,
    questions: list[str],
) -> dict[str, Any]:
    is_json = sample.content_kind is ContentKind.JSON or (route is not None)

    if route is not None:
        primary: dict[str, Any] = {
            "type": "json_api",
            "level": "L0",
            "url": route.url,
            "method": route.method,
        }
    elif sample.requires_javascript:
        primary = {"type": "dynamic_render", "level": "L2"}
        questions.append(
            f"{name}: the page needs JavaScript. Check DiscoveryStore for an endpoint "
            "before settling for a browser — rendering is the expensive answer."
        )
    else:
        primary = {"type": "direct_http", "level": "L1"}

    canary = sample.canary_candidates[0] if sample.canary_candidates else ""
    if not canary and not is_json:
        questions.append(
            f"{name}: no canary. Without one, triage accepts any page over the size "
            "limit — including the site's error page."
        )

    fields: dict[str, Any] = {}
    for field_name in wanted_fields:
        importance = (
            FieldImportance.CRITICAL.value
            if field_name in critical_fields
            else FieldImportance.IMPORTANT.value
        )
        fields[field_name] = {"importance": importance}

    extractors: list[dict[str, Any]] = []
    if is_json:
        mapping = {f: f"{TODO}.{f}" for f in wanted_fields}
        extractors.append({"kind": "json", "fields": mapping})
        if wanted_fields:
            questions.append(
                f"{name}: JSON paths are placeholders. Read one real response and "
                "replace every TODO before this is testable."
            )
    else:
        for source in sample.available_sources or ("json_ld", "heuristic"):
            extractors.append({"kind": source})
        if "json_ld" not in (sample.available_sources or ()):
            questions.append(
                f"{name}: no JSON-LD or app state was found, so extraction rests on the "
                "DOM. Expect this to need repair after the site's next redesign."
            )

    validation: dict[str, Any] = {
        "min_body_bytes": max(500, sample.body_bytes // 4),
    }
    if canary:
        validation["canary"] = canary
    if is_json:
        validation["required_json_paths"] = [f"{TODO}"]
    if fields:
        validation["fields"] = fields

    draft: dict[str, Any] = {
        "match": _match_pattern(domain, sample.url),
        "expected_content_type": "json" if is_json else "html",
        "validation": validation,
        "routes": {"primary": primary},
        "extractors": extractors,
        "observed": {
            "pages": len(pages),
            "content_kind": sample.content_kind.value,
            "requires_javascript": sample.requires_javascript,
        },
    }
    if len(wanted_fields) > 1:
        draft["quorum_fields"] = list(critical_fields or wanted_fields[:1])
    return draft


def evidence_from_draft(draft: ProfileDraft, *, pages: Sequence[ObservedPage]) -> dict[str, Any]:
    """The evidence file for a draft: what was seen, and nothing that was hoped.

    Identifiers are hashed rather than stored. An evidence file gets committed
    and shared, and a list of URLs is a description of what somebody crawls.
    """

    from web_scraper.observability.manifest import stable_hash

    return {
        "profile_version": 1,
        "state": ProfileState.DRAFT.value,
        "generated_at": utc_now(),
        "tested_pages": len(pages),
        "distinct_entities": len({stable_hash(p.url) for p in pages}),
        "url_classes": {
            name: {"cases": 0, "critical_fields": {}} for name in sorted(draft.url_classes)
        },
        "page_hashes": sorted({stable_hash(p.url) for p in pages}),
        "open_questions": list(draft.open_questions),
        "note": (
            "A draft's evidence is a record of what was observed, not of what was "
            "proven. Nothing here supports certification."
        ),
    }


def readme_for(domain: str, draft: ProfileDraft) -> str:
    """The package README, written from the draft rather than from a template."""

    classes = ", ".join(sorted(draft.url_classes)) or "none yet"
    lines = [
        f"# {domain}",
        "",
        "**State:** DRAFT — not certified, not usable in production.",
        "",
        f"**URL classes:** {classes}",
        "",
        "## Data collected",
        "",
        "Fields are declared in `profile.yaml` with an importance. A missing "
        "critical field fails a record; a missing optional one is information.",
        "",
        "## Routes",
        "",
    ]
    for name, spec in sorted(draft.url_classes.items()):
        route = spec.get("routes", {}).get("primary", {})
        lines.append(f"- `{name}`: {route.get('type')} at {route.get('level')}")
    lines += [
        "",
        "## Known limitations",
        "",
    ]
    lines += [f"- {q}" for q in draft.open_questions] or ["- none recorded"]
    lines += [
        "",
        "## Last certification",
        "",
        "Never certified.",
        "",
    ]
    return "\n".join(lines)
