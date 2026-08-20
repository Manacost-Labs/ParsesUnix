"""Strict response validation for consumers embedding the scraper core.

The full Fetch Gateway gets its validation rules from a Site Profile. Smaller
consumers sometimes inject a transport directly, so they need an equally strict
way to preserve the expected content type and the proof that useful content
arrived. This module is that boundary; it does not certify parsed records or
authorize publication.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from web_scraper.contracts import ContentKind, ContentRules, TriageResult, Verdict
from web_scraper.extract.content_kind import detect_content_kind
from web_scraper.fetchers.base import RawResponse, Transport
from web_scraper.triage import classify_response


@dataclass(frozen=True)
class ResponseContract:
    """Fail-closed proof required before an embedded response is accepted.

    HTML and text must contain declared canaries. JSON must expose declared
    schema paths. Requiring those proofs at construction time prevents a caller
    from accidentally treating an arbitrary non-empty HTTP 200 as useful data.
    """

    expected_kind: ContentKind
    min_body_bytes: int = 200
    canaries: tuple[str, ...] = ()
    required_json_paths: tuple[str, ...] = ()
    stop_signatures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.expected_kind.is_extractable:
            raise ValueError("embedded contracts support only extractable content kinds")
        if self.min_body_bytes < 1:
            raise ValueError("min_body_bytes must be at least 1")

        self._require_non_empty(self.canaries, "canary")
        self._require_non_empty(self.required_json_paths, "JSON path")
        self._require_non_empty(self.stop_signatures, "stop signature")

        if self.expected_kind is ContentKind.JSON:
            if not self.required_json_paths:
                raise ValueError("a JSON contract requires at least one JSON path")
            if self.canaries:
                raise ValueError("a JSON contract uses JSON paths, not text canaries")
        else:
            if not self.canaries:
                raise ValueError("an HTML or text contract requires at least one canary")
            if self.required_json_paths:
                raise ValueError("only a JSON contract may declare JSON paths")

    @staticmethod
    def _require_non_empty(values: tuple[str, ...], label: str) -> None:
        if any(not value.strip() for value in values):
            raise ValueError(f"{label} values must not be empty")
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate {label} values are not allowed")

    @classmethod
    def html(
        cls,
        *,
        canaries: tuple[str, ...] = (),
        min_body_bytes: int = 200,
        stop_signatures: tuple[str, ...] = (),
    ) -> ResponseContract:
        return cls(
            expected_kind=ContentKind.HTML,
            min_body_bytes=min_body_bytes,
            canaries=canaries,
            stop_signatures=stop_signatures,
        )

    @classmethod
    def json(
        cls,
        *,
        required_json_paths: tuple[str, ...] = (),
        min_body_bytes: int = 2,
        stop_signatures: tuple[str, ...] = (),
    ) -> ResponseContract:
        return cls(
            expected_kind=ContentKind.JSON,
            min_body_bytes=min_body_bytes,
            required_json_paths=required_json_paths,
            stop_signatures=stop_signatures,
        )

    @classmethod
    def text(
        cls,
        *,
        canaries: tuple[str, ...] = (),
        min_body_bytes: int = 1,
        stop_signatures: tuple[str, ...] = (),
    ) -> ResponseContract:
        return cls(
            expected_kind=ContentKind.TEXT,
            min_body_bytes=min_body_bytes,
            canaries=canaries,
            stop_signatures=stop_signatures,
        )

    def content_rules(self) -> ContentRules:
        """Compile to the canonical triage rules without duplicating triage."""

        return ContentRules(
            min_body_bytes=self.min_body_bytes,
            canaries=self.canaries,
            expected_content_type=self.expected_kind.value.lower(),
            required_json_paths=self.required_json_paths,
            stop_signatures=self.stop_signatures,
        )


TelemetryValue = str | int | bool | None


@dataclass(frozen=True)
class ValidatedResponse:
    """A raw response plus bounded evidence from canonical triage."""

    response: RawResponse
    triage: TriageResult
    content_kind: ContentKind
    content_sha256: str

    @property
    def transport_validated(self) -> bool:
        """Whether one complete response passed its declared content contract."""

        return self.triage.verdict is Verdict.OK and not self.response.truncated

    def telemetry(self) -> dict[str, TelemetryValue]:
        """Return safe diagnostics without URLs, headers, bodies, or errors."""

        final_host = urlsplit(self.response.final_url).hostname or ""
        reason = self.triage.reason
        if self.response.transport_error:
            reason = "transport failed before a complete response"
        return {
            "final_host": final_host,
            "status": self.response.status,
            "verdict": self.triage.verdict.value,
            "reason": reason,
            "body_bytes": len(self.response.body),
            "elapsed_ms": self.response.elapsed_ms,
            "truncated": self.response.truncated,
            "content_kind": self.content_kind.value,
            "content_sha256": self.content_sha256,
            "transport_validated": self.transport_validated,
            "paid_escalation_allowed": self.triage.paid_escalation_allowed,
        }


def validate_response(response: RawResponse, contract: ResponseContract) -> ValidatedResponse:
    """Validate one raw response against an explicit embedded contract."""

    content_kind = detect_content_kind(response.body, response.headers)
    if response.truncated:
        triage = TriageResult(
            Verdict.PARSE_FAIL,
            "response is truncated; a prefix is not a complete document",
            response.status,
            len(response.body),
        )
    else:
        triage = classify_response(
            status=response.status,
            body=response.body,
            headers=response.headers,
            rules=contract.content_rules(),
            source="target",
            transport_error=response.transport_error,
        )
    return ValidatedResponse(
        response=response,
        triage=triage,
        content_kind=content_kind,
        content_sha256=hashlib.sha256(response.body).hexdigest(),
    )


def fetch_validated(
    transport: Transport,
    url: str,
    contract: ResponseContract,
    *,
    headers: Mapping[str, str] | None = None,
) -> ValidatedResponse:
    """Fetch through the caller's explicit transport, then validate strictly."""

    return validate_response(transport.fetch(url, headers=headers), contract)
