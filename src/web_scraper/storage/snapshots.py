"""Snapshot store: every gateway attempt is persisted with secrets redacted.

What is written is already sanitized: header values, URL query parameters, and
well-known secret shapes inside the body are masked before touching disk, so a
snapshot can never leak a session token or signed URL.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from web_scraper.fetchers.base import RawResponse
from web_scraper.storage.redaction import redact_body, redact_headers, redact_url


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
        # pid + short uuid make the stem unique even for two processes writing
        # the same URL within the same millisecond.
        unique = f"{os.getpid():d}-{uuid.uuid4().hex[:6]}"
        stem = f"{stamp}-{attempt_index:02d}-{verdict.lower()}-{unique}"
        body_path = directory / f"{stem}.body"
        meta_path = directory / f"{stem}.meta.json"

        stored_body = redact_body(response.body)
        body_path.write_bytes(stored_body)
        meta = {
            "url": redact_url(url),
            "requested_url": redact_url(response.requested_url),
            "final_url": redact_url(response.final_url),
            "status": response.status,
            "verdict": verdict,
            "headers": redact_headers(response.headers),
            "body_file": body_path.name,
            "body_bytes": len(stored_body),
            "body_sha256": hashlib.sha256(stored_body).hexdigest(),
            "truncated": response.truncated,
            "elapsed_ms": response.elapsed_ms,
            "transport_error": response.transport_error,
            "saved_at": datetime.fromtimestamp(self._now(), tz=timezone.utc).isoformat(),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        index_path = self.root / "index.jsonl"
        line = json.dumps(
            {"key": key, "url": redact_url(url), "snapshot": stem}, ensure_ascii=False
        )
        # One O_APPEND write of a single line is atomic across processes on POSIX.
        with index_path.open("a", encoding="utf-8") as index:
            index.write(line + "\n")
        return meta_path

    def prune(
        self,
        *,
        max_age_days: float | None = None,
        max_total_bytes: int | None = None,
    ) -> list[Path]:
        """Delete old snapshots by age and/or total size (newest kept first).

        Returns the meta paths removed. Retention is a maintenance operation the
        runner calls periodically, not on every write.
        """

        entries: list[tuple[float, Path, Path, int]] = []  # (mtime, meta, body, size)
        for meta_path in self.root.glob("*/*.meta.json"):
            body_path = meta_path.with_suffix("").with_suffix(".body")
            try:
                size = meta_path.stat().st_size + (
                    body_path.stat().st_size if body_path.exists() else 0
                )
                mtime = meta_path.stat().st_mtime
            except OSError:
                continue
            entries.append((mtime, meta_path, body_path, size))

        entries.sort(key=lambda item: item[0], reverse=True)  # newest first
        removed: list[Path] = []
        now = self._now()
        running_total = 0
        for mtime, meta_path, body_path, size in entries:
            too_old = max_age_days is not None and (now - mtime) > max_age_days * 86400
            over_budget = max_total_bytes is not None and running_total + size > max_total_bytes
            if too_old or over_budget:
                for path in (meta_path, body_path):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                removed.append(meta_path)
            else:
                running_total += size
        return removed
