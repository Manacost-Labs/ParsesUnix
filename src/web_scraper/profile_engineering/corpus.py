"""The acceptance corpus: the pages a profile has to survive before anyone trusts it.

A profile tested on one page proves that one page. The failures that actually
cost data are the ones a single happy sample cannot show: the second entity
whose layout differs, the empty result that extracts nothing without erroring,
the 404 that returns a styled page with a title, the last page of a listing that
looks exactly like the first.

So the corpus is a required part of the package, and certification refuses to
proceed without a *negative* case. A suite where every case is expected to
succeed cannot distinguish a working profile from one that says yes to
everything — and the second kind is the one that quietly fills a dataset with
the site's error page.

Cases that do not apply are declared, not omitted: a URL class with no
pagination should say so once, in writing, rather than leave a reader wondering
whether pagination was tested or forgotten.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class CaseKind(StrEnum):
    """What a corpus case is there to prove."""

    NORMAL = "normal"
    DIFFERENT_ENTITY = "different_entity"
    NEW_PAGE = "new_page"
    OLD_PAGE = "old_page"
    PAGINATION = "pagination"
    EMPTY_RESULT = "empty_result"
    NOT_FOUND = "not_found"
    REDIRECT = "redirect"
    LARGE_RESPONSE = "large_response"
    LAYOUT_VARIANT = "layout_variant"
    CSR_VARIANT = "csr_variant"

    @property
    def is_negative(self) -> bool:
        """Does this case prove the profile can say *no*?

        The distinction certification hangs on. A corpus of happy paths cannot
        tell a working profile from one that accepts anything.
        """

        return self in {
            CaseKind.EMPTY_RESULT,
            CaseKind.NOT_FOUND,
            CaseKind.LAYOUT_VARIANT,
        }


class Applicability(StrEnum):
    """Whether a case kind means anything for this URL class."""

    REQUIRED = "REQUIRED"
    COVERED = "COVERED"
    MISSING = "MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class CorpusCase:
    """One page, and what the profile is expected to say about it."""

    id: str
    url_class: str
    kind: CaseKind
    #: A recorded response under the package's ``fixtures/``. Live URLs are
    #: allowed but never required: PR CI must not depend on somebody else's
    #: uptime, and a fixture is the only way a failure is reproducible a month
    #: later.
    fixture: str = ""
    url: str = ""
    expect_verdict: str = "OK"
    expect_fields: tuple[str, ...] = ()
    expect_absent_fields: tuple[str, ...] = ()
    expect_min_records: int | None = None
    notes: str = ""

    @property
    def is_negative(self) -> bool:
        return self.kind.is_negative or self.expect_verdict != "OK"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "url_class": self.url_class,
            "kind": self.kind.value,
            "expect_verdict": self.expect_verdict,
        }
        if self.fixture:
            payload["fixture"] = self.fixture
        if self.url:
            payload["url"] = self.url
        if self.expect_fields:
            payload["expect_fields"] = list(self.expect_fields)
        if self.expect_absent_fields:
            payload["expect_absent_fields"] = list(self.expect_absent_fields)
        if self.expect_min_records is not None:
            payload["expect_min_records"] = self.expect_min_records
        if self.notes:
            payload["notes"] = self.notes
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CorpusCase:
        return cls(
            id=str(payload.get("id", "")),
            url_class=str(payload.get("url_class", "")),
            kind=CaseKind(str(payload.get("kind", CaseKind.NORMAL.value))),
            fixture=str(payload.get("fixture", "")),
            url=str(payload.get("url", "")),
            expect_verdict=str(payload.get("expect_verdict", "OK")),
            expect_fields=tuple(str(f) for f in payload.get("expect_fields", ())),
            expect_absent_fields=tuple(str(f) for f in payload.get("expect_absent_fields", ())),
            expect_min_records=(
                int(payload["expect_min_records"])
                if payload.get("expect_min_records") is not None
                else None
            ),
            notes=str(payload.get("notes", "")),
        )


@dataclass(frozen=True)
class NotApplicable:
    """A case kind that means nothing here, and the reason.

    Written down rather than left out. "There is no pagination on this class"
    and "nobody tested pagination" look identical in a corpus that simply omits
    it, and only one of them is fine.
    """

    url_class: str
    kind: CaseKind
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"url_class": self.url_class, "kind": self.kind.value, "reason": self.reason}


@dataclass
class AcceptanceCorpus:
    """Everything a profile must survive, per URL class."""

    domain: str
    cases: tuple[CorpusCase, ...] = ()
    not_applicable: tuple[NotApplicable, ...] = ()

    def for_class(self, url_class: str) -> tuple[CorpusCase, ...]:
        return tuple(c for c in self.cases if c.url_class == url_class)

    def negative_cases(self, url_class: str | None = None) -> tuple[CorpusCase, ...]:
        cases = self.cases if url_class is None else self.for_class(url_class)
        return tuple(c for c in cases if c.is_negative)

    def kinds_for(self, url_class: str) -> set[CaseKind]:
        return {c.kind for c in self.for_class(url_class)}

    def excused(self, url_class: str, kind: CaseKind) -> NotApplicable | None:
        return next(
            (n for n in self.not_applicable if n.url_class == url_class and n.kind is kind),
            None,
        )

    def coverage(self, url_class: str, *, expected: Iterable[CaseKind]) -> dict[str, str]:
        """What each expected kind is: covered, excused, or simply missing."""

        present = self.kinds_for(url_class)
        out: dict[str, str] = {}
        for kind in expected:
            if kind in present:
                out[kind.value] = Applicability.COVERED.value
            elif self.excused(url_class, kind) is not None:
                out[kind.value] = Applicability.NOT_APPLICABLE.value
            else:
                out[kind.value] = Applicability.MISSING.value
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "cases": [c.to_dict() for c in self.cases],
            "not_applicable": [n.to_dict() for n in self.not_applicable],
        }

    def render(self) -> str:
        """The corpus as YAML, written without needing a YAML library."""

        lines = [
            "# Acceptance corpus: what this profile has to survive.",
            "# A suite of happy paths cannot tell a working profile from one",
            "# that says yes to everything, so at least one negative case is",
            "# required before certification.",
            f"domain: {self.domain}",
            "cases:",
        ]
        for case in self.cases:
            lines.append(f"  - id: {case.id}")
            for key, value in case.to_dict().items():
                if key == "id":
                    continue
                if isinstance(value, list):
                    rendered = "[" + ", ".join(json.dumps(v) for v in value) + "]"
                    lines.append(f"    {key}: {rendered}")
                elif isinstance(value, str):
                    lines.append(f"    {key}: {json.dumps(value)}")
                else:
                    lines.append(f"    {key}: {value}")
        if self.not_applicable:
            lines.append("not_applicable:")
            for item in self.not_applicable:
                lines.append(f"  - url_class: {item.url_class}")
                lines.append(f"    kind: {item.kind.value}")
                lines.append(f"    reason: {json.dumps(item.reason)}")
        return "\n".join(lines) + "\n"


def corpus_from_mapping(payload: Mapping[str, Any]) -> AcceptanceCorpus:
    raw_cases = payload.get("cases") or []
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, str | bytes):
        raise ValueError("corpus needs a 'cases' list")
    raw_na = payload.get("not_applicable") or []
    return AcceptanceCorpus(
        domain=str(payload.get("domain", "")),
        cases=tuple(CorpusCase.from_dict(c) for c in raw_cases if isinstance(c, Mapping)),
        not_applicable=tuple(
            NotApplicable(
                url_class=str(n.get("url_class", "")),
                kind=CaseKind(str(n.get("kind", CaseKind.NORMAL.value))),
                reason=str(n.get("reason", "")),
            )
            for n in raw_na
            if isinstance(n, Mapping)
        ),
    )


def load_corpus(path: str | Path) -> AcceptanceCorpus:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml

            payload = yaml.safe_load(text)
        except ImportError:
            from web_scraper.profiles.yamlish import loads

            payload = loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{file} does not contain an acceptance corpus")
    return corpus_from_mapping(payload)


#: What a well-covered URL class looks like. Not a checklist to satisfy —
#: several of these will be NOT_APPLICABLE on any given class, and saying so is
#: a complete answer.
EXPECTED_KINDS: tuple[CaseKind, ...] = (
    CaseKind.NORMAL,
    CaseKind.DIFFERENT_ENTITY,
    CaseKind.EMPTY_RESULT,
    CaseKind.NOT_FOUND,
    CaseKind.PAGINATION,
    CaseKind.LAYOUT_VARIANT,
)


@dataclass
class CorpusDraft:
    """A corpus assembled from evidence the project already has.

    Sources, in the order they are trusted: what a probe actually fetched, what
    discovery validated, what the queue has seen. Generation stops at what is on
    hand — it will not go and fetch a thousand URLs to fill a table, because a
    corpus is meant to be a small set of pages somebody chose, not a crawl.
    """

    domain: str
    cases: list[CorpusCase] = field(default_factory=list)
    not_applicable: list[NotApplicable] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def add(self, case: CorpusCase, *, source: str) -> None:
        if any(existing.id == case.id for existing in self.cases):
            return
        self.cases.append(case)
        if source not in self.sources:
            self.sources.append(source)

    def excuse(self, url_class: str, kind: CaseKind, reason: str) -> None:
        self.not_applicable.append(NotApplicable(url_class, kind, reason))

    def build(self) -> AcceptanceCorpus:
        return AcceptanceCorpus(
            domain=self.domain,
            cases=tuple(self.cases),
            not_applicable=tuple(self.not_applicable),
        )

    def gaps(self, url_class: str) -> list[CaseKind]:
        """Kinds nobody has covered or excused — the operator's to-do list."""

        corpus = self.build()
        coverage = corpus.coverage(url_class, expected=EXPECTED_KINDS)
        return [CaseKind(k) for k, v in coverage.items() if v == Applicability.MISSING.value]
