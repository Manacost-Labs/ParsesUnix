"""The corpus every provider is judged on, and the rules that judge it.

A provider comparison is only worth reading if every vendor answered the *same*
question. The temptation runs the other way: give the cheap provider the easy
pages, give the expensive one the site that fights back, and publish the success
rates side by side. The resulting table is arithmetic performed on nothing.

So the corpus is declared once, up front, and every strategy of every provider
is offered every target. A strategy that cannot address a target is recorded as
:data:`INELIGIBLE` — which is a different fact from a failure and is never
averaged into a success rate.

The validation rules live here too, next to the targets, for the same reason:
they must be identical across vendors. They are expressed as
:class:`~web_scraper.contracts.ContentRules` and judged by canonical triage, so
"validated" means exactly what it means in production. A second definition of
success invented for the benchmark would measure the benchmark.

Nothing sensitive belongs in a corpus file. It is a list of public URLs and the
shape they are expected to have; credentials, cookies and private hosts are not
corpus material and are rejected rather than carried.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from web_scraper.contracts import ContentKind, ContentRules
from web_scraper.observability.manifest import stable_hash


class TargetKind(StrEnum):
    """What a target is *for*. Segment winners are reported per kind.

    A provider that wins on server-rendered HTML has told us nothing about how
    it handles a client-rendered shell, and a single overall ranking hides
    exactly that.
    """

    SSR_HTML = "ssr_html"
    CSR_SHELL = "csr_shell"
    HARD_BLOCK = "hard_block"
    DEAD_URL = "dead_url"
    JSON_ENDPOINT = "json_endpoint"
    LISTING = "listing"
    LARGE_HTML = "large_html"
    CROSS_ORIGIN_DATA = "cross_origin_data"


@dataclass(frozen=True)
class CorpusTarget:
    """One page, and what a correct answer about it looks like."""

    url: str
    domain: str
    url_class: str
    kind: TargetKind
    expected_content_kind: ContentKind = ContentKind.HTML
    #: What the SITE should answer. For a dead URL this is 404, and a provider
    #: reporting anything else has misreported the target — the defect that hit
    #: three of the five adapters and would have re-billed dead URLs forever.
    expected_target_status: int = 200
    min_body_bytes: int = 500
    canaries: tuple[str, ...] = ()
    required_json_paths: tuple[str, ...] = ()
    #: Fields a profile would want from this page. Reported as extraction
    #: quality, never as the provider's success — a provider that delivers the
    #: document has done its job even if our selectors are wrong.
    critical_fields: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme not in {"http", "https"}:
            raise ValueError(f"corpus target must be http(s): {self.url!r}")
        if not parts.netloc:
            raise ValueError(f"corpus target has no host: {self.url!r}")
        if parts.query and any(
            marker in parts.query.lower() for marker in ("key=", "token=", "secret", "password")
        ):
            # A corpus file is committed and printed. A URL carrying a credential
            # would end up in the artifact, the report and the repository.
            raise ValueError(f"corpus target looks like it carries a credential: {self.url!r}")

    @property
    def expects_success(self) -> bool:
        """Should a working provider return a usable document here?"""

        return 200 <= self.expected_target_status < 300

    def rules(self) -> ContentRules:
        """The validation every provider's answer is held to, identically."""

        return ContentRules(
            min_body_bytes=self.min_body_bytes if self.expects_success else 0,
            canaries=self.canaries,
            expected_content_type=(
                "json" if self.expected_content_kind is ContentKind.JSON else "html"
            ),
            required_json_paths=self.required_json_paths,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "domain": self.domain,
            "url_class": self.url_class,
            "kind": self.kind.value,
            "expected_content_kind": self.expected_content_kind.value,
            "expected_target_status": self.expected_target_status,
            "min_body_bytes": self.min_body_bytes,
            "canaries": list(self.canaries),
            "required_json_paths": list(self.required_json_paths),
            "critical_fields": list(self.critical_fields),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class Corpus:
    """The whole question, hashed so a report can prove which one it answered."""

    name: str
    targets: tuple[CorpusTarget, ...]
    description: str = ""
    #: Domains excluded before any call, with the reason. Kept in the corpus
    #: rather than discovered per run so the exclusion is reviewable.
    skipped_by_policy: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.targets:
            raise ValueError("a corpus with no targets cannot compare anything")

    @property
    def fingerprint(self) -> str:
        """Identity of the question. Two reports with different hashes are not
        comparable, however similar their tables look."""

        return stable_hash([t.to_dict() for t in self.targets])

    @property
    def domains(self) -> tuple[str, ...]:
        return tuple(sorted({t.domain for t in self.targets}))

    @property
    def url_classes(self) -> tuple[str, ...]:
        return tuple(sorted({t.url_class for t in self.targets}))

    @property
    def kinds(self) -> tuple[TargetKind, ...]:
        return tuple(sorted({t.kind for t in self.targets}))

    def of_kind(self, kind: TargetKind) -> tuple[CorpusTarget, ...]:
        return tuple(t for t in self.targets if t.kind is kind)

    def without(self, domains: Iterable[str], *, reason: str) -> Corpus:
        """Drop domains policy forbids, recording why rather than deleting them."""

        excluded = set(domains)
        kept = tuple(t for t in self.targets if t.domain not in excluded)
        if not kept:
            raise ValueError(f"every target was excluded: {reason}")
        return Corpus(
            name=self.name,
            targets=kept,
            description=self.description,
            skipped_by_policy={**self.skipped_by_policy, **dict.fromkeys(excluded, reason)},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "fingerprint": self.fingerprint,
            "targets": [t.to_dict() for t in self.targets],
            "domains": list(self.domains),
            "url_classes": list(self.url_classes),
            "kinds": [k.value for k in self.kinds],
            "skipped_by_policy": dict(self.skipped_by_policy),
        }


def corpus_from_mapping(payload: Mapping[str, Any]) -> Corpus:
    """Build a corpus from a parsed manifest, refusing anything malformed.

    Strict on purpose. A typo in ``expected_target_status`` silently turns a
    dead-URL probe into a success test, and the resulting report would be wrong
    in the one place this whole exercise exists to get right.
    """

    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, str | bytes):
        raise ValueError("corpus manifest needs a 'targets' list")

    targets: list[CorpusTarget] = []
    for index, item in enumerate(raw_targets):
        if not isinstance(item, Mapping):
            raise ValueError(f"target #{index + 1} is not a mapping")
        raw_expected = item.get("expected")
        expected: Mapping[str, Any] = raw_expected if isinstance(raw_expected, Mapping) else {}
        url = str(item.get("url", ""))
        targets.append(
            CorpusTarget(
                url=url,
                domain=str(item.get("domain") or urlsplit(url).netloc),
                url_class=str(item.get("url_class", "page")),
                kind=TargetKind(str(item.get("kind", TargetKind.SSR_HTML.value))),
                expected_content_kind=ContentKind(
                    str(expected.get("content_kind", ContentKind.HTML.value)).upper()
                ),
                expected_target_status=int(expected.get("target_status", 200)),
                min_body_bytes=int(expected.get("min_body_bytes", 500)),
                canaries=tuple(str(c) for c in expected.get("canaries", ())),
                required_json_paths=tuple(str(p) for p in expected.get("json_paths", ())),
                critical_fields=tuple(str(f) for f in expected.get("critical_fields", ())),
                notes=str(item.get("notes", "")),
            )
        )
    return Corpus(
        name=str(payload.get("name", "corpus")),
        targets=tuple(targets),
        description=str(payload.get("description", "")),
        skipped_by_policy={
            str(k): str(v) for k, v in (payload.get("skipped_by_policy") or {}).items()
        },
    )


def load_corpus(path: str | Path) -> Corpus:
    """Read a corpus manifest. JSON always; YAML when a parser is available."""

    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() in {".yaml", ".yml"}:
        from web_scraper.profiles.yamlish import loads

        payload = loads(text)
    else:
        payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{file} does not contain a corpus manifest")
    return corpus_from_mapping(payload)
