"""Strict embedded validation for consumers that do not run a full Site Profile.

The fixtures mirror the response *shapes* that exposed the integration gap in
the Hearthstone parser. They contain no captured production rows or secrets.
"""

from __future__ import annotations

import json
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from web_scraper import (
    ContentKind,
    ResponseContract,
    Verdict,
    fetch_validated,
    validate_response,
)
from web_scraper.fetchers import RawResponse


def response(
    body: bytes,
    *,
    content_type: str,
    url: str = "https://data.example/source",
    status: int | None = 200,
    truncated: bool = False,
    transport_error: str | None = None,
) -> RawResponse:
    return RawResponse(
        requested_url=url,
        final_url=url,
        status=status,
        headers={"Content-Type": content_type, "X-Debug": "must-not-leak"},
        body=body,
        elapsed_ms=17,
        truncated=truncated,
        transport_error=transport_error,
    )


class ResponseContractTests(unittest.TestCase):
    def test_html_contract_requires_a_content_canary(self) -> None:
        with self.assertRaisesRegex(ValueError, "canary"):
            ResponseContract.html()

    def test_json_contract_requires_a_schema_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON path"):
            ResponseContract.json()

    def test_binary_is_not_an_embedded_data_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "extractable"):
            ResponseContract(expected_kind=ContentKind.BINARY, canaries=("PNG",))

    def test_json_contract_compiles_to_canonical_content_rules(self) -> None:
        contract = ResponseContract.json(
            required_json_paths=("decks.0.archetypeId",), min_body_bytes=2
        )
        rules = contract.content_rules()
        self.assertEqual(rules.expected_content_type, "json")
        self.assertEqual(rules.required_json_paths, ("decks.0.archetypeId",))
        self.assertEqual(rules.min_body_bytes, 2)


class HearthstoneShapedValidationTests(unittest.TestCase):
    def test_hsguru_ssr_html_passes_only_with_its_canary(self) -> None:
        body = (
            b"<!doctype html><html><head><title>Meta</title></head>"
            b"<body><main data-hsguru-meta>53 archetypes</main></body></html>"
        )
        result = validate_response(
            response(body, content_type="text/html"),
            ResponseContract.html(canaries=("data-hsguru-meta",), min_body_bytes=100),
        )
        self.assertIs(result.triage.verdict, Verdict.OK)
        self.assertIs(result.content_kind, ContentKind.HTML)
        self.assertTrue(result.transport_validated)

    def test_hsreplay_array_json_passes_required_paths(self) -> None:
        body = json.dumps(
            [{"trinket": {"id": 123}, "pick_rate": 0.15, "avg_placement": 4.1}]
        ).encode()
        result = validate_response(
            response(body, content_type="application/json"),
            ResponseContract.json(required_json_paths=("0.trinket.id", "0.pick_rate")),
        )
        self.assertIs(result.triage.verdict, Verdict.OK)
        self.assertIs(result.content_kind, ContentKind.JSON)

    def test_firestone_object_json_passes_required_paths(self) -> None:
        body = json.dumps(
            {
                "lastUpdate": "2026-08-20T18:00:00Z",
                "decks": [{"archetypeId": 7, "games": 1200}],
            }
        ).encode()
        result = validate_response(
            response(body, content_type="application/json"),
            ResponseContract.json(
                required_json_paths=("lastUpdate", "decks.0.archetypeId", "decks.0.games")
            ),
        )
        self.assertIs(result.triage.verdict, Verdict.OK)
        self.assertTrue(result.transport_validated)

    def test_json_cannot_pass_an_html_contract(self) -> None:
        body = b'{"decks": [{"archetypeId": 7}]}'
        result = validate_response(
            response(body, content_type="application/json"),
            ResponseContract.html(canaries=("archetypeId",), min_body_bytes=2),
        )
        self.assertIs(result.triage.verdict, Verdict.PARSE_FAIL)
        self.assertFalse(result.transport_validated)

    def test_client_rendered_shell_stays_csr_required(self) -> None:
        body = (
            b"<!doctype html><html><head><script src='/app.js'></script></head>"
            b"<body><div id='root'></div></body></html>"
        )
        result = validate_response(
            response(body, content_type="text/html"),
            ResponseContract.html(canaries=("actual-tier-data",), min_body_bytes=50),
        )
        self.assertIs(result.triage.verdict, Verdict.CSR_REQUIRED)
        self.assertFalse(result.triage.paid_escalation_allowed)

    def test_truncated_body_is_never_a_valid_document(self) -> None:
        body = b'{"decks": [{"archetypeId": 7}]}'
        result = validate_response(
            response(body, content_type="application/json", truncated=True),
            ResponseContract.json(required_json_paths=("decks.0.archetypeId",)),
        )
        self.assertIs(result.triage.verdict, Verdict.PARSE_FAIL)
        self.assertIn("truncated", result.triage.reason.lower())
        self.assertFalse(result.transport_validated)


class FakeTransport:
    def __init__(self, result: RawResponse) -> None:
        self.result = result
        self.calls: list[tuple[str, Mapping[str, str] | None]] = []

    def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> RawResponse:
        self.calls.append((url, headers))
        return self.result


class EmbeddedFetchTests(unittest.TestCase):
    def test_fetch_uses_the_explicit_transport_and_contract(self) -> None:
        url = "https://api.example/data"
        transport = FakeTransport(response(b'{"data": {"id": 1}}', content_type="application/json"))
        result = fetch_validated(
            transport,
            url,
            ResponseContract.json(required_json_paths=("data.id",)),
            headers={"Accept": "application/json"},
        )
        self.assertEqual(transport.calls, [(url, {"Accept": "application/json"})])
        self.assertIs(result.triage.verdict, Verdict.OK)

    def test_telemetry_never_contains_query_headers_or_body(self) -> None:
        url = "https://api.example/data?token=do-not-print"
        result = validate_response(
            response(
                b'{"data": {"id": "private-row-value"}}',
                content_type="application/json",
                url=url,
            ),
            ResponseContract.json(required_json_paths=("data.id",)),
        )
        telemetry = result.telemetry()
        serialized = json.dumps(telemetry, sort_keys=True)
        self.assertEqual(telemetry["final_host"], "api.example")
        self.assertNotIn("do-not-print", serialized)
        self.assertNotIn("token", serialized)
        self.assertNotIn("X-Debug", serialized)
        self.assertNotIn("private-row-value", serialized)
        self.assertIn("content_sha256", telemetry)

    def test_transport_error_detail_is_sanitized_in_telemetry(self) -> None:
        url = "https://api.example/data?token=do-not-print"
        raw = response(
            b"",
            content_type="text/plain",
            url=url,
            status=None,
            transport_error=f"timeout while requesting {url}",
        )
        result = validate_response(
            raw,
            ResponseContract.text(canaries=("ready",), min_body_bytes=1),
        )
        telemetry = json.dumps(result.telemetry(), sort_keys=True)
        self.assertIs(result.triage.verdict, Verdict.ORIGIN_DOWN)
        self.assertNotIn("do-not-print", telemetry)
        self.assertNotIn("timeout while requesting", telemetry)


if __name__ == "__main__":
    unittest.main()
