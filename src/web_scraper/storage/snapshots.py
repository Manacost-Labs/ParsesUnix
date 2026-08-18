"""Snapshot store: every gateway attempt is persisted with secrets redacted."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from web_scraper.fetchers.base import RawResponse
from web_scraper.storage.redaction import redact_headers


class SnapshotStore:
    """Writes one meta/body pair per attempt under a per-URL directory."""

    def __init__(self, root: str | Path, *, now: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self._now = now

    def save(
        self,
        *,
        url: str,
        attempt_index: int,
        response: RawResponse,
        verdict: str,
    ) -> Path:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        directory = self.root / key
        directory.mkdir(parents=True, exist_ok=True)

        stamp = int(self._now() * 1000)
        stem = f"{stamp}-{attempt_index:02d}-{verdict.lower()}"
        body_path = directory / f"{stem}.body"
        meta_path = directory / f"{stem}.meta.json"

        body_path.write_bytes(response.body)
        meta = {
            "url": url,
            "requested_url": response.requested_url,
            "final_url": response.final_url,
            "status": response.status,
            "verdict": verdict,
            "headers": redact_headers(response.headers),
            "body_file": body_path.name,
            "body_bytes": len(response.body),
            "body_sha256": hashlib.sha256(response.body).hexdigest(),
            "elapsed_ms": response.elapsed_ms,
            "transport_error": response.transport_error,
            "saved_at": datetime.fromtimestamp(self._now(), tz=timezone.utc).isoformat(),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        index_path = self.root / "index.jsonl"
        with index_path.open("a", encoding="utf-8") as index:
            index.write(json.dumps({"key": key, "url": url, "snapshot": stem}, ensure_ascii=False) + "\n")
        return meta_path
