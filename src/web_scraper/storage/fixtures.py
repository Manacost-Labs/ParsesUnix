"""Saved HTTP responses: the offline ground truth for triage verdicts.

Each fixture directory contains ``meta.json`` (status, headers, rules,
expected verdict) plus the referenced body file. Tests replay these
through triage so every verdict is proven by a saved response instead of
live network traffic.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web_scraper.contracts import ContentRules, TriageResult
from web_scraper.triage import classify_response


@dataclass(frozen=True)
class SavedResponse:
    name: str
    url: str
    status: int | None
    headers: dict[str, str]
    body: bytes
    rules: ContentRules
    expected: dict[str, Any]
    probe_expectations: dict[str, Any]

    def triage(self) -> TriageResult:
        return classify_response(
            status=self.status,
            body=self.body,
            headers=self.headers,
            rules=self.rules,
        )


def _rules_from_meta(raw: dict[str, Any]) -> ContentRules:
    return ContentRules(
        min_body_bytes=raw.get("min_body_bytes", 200),
        canary=raw.get("canary"),
        canaries=tuple(raw.get("canaries", ())),
        expected_content_type=raw.get("expected_content_type"),
        required_json_paths=tuple(raw.get("required_json_paths", ())),
        stop_signatures=tuple(raw.get("stop_signatures", ())),
    )


def load_saved_response(directory: str | Path) -> SavedResponse:
    fixture_dir = Path(directory)
    meta = json.loads((fixture_dir / "meta.json").read_text(encoding="utf-8"))
    body_file = meta.get("body_file")
    body = (fixture_dir / body_file).read_bytes() if body_file else b""
    return SavedResponse(
        name=meta.get("name", fixture_dir.name),
        url=meta["url"],
        status=meta.get("status"),
        headers={str(k): str(v) for k, v in meta.get("headers", {}).items()},
        body=body,
        rules=_rules_from_meta(meta.get("rules", {})),
        expected=meta.get("expected", {}),
        probe_expectations=meta.get("probe_expectations", {}),
    )


def iter_saved_responses(root: str | Path) -> Iterator[SavedResponse]:
    for meta_path in sorted(Path(root).glob("*/meta.json")):
        yield load_saved_response(meta_path.parent)
