"""Run configuration: which profile, which state paths, which window/limits.

A Site Profile is per-domain (routes, extractors, thresholds). A run needs the
extra per-execution facts a profile does not carry: where state lives, the time
window, batch size, and whether this is a full revision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class RunConfig:
    profile_path: Path
    state_dir: Path
    seed_urls: tuple[str, ...] = ()
    deadline_seconds: float | None = None  # run-window cap; None = until the queue drains
    batch_size: int = 20
    full_review: bool = False  # ignore freshness intervals; re-check everything
    allow_private: bool = False
    dead_zone_after_attempts: int = 3
    sweep: bool = False  # phase-A HEAD sweep to quarantine dead URLs before the main pass

    @property
    def queue_path(self) -> Path:
        return self.state_dir / "queue.sqlite3"

    @property
    def dataset_path(self) -> Path:
        return self.state_dir / "dataset.sqlite3"

    @property
    def freshness_path(self) -> Path:
        return self.state_dir / "freshness.sqlite3"

    @property
    def snapshot_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @classmethod
    def from_file(cls, path: str | Path) -> "RunConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data, base_dir=Path(path).parent)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, base_dir: Path | None = None) -> "RunConfig":
        base = base_dir or Path.cwd()

        def resolve(p: str) -> Path:
            path = Path(p)
            return path if path.is_absolute() else (base / path)

        return cls(
            profile_path=resolve(data["profile"]),
            state_dir=resolve(data.get("state_dir", "state")),
            seed_urls=tuple(data.get("seed_urls", ())),
            deadline_seconds=data.get("deadline_seconds"),
            batch_size=int(data.get("batch_size", 20)),
            full_review=bool(data.get("full_review", False)),
            allow_private=bool(data.get("allow_private", False)),
            dead_zone_after_attempts=int(data.get("dead_zone_after_attempts", 3)),
            sweep=bool(data.get("sweep", False)),
        )
